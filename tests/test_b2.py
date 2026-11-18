"""Synthetic B8 queue, recovery, persistence, gestures and real-load regressions."""
import json
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from host.radio import Controller, Mpv, SourceError, title_text
from host.library import import_library, load, atomic


def wav(path, seconds=0.35):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(2); f.setsampwidth(2); f.setframerate(48000)
        f.writeframes(b'\0' * int(seconds * 48000) * 4)


class Player:
    def __init__(self): self.eof = False; self.paused = True; self.paths = []
    def load(self, path):
        self.paths.append(path.name); self.eof = False
        if path.name == 'bad.wav': raise SourceError('synthetic corrupt source')
    def pause(self, value): self.paused = value
    def play(self): self.paused = False; self.eof = False
    def ended(self): return self.eof
    def close(self): self.paused = True


class B8Tests(unittest.TestCase):
    def controller(self, root=None, state=None, player=None):
        songs=[dict(id=str(i), title=f'Synthetic {i}', path=name) for i,name in enumerate(['a.wav','bad.wav','c.wav'])]
        c=Controller(player or Player(), lambda v: None, songs=songs, root=root, state_path=state)
        c.receive('B8\tREADY', 0)
        return c

    def event(self,c,kind,value=0,mode='N',now=1):
        if kind == 'L' and value >= 0 and mode == 'N':
            mode = c._mode()
        if kind == 'P' and mode == 'N' and value == -1:
            value = 0 if c.state in ('PLAYING', 'BUFFERING') else 1
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\t{kind}\t{value}\t{mode}\t{c.browser_token}',now)

    def test_full_volume_range_clamps_and_restores_stopped_at_startup_limit(self):
        applied=[]
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            c=Controller(Player(), applied.append, state_path=state)
            c.receive('B8\tREADY',0)
            self.event(c,'K',20);c.tick(1);self.assertEqual((c.volume,c.ceiling),(100,100))
            self.assertEqual(applied[-1],100)
            self.event(c,'K',20);self.assertEqual(c.volume,100)
            self.assertEqual(c.snapshot().split(b'\t')[4:6],[b'100',b'100'])
            c.save()
            restored=Controller(Player(),applied.append,state_path=state)
            self.assertEqual((restored.volume,restored.state),(60,'STOPPED'))
            self.event(c,'K',-20);self.event(c,'K',-20);self.event(c,'K',-20)
            c.tick(1);self.assertEqual(c.volume,0);self.assertEqual(applied[-1],0)
            capped=Controller(Player(),applied.append,ceiling=80)
            capped.receive('B8\tREADY',0);self.event(capped,'K',20)
            self.assertEqual(capped.volume,80)

    def test_browser_display_selection_timeout_and_mode_race(self):
        c=self.controller()
        self.event(c,'L'); self.event(c,'K',2,'B')
        self.assertEqual(c.cursor,2)
        self.event(c,'P',-1)  # normal release cannot toggle a pending browse
        self.assertEqual(c.state,'STOPPED')
        self.event(c,'P',1,'B')  # select displayed index, not newer cursor
        self.assertEqual((c.current,c.state),(1,'PLAYING'))
        self.event(c,'L')
        for now in (3,5,7,8): self.event(c,'H',now=now)
        c.tick(9.1)
        self.assertEqual(c.browse_until,0)
        old=c.volume; self.event(c,'K',-1,'B',now=9.2)
        self.assertEqual(c.volume,old); self.assertFalse(c.browse_until)

    def test_queue_edges_states_source_recovery_and_no_repeat(self):
        c=self.controller('/synthetic')
        self.event(c,'S',-1); self.assertEqual(c.current,0)
        self.event(c,'S',1); self.assertEqual(c.error,'SOURCE'); self.assertFalse(c.fatal)
        self.event(c,'S',1); self.assertEqual((c.current,c.state,c.error),(2,'STOPPED','-'))
        self.event(c,'P',-1); self.event(c,'S',1)
        self.assertEqual(c.state,'PLAYING')
        c.player.eof=True; c.tick(1.1); self.assertEqual(c.state,'STOPPED')
        self.event(c,'L'); self.event(c,'P',0,'B'); c.player.eof=True; c.tick(1.2)
        self.assertEqual((c.current,c.error),(1,'SOURCE'))
        self.event(c,'S',1); self.event(c,'P',-1)
        self.assertEqual(c.state,'PAUSED')
        self.event(c,'S',-1); self.event(c,'S',-1)
        self.assertEqual((c.current,c.state),(0,'PAUSED'))

    def test_persistence_debounce_restore_corrupt_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as root:
            state=Path(root)/'state.json'; c=self.controller(state=state)
            self.event(c,'K',-5); c.tick(2.9); self.assertFalse(state.exists())
            c.tick(3); self.assertEqual(json.loads(state.read_text())['volume'],50)
            self.event(c,'S',1); c.save()
            restored=self.controller(state=state)
            self.assertEqual((restored.current,restored.volume,restored.state),(1,50,'STOPPED'))
            saved=state.read_bytes()
            with patch('host.library.os.replace',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError): atomic(state,b'bad')
            self.assertEqual(state.read_bytes(),saved)
            state.write_text('{'); self.assertEqual(self.controller(state=state).volume,60)
            state.write_text('{"version":1,"volume":99}'); self.assertEqual(self.controller(state=state).volume,60)

    def test_long_title_suffix_snapshot_bound(self):
        a='Same shared prefix ' * 5
        self.assertNotEqual(title_text(a+'Remaster 1'),title_text(a+'Remaster 2'))
        c=self.controller();c.songs[0]['title']=a+'é – Remaster 2'
        self.assertLessEqual(len(c.snapshot()),1153)
        self.assertTrue(c.snapshot().isascii()); self.assertIn(b'Remaster 2',c.snapshot())

    def test_browser_neighbor_windows_follow_cursor_scope_and_artist_labels(self):
        songs=[dict(id=str(i),title=f'Song {i}',path=f'{i}.wav',artist='Demo Artist' if i%2 else f'Artist {i}') for i in range(6)]
        c=Controller(Player(),lambda v:None,songs=songs);c.receive('B8\tREADY',0)
        self.assertEqual(c.snapshot().decode().rstrip('\n').split('\t')[22:26],['']*4)
        self.event(c,'L',0)
        for cursor in (0,1,3,5):
            self.event(c,'K',cursor-c.cursor,'B')
            fields=c.snapshot().decode().rstrip('\n').split('\t')
            self.assertEqual(fields[14],f'Song {cursor}')
            self.assertEqual(fields[22:26],[f'Song {cursor+delta}' if 0<=cursor+delta<6 else '' for delta in (-2,-1,1,2)])
        self.event(c,'L',1);c.cursor=c.artists.index('Demo Artist')
        fields=c.snapshot().decode().rstrip('\n').split('\t')
        expected=[c.artist_labels[c.artists[c.cursor+delta]] if 0<=c.cursor+delta<len(c.artists) else '' for delta in (-2,-1,1,2)]
        self.assertEqual(fields[22:26],expected)
        self.event(c,'P',c.cursor,'A');self.assertEqual(c.browse_items,[1,3,5])
        self.event(c,'K',1,'B');fields=c.snapshot().decode().rstrip('\n').split('\t')
        self.assertEqual(fields[14],'Song 3');self.assertEqual(fields[22:26],['','Song 1','Song 5',''])
        self.event(c,'L',-1,'B');self.assertEqual(c.snapshot().decode().rstrip('\n').split('\t')[22:26],['']*4)
        before=c.session;c.receive('B4\tREADY',1);self.assertEqual(c.session,before)

    def test_artist_catalog_filters_tokens_casefold_and_exit(self):
        songs=[dict(id=str(i),title=f'Synthetic {i}',path=f'{i}.wav',artist=artist) for i,artist in enumerate(['Demo Artist','Other','DEMO ARTIST','Beyoncé','Beyonce'])]
        c=Controller(Player(),lambda v:None,songs=songs,shuffle_items=lambda items:items.reverse())
        c.receive('B8\tREADY',0)
        self.assertEqual(len(c.artists),4)
        self.assertEqual(c.raw_artist({'metadata':{'ARTIST':'Tagged artist'}}),'Tagged artist')
        self.assertEqual(c.raw_artist({'artist':'Demo Artist','metadata':{'artist':'Other'}}),'Demo Artist')
        self.assertEqual(c.raw_artist({}),'Demo Artist')
        self.assertEqual(c.artist(songs[0]),c.artist(songs[2]))
        self.assertNotEqual(c.artist_labels['Beyoncé'],c.artist_labels['Beyonce'])
        self.event(c,'L',1);self.assertEqual(c.browse_kind,'A')
        artist=c.artist(songs[0]);ordinal=c.artists.index(artist)
        old_token=c.browser_token
        self.event(c,'P',ordinal,'A');self.assertEqual(c.browse_items,[0,2])
        self.assertIsNone(c.scope);self.assertEqual(c.queue,[0,1,2,3,4])  # Browsing does not change playback queue.
        old_seq=c.seq
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\tP\t0\tA\t{old_token}',1)
        self.assertEqual(c.seq,old_seq);self.assertEqual(c.current,0)
        self.event(c,'P',1,'B');self.assertEqual((c.current,c.scope,c.queue),(2,artist,[0,2]))
        self.assertEqual(c.state,'PLAYING')
        self.event(c,'S',-1);self.assertEqual(c.current,0)
        self.event(c,'L',1);token=c.browser_token
        self.event(c,'L',-1,'A');self.assertFalse(c.browse_until)
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\tP\t0\tA\t{token}',1)
        self.assertFalse(c.browse_until);self.assertEqual(c.current,0)
        self.event(c,'L',0);token=c.browser_token
        # Heartbeat remains live while exact browser deadline expires before receive().
        for now in (3,5,7,8):self.event(c,'H',now=now)
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\tK\t1\tB\t{token}',9)
        self.assertFalse(c.browse_until)
        c.open_browser('B',9,'missing artist')
        self.assertEqual(c.browse_items,[])  # Unreachable from validated artist selection; must not select.
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\tP\t0\tB\t{c.browser_token}',9)
        self.assertEqual(c.current,0)

    def test_shuffle_finite_queue_history_idempotence_scope_and_restart(self):
        songs=[dict(id=str(i),title=f'Synthetic {i}',path=f'{i}.wav',artist='Demo Artist') for i in range(4)]
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            c=Controller(Player(),lambda v:None,songs=songs,state_path=state,shuffle_items=lambda items:items.reverse())
            c.receive('B8\tREADY',0)
            self.event(c,'Q',1);self.assertEqual(c.queue,[0,3,2,1]);self.assertEqual(c.state,'STOPPED')
            queue=c.queue.copy();self.event(c,'Q',1);self.assertEqual(c.queue,queue)
            self.event(c,'P',-1);self.event(c,'S',1);self.assertEqual(c.current,3)
            self.event(c,'S',-1);self.assertEqual(c.current,0)
            self.event(c,'S',1);self.assertEqual(c.current,3)
            seen=[0,3]
            while c.queue_position<len(c.queue)-1:
                c.player.eof=True;c.tick(1);seen.append(c.current)
            c.player.eof=True;c.tick(1);self.assertEqual(c.state,'STOPPED')
            self.assertEqual(len(seen),len(set(seen)));self.assertEqual(set(seen),set(range(4)))
            c.scope=c.artist(songs[0]);c.dirty_at=1;c.save()
            restored=Controller(Player(),lambda v:None,songs=songs,state_path=state,shuffle_items=lambda items:items.reverse())
            self.assertTrue(restored.shuffle);self.assertEqual(restored.scope,'Demo Artist');self.assertEqual(restored.state,'STOPPED')
            c.receive('B8\tREADY',1.1);self.assertNotEqual(c.state,'PLAYING')
            self.event(c,'Q',0);self.assertEqual(c.queue,[0,1,2,3]);self.assertEqual(c.queue_position,c.current)
            one=Controller(Player(),lambda v:None,songs=songs[:1]);one.receive('B8\tREADY',0)
            self.event(one,'Q',1);self.event(one,'P',-1);one.player.eof=True;one.tick(1)
            self.assertEqual((one.current,one.state),(0,'STOPPED'))

    def test_real_two_track_eof_and_load_barrier(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); wav(root/'a.wav', 1.2); wav(root/'b.wav',0.8)
            p=Mpv(audio='null')
            songs=[dict(id=str(i),path=n,title=n) for i,n in enumerate(['a.wav','b.wav'])]
            c=Controller(p,lambda v:None,songs=songs,root=root)
            try:
                self.assertFalse(c.fatal,c.error)
                c.receive('B8\tREADY',time.monotonic())
                self.event(c,'P',-1,now=time.monotonic())
                deadline=time.monotonic()+4
                first_end = []
                playback_started = time.monotonic()
                original_load = p.load
                def record_load(path):
                    first_end.append((time.monotonic() - playback_started, p.command('get_property', 'pause')))
                    return original_load(path)
                p.load = record_load
                while c.current==0:
                    self.event(c,'H',now=time.monotonic())
                    c.tick(time.monotonic()); self.assertLess(time.monotonic(),deadline); time.sleep(.02)
                # silence-stream time-pos lags output; elapsed wall time must cover the file.
                self.assertGreaterEqual(first_end[0][0], 1.18)
                self.assertTrue(first_end[0][1])
                self.assertEqual((c.current,c.state),(1,'PLAYING'))
                self.assertFalse(p.ended())
                # Old EOF cannot skip or stop the newly loaded second track.
                c.tick(time.monotonic()); self.assertEqual(c.state,'PLAYING')
                self.assertLess(c.elapsed,.5)
                p.pause(True); c.state='PAUSED'
                self.event(c,'S',-1,now=time.monotonic())
                self.assertTrue(p.command('get_property','pause'))
                (root/'bad.wav').write_bytes(b'not audio')
                with self.assertRaises(SourceError): p.load(root/'bad.wav')
                p.load(root/'b.wav'); self.assertTrue(p.command('get_property','pause'))
                self.assertFalse(p.source_error)
            finally: p.close()

    def test_real_import_all_formats_unicode_fallback_stability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'in';source.mkdir();dest=root/'out'
            wav(source/'Pair.wav'); wav(source/'Pair remastered.wav')
            subprocess.run(['ffmpeg','-v','error','-i',str(source/'Pair.wav'),'-metadata','title=Étoile – Version Ω',str(source/'Pair.mp3')],check=True)
            (source/'Fallback.mp3').write_bytes(b'broken'); wav(source/'Fallback.wav')
            first=import_library(source,dest); second=import_library(source,dest)
            self.assertEqual((first['inputs'],first['songs'],len(first['errors'])),(5,3,1))
            self.assertEqual(first['sha256'],second['sha256'])
            songs=load(dest/'manifest.json')['songs']
            self.assertEqual(songs[0]['source_filename'],'Fallback.wav')
            self.assertEqual(songs[1]['title'],'Étoile – Version Ω')
            self.assertNotEqual(songs[0]['id'],songs[2]['id']) # equal audio, distinct filename
            self.assertEqual(len(list(source.iterdir())),5)
            for mutate in [lambda s:s.update(path='../escape.mp3'),lambda s:s.update(duration=float('nan')),lambda s:s.update(id='bad'),lambda s:s.update(title=None)]:
                data=json.loads((dest/'manifest.json').read_text());mutate(data['songs'][0]);atomic(dest/'invalid.json',json.dumps(data).encode())
                with self.assertRaises(ValueError):load(dest/'invalid.json')

    def test_native_firmware_gestures(self):
        with tempfile.TemporaryDirectory() as root:
            binary=Path(root)/'gestures'
            subprocess.run(['c++','-std=c++11','-Wall','-Wextra','-Werror','-I.','tests/gestures.cpp','-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)
