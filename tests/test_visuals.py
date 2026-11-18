"""Synthetic audio/artwork only. No hardware, private songs or Python packages."""
import array
import hashlib
import json
import math
import os
import pty
import select
import sys
import time
from unittest.mock import patch
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
import zlib
from host.library import import_library
from host.visuals import ART_BYTES, ArtTransfer, Visuals, level_history, prepare, rms_levels
from host.radio import Controller, SerialLink
from tests.test_radio import FakePlayer


class VisualTests(unittest.TestCase):
    def test_rms_stereo_phase_silence_and_history_time(self):
        samples = array.array('h')
        for i in range(1600):
            value = round(16000 * math.sin(i * 2 * math.pi * 400 / 8000))
            samples.extend([value, -value])
        levels = rms_levels(samples.tobytes())
        self.assertEqual(len(levels), 2)
        self.assertTrue(all(v > 150 for v in levels))  # Stereo anti-phase must not cancel.
        self.assertEqual(rms_levels(bytes(6400)), bytes(2))
        self.assertEqual(level_history(bytes(range(100)), 1.29, True), bytes(range(1, 13)))
        for position, playing in [(1.2, False), (float('nan'), True), (-1, True), (30, True)]:
            self.assertEqual(level_history(levels, position, playing), bytes(12))

    def test_real_ffmpeg_prepare_and_optional_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source';source.mkdir(); library=root/'library'; output=root/'visuals'
            audio = array.array('h')
            for i in range(6400):
                value = round(16000*math.sin(i*2*math.pi*400/8000)) if 1600 <= i < 4800 else 0
                audio.extend([value, -value])
            with wave.open(str(source/'Synthetic.wav'), 'wb') as f:
                f.setparams((2,2,8000,0,'NONE','not compressed'));f.writeframes(audio.tobytes())
            import_library(source,library)
            song=json.loads((library/'manifest.json').read_text())['songs'][0]
            (root/'cover.ppm').write_bytes(b'P6\n2 2\n255\n'+b'\xff\0\0'*4)
            (root/'artwork.json').write_text(json.dumps({'covers':[{'song_id':song['id'],'master':'cover.ppm'}]}))
            self.assertEqual(prepare(library/'manifest.json',root/'artwork.json',output),{'prepared':1,'skipped':[]})
            visuals=Visuals(output/'manifest.json');art,levels,key=visuals.get(song)
            self.assertEqual(len(art),ART_BYTES);self.assertEqual(art[:2],b'\xf8\0')
            self.assertEqual(key,f'{zlib.crc32(art):08x}')
            self.assertEqual(levels[:2],bytes(2));self.assertGreater(max(levels[2:6]),150)
            self.assertEqual(levels[-2:],bytes(2))
            self.assertEqual(visuals.get(dict(song,source_sha256='bad')),(b'',b'','-'))
            self.assertEqual(Visuals(root/'missing.json').get(song),(b'',b'','-'))
            entry=json.loads((output/'manifest.json').read_text())['songs'][0]
            (output/entry['art']).write_bytes(b'broken')
            self.assertEqual(Visuals(output/'manifest.json').get(song),(b'',b'','-'))
            # Optional visual corruption must not turn a playable song into SOURCE.
            c=Controller(FakePlayer(),lambda v:None,songs=[song],visuals=Visuals(output/'manifest.json'))
            c.receive('B8\tREADY',0)
            self.assertEqual(c.state,'STOPPED');self.assertEqual(c.snapshot().split(b'\t')[15],b'-')
            (output/'manifest.json').write_text('{')
            self.assertEqual(Visuals(output/'manifest.json').get(song),(b'',b'','-'))

    def test_manifest_escape_checksum_duplicate_and_analysis_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); outside=root/'outside';outside.write_bytes(b'x')
            dest=root/'visuals';dest.mkdir();(dest/'art.rgb').symlink_to(outside)
            song=dict(id='synthetic',source_sha256='a')
            entry=dict(id='synthetic',audio_sha256='a',art='art.rgb',levels='levels',art_sha256='bad',levels_sha256='bad')
            for entries in ([entry],[entry,entry]):
                (dest/'manifest.json').write_text(json.dumps(dict(version=1,size=112,hz=10,songs=entries)))
                self.assertEqual(Visuals(dest/'manifest.json').get(song),(b'',b'','-'))

    def test_stop_wait_transfer_ack_loss_retry_crc_restart_and_identity(self):
        class C:
            session='0123456789abcdef';current=0
            def asset(self):return bytes(ART_BYTES), bytes(10), 'deadbeef'
        c=C();transfer=ArtTransfer();self.assertTrue(transfer.sync(c))
        def ack(offset, session=None):
            transfer.acknowledge(f'B8\tO\t{session or c.session}\t{c.current}\tdeadbeef\t{offset}',c)
        first=transfer.record(c,0);self.assertIsNotNone(first)
        self.assertIsNone(transfer.record(c,.1))
        ack(512,'fedcba9876543210');self.assertEqual(transfer.offset,0)
        self.assertEqual(transfer.record(c,.5),first)  # Lost progress ACK: resend same chunk.
        ack(512);self.assertEqual(transfer.offset,512)
        second=transfer.record(c,.51);self.assertIsNotNone(second)
        ack(512);self.assertEqual(transfer.offset,512)  # Delayed earlier ACK cannot rewind/progress.
        ack(1024);self.assertEqual(transfer.offset,1024)
        now=.52
        while transfer.offset < ART_BYTES:
            record=transfer.record(c,now);self.assertIsNotNone(record)
            f=record.decode().strip().split('\t');self.assertLessEqual(len(record),1153)
            self.assertEqual(int(f[5]),transfer.offset)
            payload=bytes.fromhex(f[6]);self.assertLessEqual(len(payload),512)
            self.assertIsNone(transfer.record(c,now+.001))
            end=transfer.offset+len(payload)
            if end==ART_BYTES:
                transfer.acknowledge(f'B8\tC\t{c.session}\t0\tdeadbeef',c)
                break
            ack(end);now+=.005
        self.assertFalse(transfer.active)
        c.current=1;record=transfer.record(c,now+1);self.assertEqual(record.split(b'\t')[5],b'0')
        for step in range(1,4):transfer.record(c,now+1+step*.5)
        self.assertFalse(transfer.active)  # Initial attempt plus two bounded chunk retries.
        c.current=2;transfer.record(c,now+3)
        transfer.sent_end=ART_BYTES
        ack(0);self.assertEqual((transfer.attempt,transfer.offset),(1,0))
        transfer.sent_end=ART_BYTES;ack(0);self.assertFalse(transfer.active)
        c.session='fedcba9876543210';self.assertEqual(transfer.record(c,now+4).split(b'\t')[5],b'0')

    def test_stop_wait_fits_rx_buffer_through_200ms_redraw_blackout(self):
        class C:
            session='0123456789abcdef';current=0
            def asset(self): return bytes(ART_BYTES), b'', 'deadbeef'
        c=C();transfer=ArtTransfer();transfer.sync(c)
        queued=peak=0;next_state=0;waiting=None;completed=False
        for step in range(400):
            now=step*.005
            if now>=next_state:
                queued+=800;next_state=now+.1  # B8 bounded four-neighbor snapshot remains below 800 bytes.
            else:
                record=transfer.record(c,now)
                if record:
                    self.assertIsNone(waiting)
                    queued+=len(record)
                    fields=record.decode().strip().split('\t')
                    waiting=int(fields[5])+len(bytes.fromhex(fields[6]))
            peak=max(peak,queued)
            if now>=.2:
                queued=max(0,queued-2048)
                if queued==0 and waiting is not None:
                    if waiting==ART_BYTES:
                        transfer.acknowledge(f'B8\tC\t{c.session}\t0\tdeadbeef',c);completed=True
                    else:transfer.acknowledge(f'B8\tO\t{c.session}\t0\tdeadbeef\t{waiting}',c)
                    waiting=None
        self.assertLessEqual(peak,4096)
        self.assertTrue(completed)

    def test_real_host_binds_before_chunks_and_controls_preempt_transfer(self):
        script = """
import os, sys, time
from host.radio import Controller, SerialLink, run
import host.radio as radio
def open_port(path):
 time.sleep(.25)  # Exercise startup slower than the former one-shot 100 ms READY.
 link=SerialLink(path)
 os.write(int(sys.argv[2]),b'1');os.close(int(sys.argv[2]))
 return link
radio.SerialLink=open_port
class Player:
 def pause(self, value): pass
 def play(self): pass
 def ended(self): return False
 def close(self): pass
class Assets:
 def get(self, song): return bytes(25088), bytes([128])*100, 'b31448a0'
songs=[dict(id=str(i),title='Synthetic '+str(i),path='synthetic.wav') for i in range(2)]
run(Controller(Player(),lambda v:None,songs=songs,visuals=Assets()),sys.argv[1])
"""
        master, slave=pty.openpty()
        ready_read, ready_write=os.pipe()
        process=subprocess.Popen([sys.executable,'-c',script,os.ttyname(slave),str(ready_write)],pass_fds=(ready_write,),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        os.close(ready_write)
        buffer=b'';seen=[]
        def until(predicate):
            nonlocal buffer
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if select.select([master],[],[],0.05)[0]:
                    buffer+=os.read(master,4096)
                    while b'\n' in buffer:
                        line,_,buffer=buffer.partition(b'\n');f=line.decode().split('\t');seen.append(f)
                        if predicate(f):return f
            self.fail('B8 state/artwork timed out')
        try:
            self.assertTrue(select.select([ready_read],[],[],5)[0], 'host serial initialization timed out')
            self.assertEqual(os.read(ready_read,1),b'1')
            os.write(master,b'B8\tREADY\n')
            state=until(lambda f:len(f)==29 and f[1]=='S')
            first=until(lambda f:len(f)==7 and f[1]=='A')
            self.assertEqual(first[2],state[2]);self.assertEqual(first[3],state[9]);self.assertEqual(first[5],'0')
            os.write(master,f'B8\tE\t{state[2]}\t1\tK\t1\tN\t{state[20]}\n'.encode())
            changed=until(lambda f:len(f)==29 and f[1]=='S' and f[4]=='62')
            os.write(master,f'B8\tE\t{state[2]}\t2\tS\t1\tN\t{state[20]}\n'.encode())
            next_state=until(lambda f:len(f)==29 and f[1]=='S' and f[9]!=state[9])
            art=until(lambda f:len(f)==7 and f[1]=='A' and f[3]==next_state[9])
            self.assertEqual(art[5],'0')
            os.write(master,b'B8\tREADY\n')
            new_state=until(lambda f:len(f)==29 and f[1]=='S' and f[2]!=state[2])
            art=until(lambda f:len(f)==7 and f[1]=='A' and f[2]==new_state[2])
            self.assertEqual(art[5],'0')
            # For every identity, a binding state must precede artwork.
            bound=set()
            for f in seen:
                if f[1]=='S':bound.add((f[2],f[9],f[15]))
                if f[1]=='A':self.assertIn((f[2],f[3],f[4]),bound)
        finally:
            process.terminate();process.communicate(timeout=3);os.close(master);os.close(slave);os.close(ready_read)

    def test_real_partial_art_write_and_eagain_do_not_mix_records(self):
        master, slave = pty.openpty()
        link = SerialLink(os.ttyname(slave))
        chunk = b'B8\tA\t0123456789abcdef\t0\t12345678\t0\t0011\n'
        state = b'B8\tS\tnew snapshot\n'
        writer = os.write
        calls = 0
        def partial(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1: return writer(fd, data[:5])
            if calls == 2: raise BlockingIOError()
            return writer(fd, data)
        try:
            with patch('host.radio.os.write', side_effect=partial):
                link.send(chunk)
                self.assertEqual(link.pending, chunk[5:])
                link.send(state)
                self.assertEqual(link.pending, chunk[5:])
                link.send(state)
                self.assertEqual(link.pending, b'')
            def exact(size):
                data=b'';deadline=time.monotonic()+1
                while len(data)<size:
                    self.assertLess(time.monotonic(),deadline)
                    if select.select([master],[],[],.05)[0]:data+=os.read(master,size-len(data))
                return data
            self.assertEqual(exact(len(chunk)), chunk)
            link.send(state)
            self.assertEqual(exact(len(state)), state)
        finally:
            link.close(); os.close(master); os.close(slave)

    def test_worst_case_visual_snapshot_stays_inside_existing_bound(self):
        class Assets:
            def get(self, song): return bytes(ART_BYTES), bytes([255])*20, 'ffffffff'
        songs = [dict(id=str(i), title='W'*64, path='synthetic.wav',artist='A'*64) for i in range(20000)]
        c = Controller(FakePlayer(), lambda v:None, songs=songs, visuals=Assets())
        c.receive('B8\tREADY', 0);c.current=19999;c.open_browser('B',0,'A'*64);c.cursor=19997
        c.elapsed=c.duration=999999;c.error='STARTUP';c.browse_until=1;c.volume=100;c.browser_token=2147483647
        data=c.snapshot()
        self.assertLessEqual(len(data),800)
        fields=data.rstrip(b'\n').split(b'\t')
        self.assertEqual(len(fields),29)
        self.assertTrue(all(len(fields[i])==64 for i in [6,14,17,18,22,23,24,25]))

    def test_native_crc_chunk_validation_and_session_binding(self):
        crc=f'{zlib.crc32(bytes(ART_BYTES)):08x}'
        code=r'''
#include "visuals.h"
#include <cassert>
#include <string>
int main(){
 Artwork a; const char* s="0123456789abcdef"; const char* key="CRC";
 a.bind(s,0,key);std::string part(192,'0');
 assert(!a.chunk("fedcba9876543210",0,key,0,part.c_str()));assert(a.used==0);
 assert(!a.chunk(s,1,key,0,part.c_str()));assert(a.used==0);
 assert(!a.chunk(s,0,key,96,part.c_str()));assert(a.used==0);
 assert(!a.chunk(s,0,key,0,"GG"));assert(a.used==0);
 assert(!a.chunk(s,0,key,0,part.c_str()));assert(a.used==96);
 assert(!a.chunk(s,0,key,192,part.c_str()));assert(a.used==96);
 for(size_t i=96;i<Artwork::BYTES;i+=96){size_t n=Artwork::BYTES-i;if(n>96)n=96;std::string bytes(n*2,'0');a.chunk(s,0,key,i,bytes.c_str());}
 assert(a.ready);
 for(size_t i=0;i<Artwork::BYTES;i+=96){size_t n=Artwork::BYTES-i;if(n>96)n=96;std::string bytes(n*2,'0');a.chunk(s,0,key,i,bytes.c_str());}
 assert(a.ready);assert(a.used==Artwork::BYTES);
 a.bind(s,1,key);assert(!a.ready);assert(a.used==0);
 a.bind(s,1,"00000000");
 for(size_t i=0;i<Artwork::BYTES;i+=96){size_t n=Artwork::BYTES-i;if(n>96)n=96;std::string bytes(n*2,'0');a.chunk(s,1,"00000000",i,bytes.c_str());}
 assert(!a.ready);assert(a.used==0);
 a.clear();assert(!a.ready);assert(a.index==-1);
}
'''.replace('CRC',crc)
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'test.cpp';binary=Path(directory)/'test';source.write_text(code)
            subprocess.run(['c++','-std=c++11','-Wall','-Wextra','-Werror','-Ifirmware/family_dial',str(source),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)

class SpotifyArtworkTests(unittest.TestCase):
    def test_artwork_recovers_after_a_transient_fetch_failure(self):
        from concurrent.futures import Future
        from host.visuals import SpotifyVisuals
        clock, calls = [0], []
        art = b'\xf8\x00' * (112 * 112)
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise OSError('synthetic outage')
            return art
        class CompletedExecutor:
            def submit(self, function, argument):
                future = Future()
                try:
                    future.set_result(function(argument))
                except OSError as error:
                    future.set_exception(error)
                return future
            def shutdown(self, **kwargs):
                pass
        with patch('host.visuals.ThreadPoolExecutor', return_value=CompletedExecutor()):
            visuals = SpotifyVisuals(fetch, now=lambda: clock[0])
        song = dict(id='1' * 22, album=dict(image_url='https://i.scdn.co/synthetic'))
        try:
            self.assertEqual(visuals.get(song), (b'', b'', '-'))
            self.assertEqual(visuals.get(song), (b'', b'', '-'))
            for _ in range(5):
                self.assertEqual(visuals.get(song), (b'', b'', '-'))
            self.assertEqual(len(calls), 1)
            clock[0] = 31
            visuals.get(song)
            self.assertEqual(visuals.get(song)[0], art)
            self.assertEqual(len(calls), 2)
        finally:
            visuals.close()

    def test_delayed_cover_never_replaces_the_new_selected_song(self):
        import threading
        from host.visuals import SpotifyVisuals
        release=threading.Event()
        def fetch(url):
            if url.endswith('/first'):release.wait(2)
            return (b'\xf8\x00' if url.endswith('/first') else b'\x00\x1f')*(112*112)
        visuals=SpotifyVisuals(fetch)
        first=dict(id='1'*22,album=dict(image_url='https://i.scdn.co/first'))
        second=dict(id='2'*22,album=dict(image_url='https://i.scdn.co/second'))
        try:
            self.assertEqual(visuals.get(first),(b'',b'','-'))
            self.assertEqual(visuals.get(second),(b'',b'','-'))
            release.set();deadline=time.monotonic()+1
            asset=visuals.get(second)
            while not asset[0] and time.monotonic()<deadline:
                time.sleep(.005);asset=visuals.get(second)
            self.assertEqual(asset[0],b'\x00\x1f'*(112*112));self.assertEqual(asset[1],b'')
        finally:release.set();visuals.close()

    def test_only_bounded_provider_images_are_fetched_without_redirects(self):
        from host.visuals import SpotifyVisuals
        from unittest.mock import MagicMock
        for url in ['file:///tmp/image','http://i.scdn.co/image','https://evil.invalid/image',
                    'https://i.scdn.co.evil/image','https://user@i.scdn.co/image','https://i.scdn.co/image#fragment']:
            with self.subTest(url=url), patch('host.visuals.urllib.request.build_opener') as opener:
                with self.assertRaises(ValueError):SpotifyVisuals._fetch(url)
                opener.assert_not_called()
        response=MagicMock();response.__enter__.return_value=response
        response.headers.get_content_type.return_value='image/png'
        response.read.return_value=b'x'*(2*1024*1024+1)
        opener=MagicMock();opener.open.return_value=response
        with patch('host.visuals.urllib.request.build_opener',return_value=opener),patch('host.visuals.subprocess.run') as decoder:
            with self.assertRaises(ValueError):SpotifyVisuals._fetch('https://i.scdn.co/image/synthetic')
            decoder.assert_not_called()
        response.read.return_value=b'P6\n2 1\n255\n'+b'\xff\0\0'*2
        with patch('host.visuals.urllib.request.build_opener',return_value=opener):
            art=SpotifyVisuals._fetch('https://i.scdn.co/image/synthetic')
        self.assertEqual(len(art),ART_BYTES);self.assertEqual(art[:2],b'\0\0')
        self.assertEqual(art[(56*112+56)*2:(56*112+56)*2+2],b'\xf8\0')
