"""Offline behavior and HTTP boundary checks for the synthetic setup portal."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from wifi_portal import DemoNetwork, PortalServer, Setup, validate_join


def join_data(ssid='Synthetic home', security='wpa2', password='synthetic-only'):
    return dict(ssid=ssid, security=security, password=password)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.network = DemoNetwork(clock=lambda: self.now)
        self.setup = Setup(self.network)

    def test_startup_progress_failure_retry_success(self):
        self.assertEqual(self.setup.status()['state'], 'ready')
        self.assertTrue(self.setup.join(join_data(password='wrong-password')))
        self.assertEqual(self.setup.status()['state'], 'connecting')
        self.assertFalse(self.setup.join(join_data()))
        self.now = 2
        self.assertEqual(self.setup.status()['state'], 'failed')
        self.assertTrue(self.setup.join(join_data(ssid='Hidden synthetic network')))
        self.now = 4
        status = self.setup.status()
        self.assertEqual(status['state'], 'connected')
        self.assertTrue(status['demo'])
        self.assertEqual(status['ssid'], 'Hidden synthetic network')
        self.assertNotIn('synthetic-only', json.dumps(status))
        self.assertNotIn('synthetic-only', repr(vars(self.network)))

    def test_open_and_utf8_hidden_networks(self):
        self.assertTrue(self.setup.join(join_data(ssid='é' * 16, security='open', password='')))
        with self.assertRaises(ValueError):
            validate_join(join_data(ssid='é' * 17))

    def test_input_validation_preserves_spaces_and_rejects_bad_credentials(self):
        self.assertEqual(validate_join(join_data(ssid=' Home '))[0], ' Home ')
        for payload in (None, [], {}, join_data(ssid=''), join_data(ssid='bad\nname'),
                        join_data(password='short'), join_data(password=42),
                        join_data(password='é' * 8), join_data(security='enterprise'),
                        join_data(security='open'), join_data(ssid='x' * 33)):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_join(payload)
        validate_join(join_data(password='a' * 64))
        with self.assertRaises(ValueError):
            validate_join(join_data(password='z' * 64))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.server = PortalServer(('127.0.0.1', 0), Setup(DemoNetwork(clock=lambda: self.now)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(self, data=None, headers=None, body=None):
        request_headers = {'Content-Type': 'application/json', 'X-Setup-Token': self.server.token,
                           'Origin': self.server.origin}
        request_headers.update(headers or {})
        return self.request('/api/join', 'POST', body if body is not None else json.dumps(data or join_data()), request_headers)

    def test_page_assets_security_headers_and_version_fixture(self):
        code, headers, body = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertNotIn(b'__SETUP_TOKEN__', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ('/app.js', '/style.css'):
            self.assertEqual(self.request(path)[0], 200)
        fixture = json.loads((Path(__file__).parents[1] / 'docs/fixtures/wifi-setup-v3.json').read_text())
        self.assertEqual(json.loads(self.request('/api/status')[2]), fixture['ready'])
        self.assertEqual(self.request('/not-here')[0], 404)
        self.assertEqual(self.request('/../../AGENTS.md')[0], 404)

    def test_captive_probe_redirects_are_local(self):
        for path in ('/generate_204', '/hotspot-detect.html', '/connecttest.txt', '/redirect'):
            code, headers, _ = self.request(path)
            self.assertEqual(code, 302)
            self.assertEqual(headers['Location'], '/')

    def test_real_captive_probe_redirect_precedes_host_gate(self):
        self.server.real = True
        self.server.authority = '192.168.4.1'
        self.server.origin = 'http://192.168.4.1'
        code, headers, _ = self.request('/generate_204', headers={'Host': 'connectivity.example'})
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], 'http://192.168.4.1/')
        self.assertEqual(self.request('/api/status', headers={'Host': 'connectivity.example'})[0], 403)
        code, _, body = self.request('/', headers={'Host': '192.168.4.1'})
        self.assertEqual(code, 200)
        self.assertIn(b'RADIO WI-FI SETUP', body)
        self.assertNotIn(b'LOCAL DEMO', body)
        self.assertEqual(self.request('/', headers={'Host': '192.168.4.1:80'})[0], 200)

    def test_host_origin_and_csrf_boundary(self):
        self.assertEqual(self.request('/', headers={'Host': 'attacker.example'})[0], 403)
        for headers in ({'X-Setup-Token': ''}, {'X-Setup-Token': 'wrong'},
                        {'Origin': 'https://attacker.example'}, {'Origin': 'null'},
                        {'Host': '192.168.50.1:8080'}, {'X-Setup-Token': 'é'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.post()[0], 409)
        self.now = 2
        self.assertEqual(json.loads(self.request('/api/status')[2])['state'], 'connected')

    def test_password_never_appears_in_responses_or_logs(self):
        logs = io.StringIO()
        password = '-'.join(('synthetic', 'secret', '928'))
        with contextlib.redirect_stderr(logs):
            code, _, response = self.post(join_data(password=password))
            self.assertEqual(code, 202)
            self.assertNotIn(password.encode(), response)
            for path in ('/', '/api/status', '/api/networks'):
                self.assertNotIn(password.encode(), self.request(path)[2])
            self.now = 2
            self.assertNotIn(password.encode(), self.request('/api/status')[2])
        self.assertNotIn(password, logs.getvalue())
        self.assertEqual(logs.getvalue(), '')

    def test_malformed_and_oversized_requests_do_not_start_join(self):
        for body, headers, expected in (
                ('{', {}, 400), ('null', {}, 400), ('[]', {}, 400),
                ('x' * 2049, {}, 413), ('{}', {'Content-Type': 'text/plain'}, 415),
                ('{}', {'Transfer-Encoding': 'chunked'}, 415),
                ('{}', {'Content-Length': '-1'}, 413),
                ('{}', {'Content-Length': 'nonsense'}, 400),
                ('[' * 1000 + ']' * 1000, {}, 400)):
            with self.subTest(expected=expected, headers=headers):
                self.assertEqual(self.post(body=body, headers=headers)[0], expected)
        self.assertEqual(json.loads(self.request('/api/status')[2])['state'], 'ready')

    def test_ap_gateway_host_and_origin_shape_without_exposure(self):
        self.server.authority = '192.168.50.1'
        self.server.origin = 'http://192.168.50.1'
        self.assertEqual(self.request('/', headers={'Host': '192.168.50.1'})[0], 200)
        self.assertEqual(self.post(headers={'Host': '192.168.50.1', 'Origin': 'http://192.168.50.1'})[0], 202)
        self.assertEqual(self.post(headers={'Host': '192.168.50.1', 'Origin': 'http://evil.example'})[0], 403)

    def test_unsupported_method_does_not_reflect_request(self):
        code, _, body = self.request('/synthetic-secret', method='DELETE')
        self.assertEqual(code, 501)
        self.assertNotIn(b'synthetic-secret', body)
        self.assertEqual(json.loads(body), {'error': 'Request not supported.'})

    def test_partial_body_inactivity_times_out_without_join_or_reflection(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.putrequest('POST', '/api/join')
            for name, value in {'Content-Type': 'application/json',
                                'X-Setup-Token': self.server.token,
                                'Content-Length': '100'}.items():
                conn.putheader(name, value)
            conn.endheaders()
            conn.send(b'{"password":"synthetic-secret')
            response = conn.getresponse()
            self.assertEqual(response.status, 408)
            self.assertNotIn(b'synthetic-secret', response.read())
        finally:
            conn.close()
        self.assertEqual(self.server.setup.status()['state'], 'ready')

    def test_demo_refuses_nonloopback_bind(self):
        with self.assertRaises(ValueError):
            PortalServer(('0.0.0.0', 0), Setup(DemoNetwork()))


if __name__ == '__main__':
    unittest.main()
