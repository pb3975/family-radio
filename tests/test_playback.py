"""Observable audio ownership and a synthetic external Soloist executable."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from host.playback import (AudioOwner, Cancelled, LocalItem, Select, SetPaused,
                           SetVolume, SoloistBackend, SpotifyItem, Stop, PipeWireOutput)

TRACK = 'spotify:track:' + 'A' * 22


def output_graph(level, muted, ready=True):
    return [
        {'id': 7, 'info': {'props': {'device.id': '10', 'card.profile.device': '0',
             'api.alsa.pcm.card': '0', 'media.class': 'Audio/Sink', 'node.name': 'synthetic-output'},
             'params': {'Props': [{'channelVolumes': [.064, .064], 'mute': False}]}}},
        {'id': 10, 'info': {'props': {'api.alsa.card': '0'}, 'params': {'Route': [
            {'index': 2, 'device': 0, 'direction': 'Output', 'props': {'channelVolumes': [level, level], 'mute': muted}}
        ] if ready else []}}}]


class FakeBackend:
    def __init__(self, name, records, active, release=None):
        if active:
            raise AssertionError('two backends owned the output')
        active.append(name)
        self.name, self.records, self.active, self.release = name, records, active, release
        self.state, self.position, self.duration, self.eof = 'STOPPED', 0, 10000, False
        self.started = threading.Event()
        self.volume = None
        self.error = None
        records.append(('open', name))

    def start(self, item, position, guard, volume):
        self.records.append(('start', item.key))
        self.started.set()
        while self.release is not None and not self.release.wait(.01):
            if not guard():
                raise Cancelled()
        if not guard():
            raise Cancelled()
        self.volume = volume()
        self.records.append(('audible', item.key, self.volume))
        self.state = 'PLAYING'
        if self.eof:
            self.eof, self.position = False, 0
        if position is not None:
            self.position = position

    def prepare(self, item, guard, volume):
        self.state = 'PAUSED'
        self.position = 0

    def pause(self):
        self.records.append(('pause', self.name))
        self.state = 'PAUSED'

    def set_volume(self, percent):
        self.volume = percent

    def status(self):
        if self.error:
            raise RuntimeError(self.error)
        return self.state, self.position, self.duration, self.eof

    def close(self):
        self.records.append(('close', self.name))
        self.active.remove(self.name)


class AudioOwnerTests(unittest.TestCase):
    def setUp(self):
        self.records, self.active, self.backends = [], [], []

    def owner(self, release=None):
        def factory(name):
            value = FakeBackend(name, self.records, self.active, release)
            self.backends.append(value)
            return value
        owner = AudioOwner(local_factory=lambda: factory('local'),
                           spotify_factory=lambda: factory('spotify'), poll_interval=.005)
        self.addCleanup(owner.close)
        return owner

    def wait(self, predicate, owner=None, timeout=2):
        events = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if owner:
                events.extend(owner.poll())
            if predicate(events):
                return events
            time.sleep(.005)
        self.fail('observable audio result did not arrive: ' + repr(events))

    def test_stopped_spotify_does_not_launch_and_local_starts_stopped(self):
        owner = self.owner()
        owner.submit(Select(1, SpotifyItem('spotify:a', TRACK, 10000), False))
        self.wait(lambda events: any(e.generation == 1 and e.state == 'STOPPED' for e in events), owner)
        self.assertEqual(self.backends, [])
        owner.submit(Select(2, LocalItem('local:a', Path('/synthetic.wav'), 10000), False))
        events = self.wait(lambda events: any(e.generation == 2 and e.state == 'STOPPED' for e in events), owner)
        self.assertFalse(any(e.generation == 2 and e.state == 'PLAYING' for e in events))
        self.assertEqual(self.active, ['local'])

    def test_source_switch_reaps_old_owner_before_opening_next(self):
        owner = self.owner()
        owner.submit(Select(1, LocalItem('local:a', Path('/synthetic.wav'), 10000), True))
        self.wait(lambda _: ('audible', 'local:a', 60) in self.records)
        owner.submit(Select(2, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: ('audible', 'spotify:a', 60) in self.records)
        self.assertLess(self.records.index(('close', 'local')), self.records.index(('open', 'spotify')))
        owner.close()
        self.assertEqual(self.active, [])
        self.assertFalse(owner.thread.is_alive())

    def test_pause_during_start_cancels_unmute_without_blocking_submit(self):
        release = threading.Event()
        owner = self.owner(release)
        owner.submit(Select(4, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends))
        self.assertTrue(self.backends[0].started.wait(1))
        start = time.monotonic()
        owner.submit(SetPaused(4, True))
        self.assertLess(time.monotonic() - start, .1)
        release.set()
        self.wait(lambda events: any(e.generation == 4 and e.state == 'PAUSED' for e in events), owner)
        self.assertFalse(any(row[0] == 'audible' for row in self.records))

    def test_new_selection_and_stop_replace_pending_starts(self):
        release = threading.Event()
        owner = self.owner(release)
        owner.submit(Select(1, SpotifyItem('spotify:old', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends))
        self.assertTrue(self.backends[0].started.wait(1))
        owner.submit(Select(2, SpotifyItem('spotify:new', TRACK, 10000), True))
        owner.submit(Stop(3))
        release.set()
        events = self.wait(lambda events: any(e.generation == 3 and e.state == 'STOPPED' for e in events), owner)
        self.assertFalse(any(row[0] == 'audible' for row in self.records))
        self.assertFalse(any(e.ended for e in events))
        self.assertEqual(self.active, [])

    def test_volume_edits_coalesce_and_gate_start_at_latest_level(self):
        release = threading.Event()
        owner = self.owner(release)
        owner.submit(Select(1, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends))
        self.assertTrue(self.backends[0].started.wait(1))
        for percent in range(50, 4, -1):
            owner.submit(SetVolume(percent))
        release.set()
        self.wait(lambda _: ('audible', 'spotify:a', 5) in self.records)
        self.assertEqual([r[2] for r in self.records if r[0] == 'audible'], [5])
        self.assertEqual([r for r in self.records if r[0] == 'start'], [('start', 'spotify:a')])

    def test_pause_after_stopped_startup_and_completion_stays_stopped(self):
        owner = self.owner()
        owner.submit(Select(1, LocalItem('local:a', Path('/synthetic.wav'), 10000), False))
        self.wait(lambda events: any(e.state == 'STOPPED' for e in events), owner)
        owner.submit(SetPaused(1, True))
        events = self.wait(lambda events: any(e.state == 'STOPPED' for e in events), owner)
        self.assertFalse(any(e.state == 'PAUSED' for e in events))
        owner.submit(SetPaused(1, False))
        self.wait(lambda _: self.backends[0].state == 'PLAYING')
        self.backends[0].position, self.backends[0].eof = 10000, True
        self.wait(lambda events: any(e.ended for e in events), owner)
        owner.submit(SetPaused(1, True))
        events = self.wait(lambda events: any(e.state == 'STOPPED' for e in events), owner)
        self.assertFalse(any(e.state == 'PAUSED' or e.ended for e in events))

    def test_eof_is_retained_once_and_stop_is_not_eof(self):
        owner = self.owner()
        owner.submit(Select(1, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends) and self.backends[0].state == 'PLAYING')
        owner.poll()
        self.backends[0].position, self.backends[0].eof = 10000, True
        events = self.wait(lambda events: any(e.ended for e in events), owner)
        self.assertEqual(sum(e.ended for e in events), 1)
        self.assertFalse(any(e.state == 'PLAYING' for e in events))
        owner.submit(Stop(2))
        self.wait(lambda events: any(e.generation == 2 and e.state == 'STOPPED' for e in events), owner)
        self.assertFalse(any(e.ended for e in owner.poll()))

    def test_volume_after_failure_does_not_retry_or_clear_the_error(self):
        owner = self.owner()
        item = SpotifyItem('spotify:a', TRACK, 10000)
        owner.submit(Select(1, item, True))
        self.wait(lambda _: bool(self.backends) and self.backends[0].state == 'PLAYING')
        self.backends[0].error = 'ACCOUNT'
        self.wait(lambda events: any(e.error == 'ACCOUNT' for e in events), owner)
        owner.submit(SetVolume(10))
        time.sleep(.03)
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(owner.poll(), [])
        owner.submit(SetPaused(1, False))
        self.wait(lambda events: any(e.generation == 1 and e.state == 'PLAYING' for e in events), owner)
        self.assertEqual(len(self.backends), 2)
        self.assertEqual(self.backends[-1].state, 'PLAYING')
        owner.submit(Select(2, item, True))
        self.wait(lambda events: any(e.generation == 2 and e.state == 'PLAYING' for e in events), owner)
        self.assertEqual(len(self.backends), 2)

    def test_backend_cleanup_failure_reports_original_error_and_local_still_plays(self):
        class FailingCleanup(FakeBackend):
            def close(self):
                super().close()
                raise RuntimeError('OUTPUT')
        failed=[]
        def spotify():
            backend=FailingCleanup('spotify',self.records,self.active)
            failed.append(backend)
            return backend
        owner=AudioOwner(spotify_factory=spotify,
                         local_factory=lambda:FakeBackend('local',self.records,self.active),poll_interval=.005)
        self.addCleanup(owner.close)
        with patch('threading.excepthook') as unhandled:
            owner.submit(Select(1,SpotifyItem('spotify:a',TRACK,10000),True))
            self.wait(lambda events:any(event.state=='PLAYING' for event in events),owner)
            failed[0].error='ACCOUNT'
            events=self.wait(lambda events:any(event.error for event in events) or not owner.thread.is_alive(),owner)
            self.assertTrue(owner.thread.is_alive())
            self.assertTrue(any(event.error=='ACCOUNT' for event in events))
            self.assertEqual(self.active,[])
            owner.submit(Select(2,LocalItem('local:recovery',Path('/synthetic.wav'),10000),True))
            self.wait(lambda events:any(event.generation==2 and event.state=='PLAYING' for event in events),owner)
            self.assertEqual(self.active,['local'])
            self.assertFalse(unhandled.called)

    def test_finished_item_can_be_replayed_and_finishes_once_again(self):
        owner = self.owner()
        owner.submit(Select(1, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends) and self.backends[0].state == 'PLAYING')
        backend = self.backends[0]
        backend.position, backend.eof = 10000, True
        self.wait(lambda events: any(event.ended for event in events), owner)
        owner.submit(SetPaused(1, False))
        self.wait(lambda events: any(event.state == 'PLAYING' for event in events), owner)
        self.assertEqual(sum(row[0] == 'audible' for row in self.records), 2)
        backend.position, backend.eof = 10000, True
        events = self.wait(lambda events: any(event.ended for event in events), owner)
        self.assertEqual(sum(event.ended for event in events), 1)
    def test_same_generation_pause_drops_unread_playing_and_suppresses_eof(self):
        owner = self.owner()
        owner.submit(Select(1, SpotifyItem('spotify:a', TRACK, 10000), True))
        self.wait(lambda _: bool(self.backends) and self.backends[0].state == 'PLAYING')
        owner.submit(SetPaused(1, True))
        self.assertFalse(any(event.state == 'PLAYING' for event in owner.poll()))
        self.backends[0].position, self.backends[0].eof = 9999, True
        events = self.wait(lambda events: any(event.state == 'PAUSED' for event in events), owner)
        self.assertFalse(any(event.ended for event in events))


class OutputGainTests(unittest.TestCase):
    def test_start_waits_for_the_route_before_sending_control_commands(self):
        reads, ready, level, muted = [0], [False], [.064], [False]
        class Process:
            returncode = None
            def poll(self): return self.returncode
            def terminate(self): self.returncode = 0
            def kill(self): self.returncode = -9
            def wait(self, timeout=None): return self.returncode
        def launch(output, command, **kwargs):
            if command[0] == 'pipewire':
                (Path(output.temp.name) / 'run/pipewire-0').touch()
            return Process()
        def external(output, command):
            if command[0] == 'pw-dump':
                reads[0] += 1
                ready[0] = reads[0] >= 2
                return json.dumps(output_graph(level[0], muted[0], ready[0]))
            self.assertTrue(ready[0], 'a sink alone is not a ready output control')
            if command[:4] == ['pw-cli', 'set-param', '10', 'Route']:
                value = json.loads(command[-1])
                self.assertEqual((value['index'], value['device']), (2, 0))
                if 'mute' in value['props']:
                    muted[0] = value['props']['mute']
                if 'channelVolumes' in value['props']:
                    level[0] = value['props']['channelVolumes'][0]
            else:
                self.fail(str(command))
            return ''
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return Path('/proc/asound/card0') if str(path) == '/proc/asound/MAX98357A' else original_resolve(path, *args, **kwargs)
        with patch.object(PipeWireOutput, 'launch', launch), patch.object(PipeWireOutput, 'run', external), patch.object(Path, 'resolve', resolve):
            output = PipeWireOutput(lambda: True)
            try:
                self.assertTrue(output.muted)
                self.assertEqual(output._level(), (0, True))
            finally:
                output.close()

    def test_existing_softvol_db_curve_is_preserved_through_linear_route_gain(self):
        output = PipeWireOutput.__new__(PipeWireOutput)
        output.node_id = 7
        output.guard = lambda: True
        actual = [0.0]
        def external(command):
            if command[0] == 'amixer':
                db = '-24.81' if command[-1] == '60%' else '0.00'
                return f'Front Left: [60%] [{db}dB]\nFront Right: [60%] [{db}dB]'
            if command[:4] == ['pw-cli', 'set-param', '10', 'Route']:
                actual[0] = json.loads(command[-1])['props']['channelVolumes'][0]
                return ''
            if command[0] == 'pw-dump':
                return json.dumps(output_graph(actual[0], True))
            raise AssertionError(command)
        output.run = external
        output.set_volume(60)
        self.assertAlmostEqual(actual[0], 10 ** (-24.81 / 20), places=7)
        self.assertLess(actual[0], .06)
        output.set_volume(100)
        self.assertAlmostEqual(actual[0], 1)
        output.set_volume(0)
        self.assertEqual(actual[0], 0)

    def test_volume_and_mute_wait_for_asynchronous_property_acknowledgments(self):
        output = PipeWireOutput.__new__(PipeWireOutput)
        output.node_id, output.guard = 7, lambda: True
        current = [0.064, False]
        pending = [None, None, 0]
        reads = []
        def external(command):
            if command[0] == 'amixer':
                return 'Front Left: [60%] [-24.81dB]\nFront Right: [60%] [-24.81dB]'
            if command[:4] == ['pw-cli', 'set-param', '10', 'Route']:
                props = json.loads(command[-1])['props']
                levels = props.get('channelVolumes')
                pending[:] = [levels[0] if levels else None, props.get('mute'), 2]
                return ''
            if command[0] == 'pw-dump':
                reads.append(tuple(current))
                if pending[2]:
                    pending[2] -= 1
                else:
                    if pending[0] is not None:
                        current[0] = pending[0]
                    if pending[1] is not None:
                        current[1] = pending[1]
                return json.dumps(output_graph(current[0], current[1]))
            raise AssertionError(command)
        output.run = external
        output.mute(True)
        self.assertTrue(current[1])
        self.assertTrue(output.muted)
        output.set_volume(60)
        self.assertAlmostEqual(current[0], 10 ** (-24.81 / 20), places=7)
        self.assertGreaterEqual(len(reads), 6)

    def test_mixer_route_is_authoritative_when_idle_node_props_do_not_change(self):
        output = PipeWireOutput.__new__(PipeWireOutput)
        output.node_id, output.guard = 7, lambda: True
        node_level = {'channelVolumes': [.064, .064], 'mute': False}
        matching = {'channelVolumes': [.064, .064], 'mute': False}
        other_route = {'channelVolumes': [0, 0], 'mute': True}
        objects = [
            {'id': 7, 'info': {'props': {'device.id': '10', 'card.profile.device': '0'},
                              'params': {'Props': [node_level]}}},
            {'id': 10, 'info': {'params': {'Route': [
                {'index': 2, 'device': 0, 'direction': 'Output', 'props': matching},
                {'index': 3, 'device': 1, 'direction': 'Input', 'props': other_route}]}}},
            {'id': 20, 'info': {'params': {'Route': [
                {'index': 2, 'device': 0, 'direction': 'Output', 'props': other_route}]}}}]
        def external(command):
            if command[0] == 'pw-dump':
                return json.dumps(objects)
            if command[:4] == ['pw-cli', 'set-param', '10', 'Route']:
                value = json.loads(command[-1])
                self.assertEqual((value['index'], value['device']), (2, 0))
                matching.update(value['props'])
                return ''
            if command[0] == 'amixer':
                return 'Front Left: [60%] [-24.81dB]\nFront Right: [60%] [-24.81dB]'
            raise AssertionError(command)
        output.run = external
        self.assertEqual(output._level(), (.064, False))
        output.mute(True)
        output.set_volume(60)
        self.assertTrue(output._level()[1])
        self.assertAlmostEqual(output._level()[0], 10 ** (-24.81 / 20), places=7)
        self.assertEqual(node_level, {'channelVolumes': [.064, .064], 'mute': False})
        self.assertEqual(other_route, {'channelVolumes': [0, 0], 'mute': True})


SYNTHETIC_SOLOIST = r'''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
session=Path(args[args.index('--data-dir')+1])
status=session/'synthetic-state.json'
command=session/'synthetic-command.json'
def write(path,value):
 p=path.with_suffix('.'+str(os.getpid())+'.tmp');p.write_text(json.dumps(value));p.replace(path)
if args[0]=='ctl':
 action=args[1]
 if action=='trace':
  previous=''
  while True:
   try:
    value=status.read_text()
    if value!=previous:
     prefix=str(time.time_ns()//1000000)+' ' if os.environ.get('SYNTHETIC_TRACE_PREFIX') else ''
     print(prefix+value,flush=True);previous=value
   except FileNotFoundError:pass
   time.sleep(.01)
 else:
  write(command,{'action':action,'value':int(args[2]) if action=='seek' else None,'at':time.time_ns()})
 sys.exit(0)
uri=args[args.index('--single-track')+1]
command.unlink(missing_ok=True)
duration=int(os.environ.get('SYNTHETIC_DURATION','3000'))
started=last=time.monotonic();position=0;playing=True;seen=None;paused_at=None
session.mkdir(exist_ok=True)
(session/'ws.port').write_text('12345')
while True:
 now=time.monotonic()
 if playing:position+=1000*(now-last)
 last=now
 try:
  event=json.loads(command.read_text())
  if event['at']!=seen:
   seen=event['at']
   if event['action']=='pause':playing=False;paused_at=now
   elif event['action']=='play':playing=True;paused_at=None
   elif event['action']=='seek':position=event['value']
 except FileNotFoundError:pass
 state='playing' if playing else 'paused'
 if position>=duration:state='idle';position=duration
 write(status,{'type':'playback_state','status':state,
  'item':{'uri':uri,'decorations':{'playback':{'duration_ms':duration}}},
  'position':{'position_ms':round(position),'timestamp_ms':time.time()*1000,'speed':int(playing)}})
 if position>=duration:break
 if paused_at and now-paused_at>float(os.environ.get('SYNTHETIC_PAUSE_EXIT','1000')):
  print('single-track: synthetic error key='+args[args.index('--api-key')+1],flush=True);sys.exit(1)
 early=float(os.environ.get('SYNTHETIC_EARLY_EXIT','1000'))
 if now-started>early:sys.exit(int(os.environ.get('SYNTHETIC_EXIT_CODE','0')))
 time.sleep(.01)
'''


class FakeOutput:
    def __init__(self, root, session, env=None, on_volume=None):
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.env = dict(os.environ, **(env or {}))
        self.node_name = 'synthetic-output'
        self.muted, self.level, self.audible = True, 0, []
        self.session, self.on_volume = session, on_volume

    def launch(self, command, stdout=subprocess.DEVNULL):
        return subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.DEVNULL)

    def set_volume(self, percent):
        self.level = percent
        if self.on_volume:
            self.on_volume()

    def mute(self, value):
        self.muted = value
        if not value:
            current = json.loads((self.session / 'synthetic-state.json').read_text())
            self.audible.append((current['position']['position_ms'], self.level))

    def close(self):
        self.temp.cleanup()


class SoloistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / 'session'
        self.session.mkdir(mode=0o700)
        self.binary = self.root / 'synthetic-soloist'
        self.binary.write_text(SYNTHETIC_SOLOIST)
        self.binary.chmod(0o700)
        self.key = self.root / 'synthetic.key'
        self.key.write_text('synthetic-secret-not-for-logs')
        self.key.chmod(0o600)

    def backend(self, env=None, on_volume=None):
        self.output = FakeOutput(self.root, self.session, env, on_volume)
        value = SoloistBackend(self.binary, self.key, self.session,
                               output_factory=lambda _: self.output)
        self.addCleanup(value.close)
        return value

    def test_start_and_seek_stay_muted_until_position_is_verified(self):
        value = self.backend()
        value.start(SpotifyItem('a', TRACK, 3000), 1500, lambda: True, lambda: 35)
        self.assertFalse(self.output.muted)
        self.assertGreaterEqual(self.output.audible[0][0], 1250)
        self.assertEqual(self.output.audible[0][1], 35)
        self.assertEqual(value.status()[0], 'PLAYING')

    def test_real_ctl_trace_timestamp_envelope_can_start_playback(self):
        value = self.backend({'SYNTHETIC_TRACE_PREFIX': '1'})
        value.start(SpotifyItem('a', TRACK, 3000), 0, lambda: True, lambda: 20)
        self.assertEqual(value.status()[0], 'PLAYING')
        self.assertFalse(self.output.muted)

    def test_cancellation_at_final_volume_gate_never_unmutes(self):
        valid = [True]
        value = self.backend(on_volume=lambda: valid.__setitem__(0, False))
        with self.assertRaises(Cancelled):
            value.start(SpotifyItem('a', TRACK, 3000), 0, lambda: valid[0], lambda: 35)
        self.assertTrue(self.output.muted)
        self.assertEqual(self.output.audible, [])

    def test_mute_cleanup_failure_still_reaps_player_and_output(self):
        value=self.backend()
        value.start(SpotifyItem('a',TRACK,3000),0,lambda:True,lambda:20)
        player,trace,directory=value.player,value.trace,Path(self.output.temp.name)
        with patch.object(self.output,'mute',side_effect=RuntimeError('OUTPUT')):
            with self.assertRaisesRegex(RuntimeError,'OUTPUT'):
                value.close()
        self.assertIsNotNone(player.poll())
        self.assertIsNotNone(trace.poll())
        self.assertIsNone(value.player)
        self.assertIsNone(value.output)
        self.assertFalse(directory.exists())

    def test_paused_process_death_retains_position_and_explicit_resume_seeks(self):
        value = self.backend({'SYNTHETIC_PAUSE_EXIT': '.12'})
        item = SpotifyItem('a', TRACK, 3000)
        value.start(item, 1000, lambda: True, lambda: 20)
        value.pause()
        paused = value.status()[1]
        self.assertEqual(value.player.wait(timeout=3), 1)
        self.assertEqual(value.status()[0], 'PAUSED')
        self.assertIsNone(value.player)
        self.assertFalse(value.finished)
        self.assertEqual(value.status()[1], paused)
        fault = (self.root / 'playback-diagnostic.json').read_text()
        self.assertIn('PAUSED_PROCESS_EXIT', fault)
        self.assertNotIn(self.key.read_text(), fault)
        value.start(item, None, lambda: True, lambda: 20)
        self.assertGreaterEqual(self.output.audible[-1][0], paused - 250)
        self.assertEqual(value.status()[0], 'PLAYING')

    def test_interrupted_cold_resume_preserves_position_before_seek_confirmation(self):
        value = self.backend()
        item = SpotifyItem('a', TRACK, 3000)
        value.start(item, 1500, lambda: True, lambda: 20)
        value.pause()
        paused = value.status()[1]
        value._stop_player()
        valid = [True]
        control = value._control
        def cancel_seek(*command):
            control(*command)
            if command[0] == 'seek':
                valid[0] = False
        with patch.object(value, '_control', cancel_seek):
            with self.assertRaises(Cancelled):
                value.start(item, None, lambda: valid[0], lambda: 20)
        value.pause()
        self.assertEqual(value.status()[0:2], ('PAUSED', paused))
        self.assertTrue(self.output.muted)
        self.assertIsNone(value.player)
        value.start(item, None, lambda: True, lambda: 20)
        self.assertGreaterEqual(self.output.audible[-1][0], paused - 50)

    def trace_messages(self, value, messages):
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        class Trace:
            stdout = os.fdopen(read_fd, 'rb')
            def poll(self): return 0
        value.trace = Trace()
        try:
            for message in messages:
                os.write(write_fd, (json.dumps(message) + '\n').encode())
            value._consume()
        finally:
            os.close(write_fd)

    def test_empty_initial_entity_does_not_reject_the_requested_track(self):
        value = self.backend()
        value.output = self.output
        value.item = SpotifyItem('a', TRACK, 3000)
        self.trace_messages(value, [
            {'type': 'playback_state', 'status': 'idle', 'item': {'uri': ''},
             'position': {'position_ms': 0, 'speed': 0}},
            {'type': 'playback_state', 'status': 'playing',
             'item': {'uri': TRACK, 'decorations': {'playback': {'duration_ms': 3000}}},
             'position': {'position_ms': 100, 'speed': 1}}])
        self.assertEqual(value.uri, TRACK)
        self.assertEqual(value.state, 'PLAYING')
        self.assertIsNone(value.terminal_at)

    def test_invalid_trace_shapes_leave_private_diagnostics(self):
        for message in ([], {'type': 'track_changed', 'item': None},
                        {'type': 'track_changed', 'item': {'uri': 12}},
                        {'type': 'track_changed', 'item': {'decorations': []}},
                        {'type': 'track_changed', 'item': {'decorations': {'playback': []}}},
                        {'type': 'position_sync', 'position': None}):
            with self.subTest(message=message):
                value = self.backend()
                value.output = self.output
                with self.assertRaisesRegex(RuntimeError, 'PLAYER'):
                    self.trace_messages(value, [message])
                diagnostic = self.root / 'playback-diagnostic.json'
                self.assertEqual(json.loads(diagnostic.read_text())['reason'], 'INVALID_TRACE_SHAPE')
                self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)

    def test_proactive_pause_detaches_after_thirty_seconds_without_eof(self):
        value = self.backend()
        offset = [0]
        value.now = lambda: time.monotonic() + offset[0]
        item = SpotifyItem('a', TRACK, 3000)
        value.start(item, 1000, lambda: True, lambda: 20)
        value.pause()
        paused = value.status()[1]
        offset[0] = 31
        state, position, _, ended = value.status()
        self.assertEqual((state, position, ended), ('PAUSED', paused, False))
        self.assertIsNone(value.player)
        self.assertTrue(self.output.muted)

    def test_successful_midtrack_exit_is_not_end_of_queue(self):
        value = self.backend({'SYNTHETIC_EARLY_EXIT': '.15'})
        value.start(SpotifyItem('a', TRACK, 3000), 0, lambda: True, lambda: 20)
        self.assertEqual(value.player.wait(timeout=3), 0)
        with self.assertRaisesRegex(RuntimeError, 'PLAYER'):
            value.status()
        self.assertFalse(value.finished)

    def test_natural_track_completion_reports_eof_with_real_duration(self):
        value = self.backend({'SYNTHETIC_DURATION': '350'})
        value.start(SpotifyItem('a', TRACK, 350), 0, lambda: True, lambda: 20)
        self.assertEqual(value.player.wait(timeout=3), 0)
        state, position, duration, ended = value.status()
        self.assertEqual((state, position, duration, ended), ('STOPPED', 350, 350, True))

    def terminal_trace(self, position=42074, paused=False, exit_code=0):
        value = self.backend()
        value.output = self.output
        value.item = SpotifyItem('a', TRACK, 42968)
        value.duration = 42968
        clock = [0.0]
        value.now, value.wall_time = lambda: clock[0], lambda: 0
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        reader = os.fdopen(read_fd, 'rb')
        self.addCleanup(lambda: os.close(write_fd))
        class Exited:
            returncode = exit_code
            stdout = None
            def poll(self):
                return self.returncode
        value.player = Exited()
        value.trace = Exited()
        value.trace.stdout = reader
        def feed(message):
            os.write(write_fd, (json.dumps(message) + '\n').encode())
        item = {'uri': TRACK, 'decorations': {'playback': {'duration_ms': 42968}}}
        feed({'type': 'playback_state', 'status': 'playing', 'item': item,
              'position': {'position_ms': position, 'timestamp_ms': 0, 'speed': 1}})
        value._consume()
        clock[0] = .1
        feed({'type': 'playback_changed', 'status': 'buffering'})
        feed({'type': 'playback_state', 'status': 'buffering', 'item': item,
              'position': {'position_ms': position, 'timestamp_ms': 0, 'speed': 0}})
        value._consume()
        if paused:
            value.pause()
        clock[0] += 1.527
        replacement = {'uri': 'spotify:track:' + 'B' * 22,
                       'decorations': {'playback': {'duration_ms': 68000}}}
        feed({'type': 'track_changed', 'item': replacement})
        feed({'type': 'playback_state', 'status': 'buffering', 'item': replacement,
              'position': {'position_ms': 0, 'timestamp_ms': 0, 'speed': 0}})
        feed({'type': 'position_sync', 'position': {'position_ms': 0, 'timestamp_ms': 0, 'speed': 0}})
        return value

    def test_terminal_buffering_replacement_keeps_requested_duration_and_finishes_once(self):
        value = self.terminal_trace()
        state, position, duration, ended = value.status()
        self.assertEqual((state, position, duration, ended), ('STOPPED', 42968, 42968, True))
        self.assertTrue(self.output.muted)
        self.assertEqual(value.uri, TRACK)

    def test_midtrack_replacement_is_muted_and_not_eof(self):
        value = self.terminal_trace(position=1000)
        with self.assertRaisesRegex(ValueError, 'changed the requested item'):
            value.status()
        self.assertTrue(self.output.muted)
        self.assertFalse(value.finished)

    def test_pause_during_terminal_drain_does_not_finish(self):
        value = self.terminal_trace(paused=True)
        state, position, duration, ended = value.status()
        self.assertEqual((state, position, duration, ended), ('PAUSED', 42074, 42968, False))
        self.assertTrue(self.output.muted)

    def test_failed_exit_after_terminal_change_is_not_eof(self):
        value = self.terminal_trace(exit_code=1)
        with self.assertRaisesRegex(RuntimeError, 'PLAYER'):
            value.status()
        self.assertFalse(value.finished)
        self.assertTrue(self.output.muted)


if __name__ == '__main__':
    unittest.main()
