"""Synthetic network/session checks. No daemon, privilege or radio required."""
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from host.wifi_network import AP_UUID, NetworkError, NetworkManager, keyfile, private_write, terse
from host.wifi_setup import Session, Control
from host.wifi_portal import HelperNetwork

ORIGINAL = '16d8b65c-142b-4b66-94ea-35478d0d20c0'


class FakeNetwork:
    def __init__(self, directory):
        self.directory, self.selected, self.profiles = Path(directory), ORIGINAL, {ORIGINAL: 'untouched original'}
        self.fail_join, self.fail_recovery, self.fail_persist = False, False, False
        self.calls = []
        self.activation_gate = None

    def setup_ready(self):
        self.calls.append('setup_ready')

    def preflight(self):
        self.calls.append('preflight')

    def autoconnect(self):
        return True

    def set_autoconnect(self, enabled):
        self.calls.append(('autoconnect', enabled))

    def deactivate(self, identity):
        self.calls.append(('deactivate', identity))
        if self.selected == identity:
            self.selected = None

    def finish_commit(self, identity):
        self.calls.append(('finish_commit', identity))

    def active(self):
        return self.selected

    def scan(self):
        return [dict(ssid='Synthetic home', security='wpa2')]

    def path(self, identity, persistent=False):
        return self.directory / (identity + ('.saved' if persistent else '.pending'))

    def load(self, identity, value, persistent=False):
        self.profiles[identity] = value
        private_write(self.path(identity, persistent), value)
        self.calls.append(('load', identity))

    def activate(self, identity):
        self.calls.append(('activate', identity))
        if identity != AP_UUID and identity != ORIGINAL:
            if self.activation_gate:
                self.activation_gate.wait(2)
            if self.fail_join:
                raise NetworkError('Synthetic DHCP/association failure')
        if self.fail_recovery and identity == AP_UUID:
            raise NetworkError('Synthetic recovery failure')
        self.selected = identity

    def restore(self, identity):
        self.activate(identity)

    def discard(self, identity):
        self.calls.append(('discard', identity))
        self.profiles.pop(identity, None)
        self.path(identity).unlink(missing_ok=True)
        self.path(identity, persistent=True).unlink(missing_ok=True)

    def persist(self, identity, value):
        private_write(self.path(identity, True), value)
        if self.fail_persist:
            raise NetworkError('Synthetic disk/reload failure')
        self.profiles[identity] = value
        self.path(identity).unlink(missing_ok=True)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        private_write(self.directory / 'access.json', json.dumps(dict(ssid='Synthetic setup', password='synthetic-password')))
        self.network = FakeNetwork(self.directory)
        self.commands = []
        self.now = 100
        self.session = Session(self.network, self.directory, lambda args: self.commands.append(args) or '', lambda: self.now, pause=lambda _: None, runtime=self.directory/'runtime', status_directory=self.directory/'status')
        self.session.start()

    def join(self):
        self.assertTrue(self.session.join(dict(ssid='Synthetic home', security='wpa2', password='synthetic-secret')))
        for _ in range(600):
            if self.session.state != 'connecting':
                with self.session.lock:
                    return
            time.sleep(.005)
        self.fail('Bounded synthetic join did not finish')

    def test_success_durable_without_internet_probe_and_original_untouched(self):
        self.join()
        self.assertEqual(self.session.status()['state'], 'connected')
        candidate = self.session.record['candidate']
        self.assertTrue(self.network.path(candidate, True).exists())
        self.assertIn('autoconnect=true', self.network.profiles[candidate])
        self.assertEqual(self.network.profiles[ORIGINAL], 'untouched original')
        self.assertNotIn('synthetic-secret', json.dumps(self.session.status()))
        self.assertNotIn('synthetic-secret', self.session.journal.read_text())
        self.session.stop()
        self.assertEqual(self.network.selected, candidate)
        self.assertNotIn(AP_UUID, self.network.profiles)
        self.assertFalse(self.session.journal.exists())

    def test_join_or_dhcp_failure_returns_setup_and_stop_restores_original(self):
        self.network.fail_join = True
        self.join()
        self.assertEqual(self.session.state, 'failed')
        self.assertEqual(self.network.selected, AP_UUID)
        self.assertIsNone(self.session.record['candidate'])
        self.session.stop()
        self.assertEqual(self.network.selected, ORIGINAL)
        self.assertEqual(self.network.profiles, {ORIGINAL: 'untouched original'})

    def test_persistence_failure_removes_partial_saved_profile(self):
        self.network.fail_persist = True
        self.join()
        self.assertEqual(self.session.state, 'failed')
        self.assertEqual(list(self.directory.glob('*.saved')), [])
        self.assertEqual(self.network.selected, AP_UUID)
        self.session.stop()

    def test_recovery_failure_is_distinct_and_journal_survives(self):
        self.network.fail_join = self.network.fail_recovery = True
        self.join()
        self.assertEqual(self.session.state, 'recovery_failed')
        self.assertTrue(self.session.journal.exists())
        self.session.stop()
        self.assertEqual(self.network.selected, ORIGINAL)

    def test_interrupted_session_restores_original_without_starting_ap(self):
        fresh = Session(self.network, self.directory, lambda args: self.commands.append(args) or '', runtime=self.directory/'runtime', status_directory=self.directory/'status')
        before = len(self.network.calls)
        fresh.recover()
        self.assertEqual(self.network.selected, ORIGINAL)
        self.assertNotIn(('activate', AP_UUID), self.network.calls[before:])
        self.assertFalse(fresh.journal.exists())

    def test_timeout_late_activation_cannot_commit_and_duplicate_is_rejected(self):
        gate = self.network.activation_gate = threading.Event()
        data = dict(ssid='Synthetic home', security='wpa2', password='synthetic-secret')
        self.assertTrue(self.session.join(data))
        self.assertFalse(self.session.join(data))
        self.now += 601
        gate.set()
        for _ in range(600):
            if self.session.state != 'connecting':
                break
            time.sleep(.005)
        self.assertEqual(self.session.state, 'failed')
        self.assertEqual(list(self.directory.glob('*.saved')), [])
        self.session.stop()
        self.assertEqual(self.network.selected, ORIGINAL)

    def test_offline_start_and_cancel_cleanup_do_not_require_an_old_network(self):
        self.session.stop()
        self.network.selected=None
        self.network.calls.clear()
        fresh=Session(self.network,self.directory,lambda args:'',lambda:self.now,pause=lambda _:None,
                      runtime=self.directory/'runtime',status_directory=self.directory/'status')
        old=self.network.profiles.copy()
        fresh.start()
        self.assertIsNone(fresh.record['original'])
        self.assertEqual(fresh.state,'ready')
        fresh.stop()
        self.assertIsNone(self.network.selected)
        self.assertEqual(self.network.profiles,old)
        self.assertFalse(fresh.journal.exists())
        self.assertNotIn(('activate',ORIGINAL),self.network.calls)
        status=json.loads((self.directory/'status/status.json').read_text())
        self.assertEqual((status['state'],status['terminal'],status['error']),('cancelled',True,'-'))

    def test_live_access_rotates_while_ssid_and_saved_destination_profiles_stay(self):
        first=json.loads((self.directory/'runtime/access.json').read_text())
        safe=(self.directory/'status/status.json').read_text()
        self.assertNotIn(first['password'],safe)
        self.assertNotIn('synthetic-password',self.network.profiles[AP_UUID])
        self.session.stop()
        self.assertFalse((self.directory/'runtime/access.json').exists())
        fresh=Session(self.network,self.directory,lambda args:'',lambda:self.now,pause=lambda _:None,
                      runtime=self.directory/'runtime',status_directory=self.directory/'status')
        fresh.start()
        second=json.loads((self.directory/'runtime/access.json').read_text())
        self.assertEqual(first['ssid'],second['ssid'])
        self.assertNotEqual(first['password'],second['password'])
        self.assertEqual(self.network.profiles[ORIGINAL],'untouched original')
        fresh.stop()

    def test_cancel_during_readiness_probe_reports_cancelled_after_cleanup(self):
        self.session.stop()
        entered=threading.Event();release=threading.Event();stop=threading.Event()
        def ready():
            entered.set();release.wait(2)
            raise NetworkError('Synthetic probe stopped with services')
        self.network.setup_ready=ready
        fresh=Session(self.network,self.directory,lambda args:'',lambda:self.now,pause=lambda _:None,
                      runtime=self.directory/'runtime',status_directory=self.directory/'status',stop_event=stop)
        errors=[]
        def start():
            try:fresh.start()
            except NetworkError:errors.append(True)
        thread=threading.Thread(target=start);thread.start()
        self.assertTrue(entered.wait(1));stop.set();release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertTrue(errors)
        fresh.stop(cancel=True)
        result=json.loads((self.directory/'status/status.json').read_text())
        self.assertEqual((result['state'],result['error'],result['terminal']),('cancelled','-',True))
        self.assertFalse(fresh.journal.exists())
        self.assertEqual(self.network.selected,ORIGINAL)

    def test_cancel_during_candidate_activation_does_not_reopen_setup(self):
        entered=threading.Event();release=threading.Event()
        original=self.network.activate
        def activate(identity):
            if identity not in (AP_UUID, ORIGINAL):
                entered.set();release.wait(2)
            original(identity)
        self.network.activate=activate
        self.assertTrue(self.session.join(dict(ssid='Synthetic second',security='wpa2',password='synthetic-only')))
        self.assertTrue(entered.wait(1))
        self.session.stop_event.set();release.set()
        self.session.stop(cancel=True)
        self.assertEqual(self.network.selected,ORIGINAL)
        self.assertEqual(self.network.calls.count(('activate',AP_UUID)),1)
        self.assertFalse(self.session.journal.exists())
        self.assertNotIn(AP_UUID,self.network.profiles)
        result=json.loads((self.directory/'status/status.json').read_text())
        self.assertEqual((result['state'],result['terminal']),('cancelled',True))

    def test_genuine_readiness_failure_is_distinct_from_cancel(self):
        self.session.stop()
        self.network.setup_ready=lambda:(_ for _ in ()).throw(NetworkError('Synthetic service failure'))
        fresh=Session(self.network,self.directory,lambda args:'',lambda:self.now,pause=lambda _:None,
                      runtime=self.directory/'runtime',status_directory=self.directory/'status')
        with self.assertRaises(NetworkError):fresh.start()
        fresh.stop(cancel=False)
        result=json.loads((self.directory/'status/status.json').read_text())
        self.assertEqual((result['state'],result['error'],result['terminal']),('error','NETWORK',True))

    def test_legacy_journal_migrates_owned_ids_and_cleans_even_if_original_unavailable(self):
        record={k:v for k,v in self.session.record.items() if k in ('original','candidate','committed','autoconnect')}
        record['version']=1
        private_write(self.session.journal,json.dumps(record))
        self.network.restore=lambda _:(_ for _ in ()).throw(NetworkError('Synthetic saved network unavailable'))
        self.session.recover()
        self.assertFalse(self.session.journal.exists())
        self.assertNotIn(AP_UUID,self.network.profiles)
        self.assertEqual(self.network.profiles[ORIGINAL],'untouched original')
        result=json.loads((self.directory/'status/status.json').read_text())
        self.assertTrue(result['terminal'])
        self.assertEqual(result['error'],'NETWORK')

    def test_durable_commit_wins_cancel_before_final_save_step(self):
        original=self.network.finish_commit
        def commit(identity):
            self.session.stop_event.set()
            original(identity)
        self.network.finish_commit=commit
        self.join()
        candidate=self.session.record['candidate']
        self.session.stop(cancel=True)
        result=json.loads((self.directory/'status/status.json').read_text())
        self.assertEqual(result['state'],'connected')
        self.assertEqual(self.network.selected,candidate)
        self.assertFalse(self.session.journal.exists())

    def test_invalid_recovery_record_cannot_delete_original(self):
        value = dict(version=1, original=ORIGINAL, candidate=ORIGINAL, committed=False, autoconnect=True)
        private_write(self.session.journal, json.dumps(value))
        with self.assertRaises(NetworkError):
            self.session.recover()
        self.assertIn(ORIGINAL, self.network.profiles)

    def test_ap_delete_failure_still_attempts_restore_and_autoconnect(self):
        actual = self.network.discard
        def discard(identity):
            if identity == AP_UUID:
                raise NetworkError('Synthetic delete failed')
            actual(identity)
        self.network.discard = discard
        with self.assertRaises(NetworkError):
            self.session.stop()
        self.assertEqual(self.network.selected, ORIGINAL)
        self.assertIn(('autoconnect', True), self.network.calls)
        self.assertTrue(self.session.journal.exists())

    def test_client_restore_clears_firewall_even_after_ap_cleanup_failure(self):
        table = [True]
        def command(arguments):
            if arguments == ['nft', 'list', 'tables']:
                return 'table inet family_radio_setup\n' if table[0] else ''
            if arguments[:3] == ['nft', 'delete', 'table']:
                table[0] = False
            return ''
        self.session.run = command
        def failed_ap_cleanup(identity):
            if identity == AP_UUID:
                raise NetworkError('Synthetic AP cleanup failed')
        self.network.deactivate = failed_ap_cleanup
        self.network.discard = failed_ap_cleanup
        with self.assertRaises(NetworkError):
            self.session.stop()
        self.assertEqual(self.network.selected, ORIGINAL)
        self.assertFalse(table[0])
        self.assertIn(('autoconnect', True), self.network.calls)

    def test_unavailable_original_does_not_leave_autoconnect_disabled(self):
        self.network.restore = lambda _: (_ for _ in ()).throw(NetworkError('Synthetic original unavailable'))
        self.session.stop()
        self.assertIn(('autoconnect', True), self.network.calls)
        self.assertNotIn(AP_UUID, self.network.profiles)
        self.assertFalse(self.session.journal.exists())
        status=json.loads((self.directory/'status/status.json').read_text())
        self.assertTrue(status['terminal'])
        self.assertEqual(status['error'], 'NETWORK')

    def test_stop_before_queued_worker_prevents_late_network_mutation(self):
        queued = []
        class Queued:
            def __init__(self, target, args, daemon):
                queued.append((target, args))
            def start(self):
                pass
        with patch('host.wifi_setup.threading.Thread', Queued):
            self.assertTrue(self.session.join(dict(ssid='Synthetic home', security='wpa2', password='synthetic-only')))
        self.session.stop()
        before = list(self.network.calls)
        target, arguments = queued[0]
        target(*arguments)
        self.assertEqual(self.network.calls, before)
        self.assertFalse(self.session.journal.exists())
        self.assertEqual(self.network.selected, ORIGINAL)

    def test_commit_journal_failure_before_replace_rolls_back_candidate(self):
        original = self.session.save
        def save(record=None):
            if record and record['committed']:
                raise OSError('Synthetic failed journal write')
            original(record)
        self.session.save = save
        self.join()
        self.assertEqual(self.session.state, 'failed')
        self.assertFalse(self.session.record['committed'])
        self.assertEqual(list(self.directory.glob('*.saved')), [])
        self.session.stop()

    def test_commit_journal_failure_after_replace_finishes_durable_intent(self):
        from host.wifi_network import private_write as actual
        def write(path, value):
            actual(path, value)
            if path == self.session.journal and json.loads(value)['committed']:
                raise OSError('Synthetic directory fsync failure after replace')
        with patch('host.wifi_setup.private_write', write):
            self.join()
        self.assertEqual(self.session.state, 'persistence_failed')
        committed = json.loads(self.session.journal.read_text())
        self.assertTrue(committed['committed'])
        self.session.stop()
        self.assertEqual(self.network.selected, committed['candidate'])

    def test_dns_and_http_stop_before_ap_deactivation_and_restart_after_failure(self):
        self.network.fail_join = True
        events = []
        self.session.run = lambda args: events.append(tuple(args)) or ''
        deactivate = self.network.deactivate
        def down(identity):
            events.append(('ap-down',))
            deactivate(identity)
        self.network.deactivate = down
        self.join()
        stop = next(i for i, event in enumerate(events) if event[:2] == ('systemctl', 'stop'))
        down = events.index(('ap-down',))
        start = next(i for i, event in enumerate(events) if event[:2] == ('systemctl', 'start'))
        self.assertLess(stop, down)
        self.assertLess(down, start)
        self.session.stop()

    def test_socket_allows_only_fixed_operations_and_never_echoes_password(self):
        path = str(self.directory / 'control.sock')
        import os
        with Control(path, self.session, os.getuid()) as control:
            thread = threading.Thread(target=control.serve_forever, daemon=True)
            thread.start()
            try:
                client = HelperNetwork(path)
                self.assertFalse(client.status()['demo'])
                self.assertEqual(client.status()['version'],3)
                self.assertEqual(client.networks()['version'],3)
                self.assertEqual(client.networks()['networks'][0]['ssid'], 'Synthetic home')
                with self.assertRaises(ValueError):
                    client.request('start', command='arbitrary')
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as malformed:
                    malformed.settimeout(3)
                    malformed.connect(path)
                    malformed.sendall(b'[' * 1000 + b']' * 1000 + b'\n')
                    self.assertIn(b'error', malformed.recv(4096))
                self.assertEqual(client.status()['state'], 'ready')
                self.assertTrue(client.join(dict(ssid='Synthetic home', security='wpa2', password='synthetic-secret')))
            finally:
                control.shutdown()
                thread.join()
        self.session.stop()


class NetworkTests(unittest.TestCase):
    def test_keyfile_escaping_cannot_inject_settings(self):
        value = keyfile(ORIGINAL, ' Home\\;é ', 'wpa2', ' space\\password ')
        self.assertIn('ssid=' + ';'.join(str(b) for b in ' Home\\;é '.encode()) + ';', value)
        self.assertIn('psk=\\sspace\\\\password\\s', value)
        self.assertIn('autoconnect=false', value)
        self.assertIn('proto=rsn;', value)
        self.assertIn('pmf=1', value)
        self.assertNotIn('[wifi-security]', keyfile(ORIGINAL, 'Open', 'open', ''))

    def test_escaped_scan_unsupported_and_5ghz_networks(self):
        network = NetworkManager(lambda args, **kwargs: 'Home\\:west:WPA1 WPA2:2412 MHz\nEnterprise:WPA2 802.1X:2412 MHz\nFast:WPA2:5180 MHz\nGuest:--:2437 MHz\nModern:WPA3:2412 MHz\n')
        self.assertEqual(network.scan(), [dict(ssid='Home:west', security='wpa2'), dict(ssid='Guest', security='open')])
        self.assertEqual(terse('a\\\\b:c'), ['a\\b', 'c'])

    def test_autoconnect_changes_preserve_password_substrings(self):
        from host.wifi_network import autoconnect_keyfile
        password = 'xautoconnect=falsexautoconnect=truex'
        value = keyfile(ORIGINAL, 'Synthetic home', 'wpa2', password, persistent=True)
        for enabled in (False, True):
            value = autoconnect_keyfile(value, enabled)
            self.assertIn('psk=' + password + '\n', value)
            self.assertIn('autoconnect=' + str(enabled).lower() + '\n', value)
        self.assertNotIn('group=ccmp', value)
        self.assertIn('group=ccmp', keyfile(AP_UUID, 'Synthetic setup', 'wpa2', password, ap=True))

    def test_private_write_temporary_is_hidden_and_cleaned_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'synthetic.nmconnection'
            sources = []
            def failed_replace(source, destination):
                sources.append(Path(source))
                raise OSError('Synthetic replace failure')
            with patch('host.wifi_network.os.replace', failed_replace):
                with self.assertRaises(OSError):
                    private_write(target, 'synthetic-secret')
            self.assertTrue(sources[0].name.startswith('.'))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_loaded_candidate_delete_preserves_other_loaded_uuids(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = {ORIGINAL, AP_UUID}
            def runner(args, **kwargs):
                if 'UUID' in args:
                    return '\n'.join(loaded)
                if 'delete' in args:
                    loaded.remove(args[-1])
                return ''
            network = NetworkManager(runner, Path(directory), Path(directory))
            network.discard(AP_UUID)
            self.assertEqual(loaded, {ORIGINAL})

    def test_multiple_ipv4_addresses_from_real_nmcli_format(self):
        def runner(args, **kwargs):
            if 'GENERAL.CON-UUID' in args:
                return ORIGINAL
            if 'IP4.ADDRESS' in args:
                return '169.254.2.1/16 | 10.0.0.5/24\n'
            return ''
        NetworkManager(runner).activate(ORIGINAL)

    def test_activation_requires_exact_uuid_and_non_link_local_address(self):
        def runner(args, **kwargs):
            if 'GENERAL.CON-UUID' in args:
                return ORIGINAL
            if 'IP4.ADDRESS' in args:
                return '169.254.2.1/16\n'
            return ''
        network = NetworkManager(runner)
        with self.assertRaises(NetworkError):
            network.activate(ORIGINAL)
        with self.assertRaises(NetworkError):
            network.activate(str(uuid.uuid4()))

    def test_unloaded_candidate_file_is_removed_without_nm_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            network = NetworkManager(lambda args, **kwargs: calls.append(args) or '', Path(directory), Path(directory))
            private_write(network.path(ORIGINAL, True), 'synthetic')
            network.discard(ORIGINAL)
            self.assertFalse(any('delete' in args for args in calls))
            self.assertFalse(network.path(ORIGINAL, True).exists())


if __name__ == '__main__':
    unittest.main()
