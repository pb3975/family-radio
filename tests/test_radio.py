import json
import os
from pathlib import Path
import pty
import select
import signal
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from host.radio import Controller, Lines, Mpv, SerialLink, mixer, title_text
from host.playback import Cancelled


class FakePlayer:
    def __init__(self):
        self.paused = True
        self.eof = False
        self.closed = False
        self.plays = 0

    def pause(self, paused):
        self.paused = paused

    def play(self):
        self.paused = False
        self.eof = False
        self.plays += 1

    def ended(self):
        return self.eof

    def close(self):
        self.closed = True
        self.paused = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.player = FakePlayer()
        self.volumes = []
        self.c = Controller(self.player, self.volumes.append, 'Synthetic song', ceiling=40, initial=10)
        self.c.receive('B8\tREADY', 0)

    def event(self, seq, kind='P', value=-1, session=None, now=1):
        if kind == 'P' and value == -1:
            value = 0 if self.c.state in ('PLAYING', 'BUFFERING') else 1
        self.c.receive(f'B8\tE\t{session or self.c.session}\t{seq}\t{kind}\t{value}\tN\t{self.c.browser_token}', now)

    def test_startup_volume_and_bounds(self):
        self.assertTrue(self.player.paused)
        self.assertEqual(self.c.state, 'STOPPED')
        self.assertEqual(self.volumes, [10])
        for i in range(1, 10):
            self.event(i, 'K', 20)
        self.assertEqual(self.c.volume, 40)
        for i in range(10, 20):
            self.event(i, 'K', -20)
        self.assertEqual(self.c.volume, 0)
        self.assertTrue(all(0 <= v <= 40 for v in self.volumes))
        with self.assertRaises(ValueError):
            Controller(self.player, self.volumes.append, '', ceiling=101)

    def test_duplicate_out_of_order_stale_session(self):
        self.event(2)
        self.event(2)
        self.event(1)
        self.event(3, session='0000000000000000')
        self.assertEqual(self.player.plays, 1)
        self.assertEqual(self.c.state, 'PLAYING')
        self.event(3)
        self.assertEqual(self.c.state, 'PAUSED')

    def test_knob_burst_applies_one_mixer_change(self):
        for seq in range(1, 11):
            self.event(seq, 'K', 1)
        self.assertEqual(self.volumes, [10])
        self.c.tick(1)
        self.assertEqual(self.volumes, [10, 30])
        self.c.tick(1.2)
        self.assertEqual(self.volumes, [10, 30])
        self.event(11, 'K', -1)
        self.player.play = lambda: self.assertEqual(self.volumes[-1], 28)
        self.event(12)

    def test_deferred_mixer_failure_stops_playback(self):
        self.event(1)
        self.event(2, 'K', 2)
        self.c.set_volume = unittest.mock.Mock(side_effect=RuntimeError('mixer failed'))
        self.c.tick(1)
        self.assertEqual(self.c.state, 'ERROR')
        self.assertTrue(self.player.closed)
        self.event(3, 'K', 2)
        self.c.tick(1.2)
        self.c.set_volume.assert_called_once()

    def test_malformed_does_not_advance_sequence_or_liveness(self):
        for record in ['garbage', 'B1\tREADY', f'B8\tE\t{self.c.session}\t1\tT\t1',
                       f'B8\tE\t{self.c.session}\t1\tV\t999',
                       f'B8\tE\t{self.c.session}\t-1\tT\t0',
                       f'B8\tE\t{self.c.session}\t2147483648\tT\t0']:
            self.c.receive(record, 2)
        self.assertEqual(self.c.seq, 0)
        self.assertEqual(self.c.last_seen, 0)

    def test_timeout_and_reconnect_never_replay(self):
        old = self.c.session
        self.event(1)
        self.c.tick(4)
        self.assertTrue(self.player.paused)
        self.assertEqual(self.c.session, '')
        self.event(2, session=old)
        self.c.receive('B8\tREADY', 5)
        self.assertNotEqual(old, self.c.session)
        self.event(3, session=old)
        self.assertEqual(self.player.plays, 1)
        self.assertEqual(self.c.state, 'PAUSED')
        self.event(1)
        self.assertEqual(self.player.plays, 2)
        self.c.disconnect()  # Cable EOF follows the same pause path.
        self.assertTrue(self.player.paused)

    def test_heartbeat_and_eof(self):
        self.event(1)
        self.event(2, 'H', 0, now=2)
        self.c.tick(3.5)
        self.assertTrue(self.c.session)
        self.player.eof = True
        self.c.tick(4)
        self.assertEqual(self.c.state, 'STOPPED')
        self.assertTrue(self.player.paused)
        self.event(3)
        self.assertEqual(self.c.state, 'PLAYING')

    def test_player_or_mixer_failure_latches_error(self):
        self.player.play = lambda: (_ for _ in ()).throw(RuntimeError('failed'))
        self.event(1)
        self.assertEqual(self.c.state, 'ERROR')
        self.assertTrue(self.player.closed)
        self.event(2)
        self.c.receive('B8\tREADY', 3)
        self.assertEqual(self.c.state, 'ERROR')
        with patch('host.radio.subprocess.run', side_effect=subprocess.TimeoutExpired('amixer', 1)):
            c = Controller(FakePlayer(), mixer, 'song')
        self.assertEqual(c.error, 'STARTUP')
        c = Controller(None, mixer, 'song')
        self.assertIn(b'ERROR', c.snapshot())

    def test_title_is_bounded_ascii(self):
        self.assertEqual(title_text('a\t\n☃'), 'a')
        self.assertEqual(len(title_text('a' * 1000)), 64)
        self.assertLess(len(self.c.snapshot()), 1153)


class TransportTests(unittest.TestCase):
    def test_versioned_fixtures(self):
        fixture = json.loads(Path('docs/fixtures/protocol-b8.json').read_text())
        c = Controller(FakePlayer(), lambda volume: None, 'Synthetic song')
        c.receive(fixture['ready'].rstrip('\n'), 0)
        c.session = '0123456789abcdef'
        self.assertEqual(c.snapshot().decode(), fixture['snapshot'])
        for record in fixture['events'] + fixture['invalid']:
            c.receive(record.rstrip('\n'), 1)
        self.assertEqual(c.volume, 62)
        self.assertEqual(c.state, 'PLAYING')
        self.assertEqual(c.seq,3)
        for case in fixture['catalog_cases']:
            c.receive(case['record'].rstrip('\n'),1)
            self.assertEqual(c.snapshot().decode().split('\t')[8],case['mode'])
            self.assertEqual(c.shuffle,case['shuffle'])
        self.assertEqual(c.seq, 9)

    def test_oversized_partial_and_non_ascii(self):
        lines = Lines()
        self.assertEqual(lines.feed(b'B8\tREA'), [])
        self.assertEqual(lines.feed(b'DY\n'), ['B8\tREADY'])
        self.assertEqual(lines.feed(b'x' * 200), [])
        self.assertEqual(lines.feed(b'x' * 10000), [])
        self.assertLessEqual(len(lines.buffer), 1152)
        self.assertEqual(lines.feed(b'B8\tREADY\n\xff\nB8\tREADY\n'), ['B8\tREADY'])

    def test_pty_partial_read_write_eof_and_fresh_connection(self):
        master, slave = pty.openpty()
        link = SerialLink(os.ttyname(slave))
        try:
            os.write(master, b'B8\tRE')
            self.assertEqual(link.read(), [])
            os.write(master, b'ADY\n')
            self.assertEqual(link.read(), ['B8\tREADY'])
            with patch.object(link.selector, 'select', return_value=[(None, None)]), \
                    patch('host.radio.os.read', side_effect=BlockingIOError):
                self.assertEqual(link.read(), [])
            link.send(b'hello\n')
            self.assertEqual(os.read(master, 64), b'hello\n')
            with patch('host.radio.os.write', side_effect=BlockingIOError):
                link.send(b'one\n')
            link.send(b'two\n')
            self.assertEqual(os.read(master, 64), b'one\n')
            self.assertEqual(link.pending, b'')
            os.close(master)
            master = None
            with self.assertRaises(OSError):
                link.read()
        finally:
            link.close()
            os.close(slave)
            if master is not None:
                os.close(master)
        # New open drops old bytes; no physical serial ports are used.
        master, slave = pty.openpty()
        os.write(master, b'stale\n')
        link = SerialLink(os.ttyname(slave))
        try:
            self.assertEqual(link.read(), [])
        finally:
            link.close()
            os.close(master)
            os.close(slave)

    def test_host_loop_cable_reconnect_and_sigterm(self):
        # A real child host loop and two PTYs, with no audio or serial hardware.
        script = """
import signal
from host.radio import Controller, run, stop_on_signal
class Player:
    def pause(self, paused): pass
    def play(self): pass
    def ended(self): return False
    def close(self): print('CLOSED', flush=True)
signal.signal(signal.SIGTERM, stop_on_signal)
import sys
try:
    run(Controller(Player(), lambda v: None, 'Synthetic song'), sys.argv[1])
except KeyboardInterrupt:
    pass
"""
        master, slave = pty.openpty()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dial'
            path.symlink_to(os.ttyname(slave))
            process = subprocess.Popen([sys.executable, '-c', script, str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def snapshot(predicate, ready=False):
                buffer = b''
                deadline = time.monotonic() + 5
                next_ready = 0
                while time.monotonic() < deadline:
                    if ready and time.monotonic() >= next_ready:
                        os.write(master, b'B8\tREADY\n')
                        next_ready = time.monotonic() + 0.3
                    if select.select([master], [], [], 0.1)[0]:
                        buffer += os.read(master, 1024)
                        while b'\n' in buffer:
                            line, _, buffer = buffer.partition(b'\n')
                            fields = line.decode().split('\t')
                            if len(fields) == 29 and fields[:2] == ['B8', 'S'] and predicate(fields):
                                return fields
                self.fail('host snapshot timed out')
            try:
                fields = snapshot(lambda f: f[3] == 'STOPPED', ready=True)
                old = fields[2]
                os.write(master, f'B8\tE\t{old}\t1\tP\t1\tN\t{fields[20]}\n'.encode())
                snapshot(lambda f: f[3] == 'PLAYING')
                os.write(master, f'B8\tE\t{old}\t1\tP\t1\tN\t{fields[20]}\n'.encode())
                snapshot(lambda f: f[3] == 'PLAYING')
                os.close(master)
                os.close(slave)
                master, slave = pty.openpty()
                path.unlink()
                path.symlink_to(os.ttyname(slave))
                fields = snapshot(lambda f: f[3] == 'PAUSED', ready=True)
                self.assertNotEqual(fields[2], old)
                os.write(master, f'B8\tE\t{old}\t2\tP\t1\tN\t{fields[20]}\n'.encode())
                snapshot(lambda f: f[3] == 'PAUSED')
                os.write(master, f'B8\tE\t{fields[2]}\t1\tP\t1\tN\t{fields[20]}\n'.encode())
                snapshot(lambda f: f[3] == 'PLAYING')
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertIn(b'CLOSED', stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
                os.close(master)
                os.close(slave)

    def test_mpv_missing_file_and_failed_process(self):
        with self.assertRaises(ValueError):
            Mpv('/does-not-exist')
        with tempfile.NamedTemporaryFile(suffix='.m3u') as playlist:
            with self.assertRaisesRegex(ValueError, 'MP3 or WAV'):
                Mpv(playlist.name)
        with tempfile.NamedTemporaryFile(suffix='.wav') as song:
            with self.assertRaises(FileNotFoundError):
                Mpv(song.name, executable='/no-such-player')
            with self.assertRaises(RuntimeError):
                Mpv(song.name, executable='/bin/false')

    def test_mpv_startup_allows_slow_boot_and_bounds_failure(self):
        for ready in (True, False):
            clock = [0]
            attempts = iter((4, 9, 9.5 if ready else 10))
            def connect(_):
                clock[0] = next(attempts)
                if clock[0] == 9:
                    raise ConnectionRefusedError()
                if clock[0] == 4 or not ready:
                    raise FileNotFoundError()
            with self.subTest(ready=ready), \
                    patch('host.radio.subprocess.Popen') as launch, \
                    patch('host.radio.socket.socket') as socket_factory, \
                    patch('host.radio.time.monotonic', side_effect=lambda: clock[0]), \
                    patch.object(Mpv, 'command', return_value=True) as command, \
                    patch('host.radio.time.sleep'):
                launch.return_value.poll.return_value = None
                candidate = socket_factory.return_value
                candidate.connect.side_effect = connect
                if ready:
                    player = Mpv()
                    try:
                        self.assertIs(player.sock, candidate)
                        self.assertEqual(candidate.connect.call_count, 3)
                        command.assert_called_once_with('get_property', 'pause', timeout=0.5,
                                                        cancelled=player.cancelled)
                        launch.return_value.terminate.assert_not_called()
                    finally:
                        player.close()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'mpv startup timed out'):
                        Mpv()
                    self.assertEqual(candidate.connect.call_count, 3)
                    launch.return_value.terminate.assert_called_once()
                    launch.return_value.wait.assert_called_once_with(timeout=1)
                ipc_arg = next(arg for arg in launch.call_args.args[0]
                               if arg.startswith('--input-ipc-server='))
                self.assertFalse(Path(ipc_arg.split('=', 1)[1]).parent.exists())

    def test_mpv_connected_socket_waits_for_readiness_and_reaps_on_failure(self):
        for outcome in ('ready', 'timeout', 'cancelled'):
            clock, receive_timeout = [0.0], [0.5]
            def recv(_):
                clock[0] += receive_timeout[0]
                if outcome == 'ready' and clock[0] >= 1.2:
                    return b'{"request_id":1,"error":"success","data":true}\n'
                raise TimeoutError()
            with self.subTest(outcome=outcome), \
                    patch('host.playback.subprocess.Popen') as launch, \
                    patch('host.playback.socket.socket') as socket_factory, \
                    patch('host.playback.time.monotonic', side_effect=lambda: clock[0]):
                launch.return_value.poll.return_value = None
                candidate = socket_factory.return_value
                candidate.settimeout.side_effect = lambda value: receive_timeout.__setitem__(0, value)
                candidate.recv.side_effect = recv
                cancelled = lambda: outcome == 'cancelled' and clock[0] >= 0.3
                if outcome == 'ready':
                    player = Mpv(cancelled=cancelled)
                    self.assertGreaterEqual(clock[0], 1.2)
                    launch.return_value.terminate.assert_not_called()
                    player.close()
                else:
                    expected = Cancelled if outcome == 'cancelled' else RuntimeError
                    with self.assertRaises(expected):
                        Mpv(cancelled=cancelled)
                    self.assertAlmostEqual(clock[0], 0.3 if outcome == 'cancelled' else 10)
                candidate.connect.assert_called_once()
                launch.return_value.terminate.assert_called_once()
                launch.return_value.wait.assert_called_once_with(timeout=1)
                candidate.close.assert_called_once()
                ipc_arg = next(arg for arg in launch.call_args.args[0]
                               if arg.startswith('--input-ipc-server='))
                self.assertFalse(Path(ipc_arg.split('=', 1)[1]).parent.exists())

    def test_mpv_command_receives_after_interim_timeout_without_stale_cancellation(self):
        player = Mpv.__new__(Mpv)
        player.process = unittest.mock.Mock()
        player.process.poll.return_value = None
        player.sock = unittest.mock.Mock()
        player.request_id = 0
        player.buffer = bytearray()
        player.cancelled = lambda: True
        clock = [0.0]
        def recv(_):
            clock[0] += 0.1
            if clock[0] < 0.2:
                raise TimeoutError()
            return b'{"request_id":1,"error":"success","data":true}\n'
        player.sock.recv.side_effect = recv
        with patch('host.playback.time.monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(player.command('get_property', 'pause'))
        self.assertEqual(player.sock.recv.call_count, 2)
        self.assertLess(clock[0], 0.7)
        clock[0] = 0
        def unresponsive(_):
            clock[0] += player.sock.settimeout.call_args.args[0]
            raise TimeoutError()
        player.sock.recv.side_effect = unresponsive
        with patch('host.playback.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, '^mpv IPC timeout: loadfile$'):
                player.command('loadfile', '/synthetic/private-path.wav', 'replace')
        self.assertAlmostEqual(clock[0], 0.7)
        player.sock.reset_mock()
        player.sock.sendall.side_effect = TimeoutError()
        with patch('host.playback.time.monotonic', return_value=0):
            with self.assertRaisesRegex(RuntimeError, '^mpv IPC timeout: loadfile$'):
                player.command('loadfile', '/synthetic/private-path.wav', 'replace')
        player.sock.settimeout.assert_called_once_with(0.7)
        player.sock.recv.assert_not_called()

    def test_ipc_fake_process_bounded_and_error(self):
        player = Mpv.__new__(Mpv)
        player.process = unittest.mock.Mock()
        player.process.poll.return_value = None
        player.sock = unittest.mock.Mock()
        player.request_id = 0
        player.buffer = bytearray()
        player.sock.recv.return_value = b'{"request_id":1,"error":"failure"}\n'
        with self.assertRaises(RuntimeError):
            player.command('get_property', 'pause')
        player.buffer.clear()
        player.sock.recv.return_value = b'{"event":"end-file","reason":"error"}\n'
        player.sock.recv.return_value += b'{"request_id":2,"error":"success","data":true}\n'
        player.command('get_property', 'pause')
        self.assertTrue(player.source_error)
        player.buffer.clear()
        player.sock.recv.return_value = b'x' * 4096
        with self.assertRaisesRegex(RuntimeError, 'limit'):
            player.command('get_property', 'pause')
        player.buffer.clear()
        player.sock.recv.return_value = b''
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            player.command('get_property', 'pause')
        player.process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            player.command('get_property', 'pause')


class PlaybackTests(unittest.TestCase):
    def test_paused_silent_stream_does_not_advance_song(self):
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / 'synthetic.wav'
            seconds = 1.2
            with wave.open(str(song), 'wb') as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b'\0' * int(48000 * 4 * seconds))
            player = Mpv(audio='null')
            try:
                # Observe actual source samples, not time-pos's output-delay
                # estimate. This test-only filter passes the audio unchanged.
                player.command('af', 'add', '@pause_probe:lavfi=[astats=metadata=1:reset=0:measure_perchannel=none:measure_overall=Number_of_samples]')
                player.load(song)
                def samples():
                    metadata = player.command('get_property', 'af-metadata/pause_probe')
                    return int(float(metadata['lavfi.astats.Overall.Number_of_samples'])) if metadata else 0
                initial_samples = samples()
                player.play()
                deadline = time.monotonic() + 3
                while samples() <= initial_samples or (player.command('get_property', 'time-pos') or 0) < 0.1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                player.pause(True)
                self.assertTrue(player.command('get_property', 'pause'))
                paused_samples = samples()
                self.assertGreater(paused_samples, initial_samples)
                # More than the whole clip duration rules out a full queue
                # hiding continued consumption. No sample may pass the filter.
                paused_until = time.monotonic() + seconds
                while True:
                    self.assertTrue(player.command('get_property', 'pause'))
                    self.assertEqual(samples(), paused_samples)
                    self.assertFalse(player.ended())
                    if time.monotonic() >= paused_until:
                        break
                    time.sleep(0.02)
                position = player.command('get_property', 'time-pos')
                player.pause(False)
                self.assertFalse(player.command('get_property', 'pause'))
                deadline = time.monotonic() + 3
                while samples() <= paused_samples or player.command('get_property', 'time-pos') <= position:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                while not player.ended():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                self.assertEqual(samples(), int(seconds * 48000))
            finally:
                player.close()

    def test_real_mpv_synthetic_wav_and_mp3_null_output(self):
        # Generated in a temporary directory, no private inputs or sound devices.
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / 'synthetic.wav'
            with wave.open(str(wav), 'wb') as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b'\0' * 48000 * 4)
            mp3 = Path(directory) / 'synthetic.mp3'
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(wav), str(mp3)], check=True, timeout=10)
            for song in (wav, mp3):
                with self.subTest(format=song.suffix):
                    player = Mpv(song, audio='null')
                    try:
                        self.assertTrue(player.command('get_property', 'pause'))
                        player.play()
                        self.assertFalse(player.command('get_property', 'pause'))
                        player.pause(True)
                        self.assertTrue(player.command('get_property', 'pause'))
                        player.command('seek', 100, 'absolute')
                        player.pause(False)
                        deadline = time.monotonic() + 3
                        while not player.ended() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(player.ended())
                        player.play()
                        self.assertFalse(player.command('get_property', 'pause'))
                    finally:
                        player.close()


if __name__ == '__main__':
    unittest.main()
