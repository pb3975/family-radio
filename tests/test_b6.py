"""Synthetic catalog and actor integration; no account or hardware required."""
from pathlib import Path
import tempfile
import time
import unittest

from host.catalogs import allowed, Catalogs
from host.approvals import Store
from host.playback import AudioEvent, AudioOwner, Select, SetPaused, SetVolume, Stop, SpotifyItem
from host.radio import Controller

ARTIST='A'*22
PLAYLIST='P'*22


def fact(identity, artists=None):
    return dict(id=identity, uri='spotify:track:'+identity, title='Synthetic '+identity[0],
                artists=artists or [dict(id=ARTIST,name='Approved artist')], duration_ms=100000,
                explicit=False, album=dict(id='Z'*22,name='Synthetic album',image_url=None,url='https://open.spotify.com/album/'+'Z'*22),
                url='https://open.spotify.com/track/'+identity)


def catalog():
    tracks={identity:fact(identity) for identity in ['1'*22,'2'*22,'3'*22]}
    return dict(version=1,approvals_revision=2,generated_at=1000,
                groups=[dict(kind='playlist',id=PLAYLIST,name='Approved playlist',track_ids=['1'*22,'2'*22],verified_at=1000),
                        dict(kind='artist',id=ARTIST,name='Approved artist',track_ids=['1'*22,'3'*22],verified_at=1000)],tracks=tracks)


class Audio:
    def __init__(self):self.commands=[];self.events=[]
    def submit(self,command):self.commands.append(command)
    def poll(self):events,self.events=self.events,[];return events
    def close(self):pass


class Catalog:
    def __init__(self):
        self.catalog=catalog();self.approvals=dict(version=1,revision=2,approvals=[dict(provider='spotify',kind='artist',id=ARTIST),dict(provider='spotify',kind='playlist',id=PLAYLIST)])
        self.serial=1;self.status='READY';self.error=None;self.future=False;self.now=1000;self.readable=True;self.group_status={}
    def tick(self,**kwargs):return self.serial
    def pending_for(self,identity):return self.future
    def permitted(self,identity,start=True):return allowed(self.catalog['tracks'].get(identity),self.approvals,self.catalog,self.now,fresh=start)


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.audio=Audio();self.catalog=Catalog()
        self.c=Controller(self.audio,lambda value:None,songs=[dict(id='local',title='Local song',path='local.wav',artist='Demo Artist')],root='/synthetic',catalogs=self.catalog)
        self.c.receive('B8\tREADY',0)
    def event(self,kind,value,token=None,mode=None,now=1):
        c=self.c
        c.receive(f'B8\tE\t{c.session}\t{c.seq+1}\t{kind}\t{value}\t{mode or c._mode()}\t{token if token is not None else c.browser_token}',now)
    def spotify(self):
        self.event('L',3);self.assertEqual(self.c._mode(),'R')
        self.event('P',1);self.assertEqual((self.c.source,self.c.state,self.c._mode()),('S','STOPPED','P'))
    def start(self):
        self.spotify();self.event('P',0);self.assertEqual(self.c.scope,None)
        self.event('P',0);self.assertEqual(self.c.state,'BUFFERING')
        self.audio.events.append(AudioEvent(self.c.item_token,'PLAYING',1000,100000));self.c.tick(1.1)
        self.assertEqual(self.c.state,'PLAYING')

    def test_sources_playlist_scope_artist_grants_and_no_source_autoplay(self):
        self.spotify()
        selections=[command for command in self.audio.commands if isinstance(command,Select) and isinstance(command.item,SpotifyItem)]
        self.assertTrue(selections);self.assertTrue(all(not command.playing for command in selections))
        self.assertEqual(self.c.artists,[ARTIST]);self.assertEqual(len(self.c.songs),3)
        self.event('P',0);self.assertEqual(self.c._mode(),'B');self.assertEqual(len(self.c.browse_items),2)
        self.event('P',0);self.assertEqual(self.c.scope,('playlist',PLAYLIST));self.assertEqual(len(self.c.queue),2)
        self.assertTrue(isinstance(self.audio.commands[-1],Select));self.assertTrue(self.audio.commands[-1].playing)
        fields=self.c.snapshot().decode().rstrip('\n').split('\t')
        self.assertEqual(len(fields),29);self.assertEqual(fields[26:28],[ 'S','READY']);self.assertEqual(fields[16],'0'*24)
        self.event('L',3);self.event('P',0)
        self.assertEqual((self.c.source,self.c.state),('L','STOPPED'))

    def test_explicit_pause_is_idempotent_and_stale_start_never_resumes(self):
        self.start();self.event('P',0);self.assertEqual(self.c.state,'PAUSED')
        self.event('P',0);self.assertEqual(self.c.state,'PAUSED')
        self.audio.events.append(AudioEvent(self.c.item_token,'PLAYING',2000,100000));self.c.tick(1.2)
        self.assertEqual(self.c.state,'PAUSED');self.assertIsInstance(self.audio.commands[-1],SetPaused)
        self.assertTrue(self.audio.commands[-1].paused)

    def test_stale_normal_play_skip_rejected_but_volume_shuffle_are_explicit(self):
        self.start();old=self.c.browser_token;self.event('S',1)
        self.assertNotEqual(old,self.c.browser_token)
        seq=self.c.seq;self.event('P',1,token=old);self.assertEqual(self.c.seq,seq)
        self.event('K',-1,token=old);self.c.tick(1.2);self.assertEqual(self.c.volume,58)
        self.event('Q',1,token=old);self.assertTrue(self.c.shuffle)

    def test_removing_all_grants_stops_current_and_empty_remains_navigable(self):
        self.start();old=self.c.item_token
        self.catalog.approvals['approvals']=[];self.catalog.serial+=1;self.catalog.status='EMPTY'
        self.c.tick(1.2)
        self.assertEqual((self.c.current,self.c.state),(-1,'STOPPED'));self.assertGreater(self.c.item_token,old)
        self.assertTrue(any(isinstance(command,Stop) and command.generation==self.c.item_token for command in self.audio.commands))
        self.event('L',2);fields=self.c.snapshot().decode().rstrip('\n').split('\t')
        self.assertEqual((fields[9],fields[10],fields[11],fields[21]),('0','0','0','0'))
        self.assertEqual(fields[14],'No playlists');self.assertEqual(fields[22:26],['']*4)
        seq=self.c.seq;self.event('P',0);self.assertEqual(self.c.seq,seq)
        self.event('L',-1);self.event('L',3);self.event('P',0);self.assertEqual(self.c.source,'L')

    def test_union_survives_playlist_removal_and_stale_metadata_does_not_rotate_normal_token(self):
        self.start();token=self.c.browser_token
        self.catalog.serial+=1;self.c.tick(1.2)
        self.assertEqual(self.c.browser_token,token)
        self.catalog.approvals['approvals']=[a for a in self.catalog.approvals['approvals'] if a['kind']=='artist'];self.catalog.serial+=1
        self.c.tick(1.3);self.assertEqual(self.c.state,'PLAYING')
        self.assertTrue(self.catalog.permitted('1'*22))

    def test_expired_membership_blocks_next_start_without_interrupting_current(self):
        self.start()
        # Track two is not covered by the artist grant.
        self.event('S',1);self.audio.events.append(AudioEvent(self.c.item_token,'PLAYING',1000,100000));self.c.tick(1.2)
        self.catalog.catalog['tracks']['2'*22]['artists']=[dict(id='B'*22,name='Other artist')]
        self.catalog.now=1061;self.c.tick(1.3);self.assertEqual(self.c.state,'PLAYING')
        self.event('P',0);self.event('P',1);self.c.tick(1.4)
        self.assertNotEqual(self.c.state,'PLAYING')

    def test_disconnect_cancels_pending_catalog_start_and_new_session_stays_paused(self):
        self.spotify();self.catalog.now=1061
        self.catalog.catalog['tracks']['1'*22]['artists']=[dict(id='B'*22,name='Other artist')]
        self.catalog.future=True;self.catalog.status='LOADING'
        self.event('P',0);self.event('P',0);self.assertEqual(self.c.state,'BUFFERING')
        self.c.disconnect();self.assertIsNone(self.c.pending_start);self.assertEqual(self.c.state,'PAUSED')
        self.catalog.now=1001;self.catalog.serial+=1
        self.c.receive('B8\tREADY',2);self.c.tick(2.1);self.assertNotEqual(self.c.state,'PLAYING')

    def test_paused_or_disconnected_deferred_selection_can_play_after_refresh(self):
        for cancel in ('pause','disconnect'):
            with self.subTest(cancel=cancel):
                started=[]
                class Backend:
                    state='STOPPED';position=0;duration=100000
                    def start(self,item,position,guard,volume):
                        self.state='PLAYING';self.position=position or 0;started.append(item.uri)
                    def prepare(self,item,guard,volume):self.state='STOPPED'
                    def pause(self):self.state='PAUSED'
                    def set_volume(self,value):pass
                    def status(self):return self.state,self.position,self.duration,False
                    def close(self):self.state='STOPPED'
                owner=AudioOwner(spotify_factory=Backend,poll_interval=.005)
                try:
                    data=Catalog();data.now=1061;data.future=True;data.status='LOADING'
                    data.catalog['tracks']['1'*22]['artists']=[dict(id='B'*22,name='Other artist')]
                    c=Controller(owner,lambda value:None,songs=[],catalogs=data)
                    self.c=c;c.receive('B8\tREADY',0)
                    self.event('L',3);self.event('P',1);self.event('P',0);self.event('P',0)
                    self.assertEqual(c.state,'BUFFERING')
                    if cancel=='pause':self.event('P',0)
                    else:c.disconnect();c.receive('B8\tREADY',1.1)
                    self.assertIsNone(c.pending_start)
                    data.catalog['groups'][0]['verified_at']=1061
                    data.future=False;data.status='READY';data.serial+=1;c.tick(1.2)
                    self.assertEqual(started,[])
                    self.event('P',1,now=1.3)
                    deadline=time.monotonic()+1
                    while c.state!='PLAYING' and time.monotonic()<deadline:
                        c.tick(1.4);time.sleep(.005)
                    self.assertEqual(c.state,'PLAYING')
                    self.assertEqual(started,['spotify:track:'+'1'*22])
                finally:owner.close()

    def test_replacing_a_deferred_selection_accepts_progress_and_completion(self):
        for action in ('skip','select'):
            with self.subTest(action=action):
                audio=Audio();data=Catalog()
                data.now=1061;data.future=True;data.status='LOADING'
                data.catalog['tracks']['1'*22]['artists']=[dict(id='B'*22,name='Other artist')]
                c=Controller(audio,lambda value:None,songs=[],catalogs=data)
                self.c=c;c.receive('B8\tREADY',0)
                self.spotify();self.event('P',0);self.event('P',0)
                self.assertIsNotNone(c.pending_start)
                if action=='skip':self.event('S',1)
                else:self.event('L',0);self.event('P',2)
                audio.events.append(AudioEvent(c.item_token,'PLAYING',2000,100000));c.tick(1.1)
                self.assertEqual(c.state,'PLAYING')
                self.assertIsNone(c.pending_start)
                audio.events.append(AudioEvent(c.item_token,'STOPPED',100000,100000,ended=True));c.tick(1.2)
                self.assertEqual(c.state,'STOPPED')

    def test_permission_refresh_resumes_retained_position_without_a_new_selection(self):
        self.start()
        self.event('S',1)
        identity=self.c.songs[self.c.current]['id']
        self.catalog.catalog['tracks'][identity]['artists']=[dict(id='B'*22,name='Other artist')]
        self.audio.events.append(AudioEvent(self.c.item_token,'PLAYING',42000,100000));self.c.tick(1.2)
        self.event('P',0)
        self.catalog.now=1061;self.catalog.future=True;self.catalog.status='LOADING'
        selections=sum(isinstance(command,Select) for command in self.audio.commands)
        token=self.c.item_token
        self.event('P',1)
        self.assertEqual(self.c.state,'BUFFERING')
        self.catalog.catalog['groups'][0]['verified_at']=1061
        self.catalog.future=False;self.catalog.status='READY';self.catalog.serial+=1
        self.c.tick(1.3)
        self.assertEqual(self.c.item_token,token)
        self.assertEqual(sum(isinstance(command,Select) for command in self.audio.commands),selections)
        self.assertEqual(self.audio.commands[-1],SetPaused(token,False))
        self.assertEqual(self.c.elapsed,42)

    def test_refresh_keeps_browser_selection_by_identity_when_order_changes(self):
        self.spotify();self.event('P',0);self.event('K',1)
        selected=self.c.songs[self.c.browse_items[self.c.cursor]]['id'];token=self.c.browser_token
        self.catalog.catalog['groups'][0]['track_ids'].reverse();self.catalog.serial+=1
        self.c.tick(1.2)
        self.assertEqual(self.c.songs[self.c.browse_items[self.c.cursor]]['id'],selected)
        self.assertNotEqual(self.c.browser_token,token)

    def test_stale_browser_knob_does_not_move_a_replaced_same_size_view(self):
        self.spotify();self.event('P',0);token=self.c.browser_token
        self.catalog.catalog['groups'][0]['track_ids'].reverse();self.catalog.serial+=1;self.c.tick(1.2)
        self.assertNotEqual(token,self.c.browser_token);cursor=self.c.cursor;seq=self.c.seq
        self.event('K',1,token=token,mode='B')
        self.assertEqual((self.c.cursor,self.c.seq),(cursor,seq))

    def test_removed_playlist_current_can_finish_through_artist_without_expanding_scope(self):
        self.start();self.catalog.catalog['groups'][0]['track_ids']=['2'*22];self.catalog.serial+=1
        self.c.tick(1.2)
        self.assertEqual(self.c.state,'PLAYING');self.assertEqual(self.c.scope,('playlist',PLAYLIST))
        self.assertEqual([self.c.songs[i]['id'] for i in self.c.queue],['1'*22,'2'*22])
        self.event('Q',1)
        self.assertEqual([self.c.songs[i]['id'] for i in self.c.queue],['1'*22,'2'*22])
        self.audio.events.append(AudioEvent(self.c.item_token,'STOPPED',100000,100000,ended=True));self.c.tick(1.3)
        self.assertEqual(self.c.songs[self.c.current]['id'],'2'*22)

    def test_removed_scope_grant_does_not_expand_to_another_grants_tracks(self):
        self.spotify();self.event('L',1);self.event('P',0);self.event('P',0)
        self.audio.events.append(AudioEvent(self.c.item_token,'PLAYING',1000,100000));self.c.tick(1.1)
        self.assertEqual(self.c.scope,('artist',ARTIST))
        self.catalog.approvals['approvals']=[a for a in self.catalog.approvals['approvals'] if a['kind']=='playlist'];self.catalog.serial+=1
        self.c.tick(1.2);self.assertEqual(self.c.state,'PLAYING')
        self.assertEqual([self.c.songs[i]['id'] for i in self.c.queue],['1'*22])
        self.audio.events.append(AudioEvent(self.c.item_token,'STOPPED',100000,100000,ended=True));self.c.tick(1.3)
        self.assertEqual(self.c.state,'STOPPED')

    def test_playlist_refresh_does_not_replay_an_already_completed_removed_then_readded_song(self):
        self.start();self.event('S',1)
        self.catalog.catalog['groups'][0]['track_ids']=['2'*22];self.catalog.serial+=1;self.c.tick(1.2)
        self.catalog.catalog['groups'][0]['track_ids']=['1'*22,'2'*22];self.catalog.serial+=1;self.c.tick(1.3)
        self.assertEqual([self.c.songs[i]['id'] for i in self.c.queue],['2'*22])

    def test_saved_spotify_selection_and_scope_restore_stopped_into_actor(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            state.write_text(json.dumps(dict(version=3,source='S',selected_id='2'*22,scope=['playlist',PLAYLIST],volume=90,shuffle=True)))
            audio=Audio();c=Controller(audio,lambda v:None,songs=[],catalogs=self.catalog,state_path=state)
            c.receive('B8\tREADY',0);c.tick(.1)
            self.assertEqual((c.source,c.state,c.volume,c.scope),('S','STOPPED',60,('playlist',PLAYLIST)))
            selected=[command for command in audio.commands if isinstance(command,Select)]
            self.assertTrue(selected);self.assertEqual(selected[-1].item.uri,'spotify:track:'+'2'*22)
            self.assertFalse(selected[-1].playing)

    def test_restored_shuffle_plays_every_other_song_after_the_selected_song(self):
        import json
        for selected,cached in (('1'*22,True),('2'*22,False)):
            with self.subTest(selected=selected,cached=cached),tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'state.json'
                state.write_text(json.dumps(dict(version=3,source='S',selected_id=selected,scope=['playlist',PLAYLIST],shuffle=True)))
                data=Catalog();complete=catalog()
                if not cached:
                    data.catalog=dict(complete,groups=[],tracks={});data.status='LOADING';data.future=True
                audio=Audio();c=Controller(audio,lambda v:None,songs=[],catalogs=data,state_path=state,shuffle_items=lambda items:items.reverse())
                self.c=c
                c.receive('B8\tREADY',0);c.tick(.1)
                if not cached:
                    data.catalog=dict(complete,groups=[complete['groups'][1]],tracks={identity:complete['tracks'][identity] for identity in ['1'*22,'3'*22]})
                    data.serial+=1;c.tick(.15)
                    data.catalog=complete;data.status='READY';data.future=False;data.serial+=1;c.tick(.2)
                other='2'*22 if selected=='1'*22 else '1'*22
                self.assertEqual([c.songs[index]['id'] for index in c.queue],[selected,other])
                self.assertEqual(c.queue_position,0)
                self.event('P',1,now=.3)
                audio.events.append(AudioEvent(c.item_token,'PLAYING',10,100000));c.tick(.4)
                audio.events.append(AudioEvent(c.item_token,'STOPPED',100000,100000,ended=True));c.tick(.5)
                self.assertEqual(c.songs[c.current]['id'],other)
                self.assertTrue(audio.commands[-1].playing)
                audio.events.append(AudioEvent(c.item_token,'STOPPED',100000,100000,ended=True));c.tick(.6)
                self.assertEqual(c.state,'STOPPED')

    def test_missing_cache_retains_saved_song_and_scope_through_partial_catalogs(self):
        import json
        for partial_ids in (['1'*22,'3'*22],['1'*22,'2'*22,'3'*22]):
            with self.subTest(partial_ids=partial_ids),tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'state.json'
                state.write_text(json.dumps(dict(version=3,source='S',selected_id='2'*22,scope=['playlist',PLAYLIST],volume=30)))
                data=Catalog();complete=catalog()
                data.catalog=dict(version=1,approvals_revision=2,generated_at=1000,groups=[],tracks={})
                data.status='LOADING';data.future=True
                audio=Audio();c=Controller(audio,lambda v:None,songs=[],catalogs=data,state_path=state)
                c.receive('B8\tREADY',0);c.tick(.1)
                self.assertEqual(c._saved_spotify_id,'2'*22)
                c.dirty_at=0;c.save()
                self.assertEqual(json.loads(state.read_text())['selected_id'],'2'*22)
                artist=dict(complete['groups'][1],track_ids=partial_ids)
                data.catalog=dict(complete,groups=[artist],tracks={identity:complete['tracks'][identity] for identity in partial_ids})
                data.serial+=1;c.tick(.2)
                self.assertTrue(c.songs)
                self.assertEqual(c._saved_spotify_scope,('playlist',PLAYLIST))
                c.dirty_at=0;c.save()
                self.assertEqual(json.loads(state.read_text())['scope'],['playlist',PLAYLIST])
                data.catalog=complete;data.status='READY';data.future=False;data.serial+=1;c.tick(.3)
                self.assertEqual(c.songs[c.current]['id'],'2'*22)
                self.assertEqual(c.scope,('playlist',PLAYLIST))
                self.assertEqual(c.state,'STOPPED')
                self.assertIsNone(c._saved_spotify_id)
                selections=[command for command in audio.commands if isinstance(command,Select)]
                self.assertEqual(selections[-1].item.uri,'spotify:track:'+'2'*22)
                self.assertTrue(all(not command.playing for command in selections))

    def test_explicit_selection_cancels_pending_startup_restoration(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            state.write_text(json.dumps(dict(version=3,source='S',selected_id='2'*22,scope=['playlist',PLAYLIST])))
            data=Catalog();complete=catalog()
            data.catalog=dict(complete,groups=[complete['groups'][1]],tracks={identity:complete['tracks'][identity] for identity in ['1'*22,'3'*22]})
            audio=Audio();c=Controller(audio,lambda v:None,songs=[],catalogs=data,state_path=state)
            c.receive('B8\tREADY',0);c.tick(.1)
            c.scope=('artist',ARTIST);c.select(c.song_index['3'*22],'PLAYING')
            self.assertIsNone(c._saved_spotify_id)
            count=len(audio.commands)
            data.catalog=complete;data.serial+=1;c.tick(.2)
            self.assertEqual(c.songs[c.current]['id'],'3'*22)
            self.assertEqual(c.scope,('artist',ARTIST))
            self.assertEqual(len(audio.commands),count)

    def test_removed_saved_grant_does_not_wait_for_an_unavailable_catalog(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            state.write_text(json.dumps(dict(version=3,source='S',selected_id='2'*22,scope=['playlist',PLAYLIST])))
            data=Catalog();data.approvals['approvals']=[grant for grant in data.approvals['approvals'] if grant['kind']=='artist']
            data.catalog=dict(version=1,approvals_revision=2,generated_at=1000,groups=[],tracks={})
            data.status='LOADING';data.future=True
            c=Controller(Audio(),lambda v:None,songs=[],catalogs=data,state_path=state)
            c.receive('B8\tREADY',0);c.tick(.1)
            self.assertIsNone(c._saved_spotify_id)
            self.assertIsNone(c._saved_spotify_scope)

    def test_verified_short_track_eof_can_arrive_before_first_playing_snapshot(self):
        self.spotify();self.event('P',0);self.event('P',0)
        self.assertEqual(self.c.state,'BUFFERING');first=self.c.item_token
        self.audio.events.append(AudioEvent(first,'STOPPED',100000,100000,ended=True));self.c.tick(1.1)
        self.assertNotEqual(self.c.item_token,first)
        self.assertEqual(self.c.songs[self.c.current]['id'],'2'*22)


class GrantTests(unittest.TestCase):
    def test_union_exact_credits_membership_expiry_and_unknown(self):
        data=catalog();grants=dict(approvals=[dict(kind='playlist',id=PLAYLIST)])
        self.assertTrue(allowed(data['tracks']['1'*22],grants,data,1000))
        self.assertFalse(allowed(data['tracks']['1'*22],grants,data,1061))
        self.assertTrue(allowed(data['tracks']['1'*22],grants,data,1061,fresh=False))
        grants['approvals'].append(dict(kind='artist',id=ARTIST))
        self.assertTrue(allowed(data['tracks']['3'*22],grants,data,999999))
        other=fact('4'*22,[dict(id='B'*22,name='Approved artist')])
        self.assertFalse(allowed(other,grants,data,1000));self.assertFalse(allowed(other,None,data,1000))

    def test_group_jobs_publish_playlist_without_waiting_for_artist_and_locked_read_is_bounded(self):
        import threading
        release=threading.Event();started=threading.Event()
        class Client:
            def playlist(self,identity):return dict(id=identity,name='Synthetic playlist',snapshot_id='snapshot',tracks=[fact('1'*22)])
            def artist(self,identity):started.set();release.wait(3);return dict(id=identity,name='Synthetic artist',tracks=[fact('3'*22)])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'approvals.sqlite3');store.initialize();store.add('playlist',PLAYLIST);store.add('artist',ARTIST)
            service=Catalogs(store.path,root/'creds',root/'catalog.json',now=lambda:1000,client_factory=Client)
            try:
                service.tick(selected=True);self.assertTrue(started.wait(1))
                deadline=time.monotonic()+1
                while '1'*22 not in service.catalog['tracks'] and time.monotonic()<deadline:
                    service.tick(selected=True);time.sleep(.005)
                self.assertIn('1'*22,service.catalog['tracks']);self.assertNotIn('3'*22,service.catalog['tracks'])
                self.assertEqual(service.status,'READY')
                import sqlite3
                connection=sqlite3.connect(store.path);connection.execute('BEGIN EXCLUSIVE')
                started_at=time.monotonic();service.tick(force=True)
                self.assertLess(time.monotonic()-started_at,.15);self.assertFalse(service.permitted('1'*22))
                self.assertTrue(service.permitted('1'*22,start=False));connection.rollback();connection.close()
            finally:release.set();service.close()

    def test_slow_cache_persistence_does_not_hold_serial_tick(self):
        import threading
        release=threading.Event();writing=threading.Event()
        class Client:
            def playlist(self,identity):return dict(id=identity,name='Synthetic playlist',snapshot_id='snapshot',tracks=[fact('1'*22)])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'approvals.sqlite3');store.initialize();store.add('playlist',PLAYLIST)
            service=Catalogs(store.path,root/'creds',root/'catalog.json',now=lambda:1000,client_factory=Client)
            def write(value):writing.set();release.wait(2)
            service._save_cache=write
            try:
                service.tick(selected=True)
                deadline=time.monotonic()+1
                while not writing.is_set() and time.monotonic()<deadline:
                    started=time.monotonic();service.tick(selected=True);self.assertLess(time.monotonic()-started,.1);time.sleep(.005)
                self.assertTrue(writing.is_set());self.assertIn('1'*22,service.catalog['tracks'])
                started=time.monotonic();service.tick(selected=True);self.assertLess(time.monotonic()-started,.1)
            finally:release.set();service.close()

    def test_long_artist_scans_keep_capacity_for_playlist_refreshes(self):
        import threading
        release=threading.Event();artist_started=threading.Event();calls=[];clock=[1000]
        class Client:
            def playlist(self,identity):
                calls.append('playlist')
                return dict(id=identity,name='Synthetic playlist',snapshot_id='snapshot',tracks=[fact('1'*22)])
            def artist(self,identity):
                calls.append('artist');artist_started.set();release.wait(3)
                return dict(id=identity,name='Synthetic artist',tracks=[fact('3'*22)])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'approvals.sqlite3');store.initialize()
            store.add('playlist',PLAYLIST);store.add('artist',ARTIST);store.add('artist','B'*22)
            service=Catalogs(store.path,root/'creds',root/'catalog.json',now=lambda:clock[0],client_factory=Client)
            try:
                service.tick(selected=True);self.assertTrue(artist_started.wait(1))
                deadline=time.monotonic()+1
                while '1'*22 not in service.catalog['tracks'] and time.monotonic()<deadline:
                    service.tick(selected=True);time.sleep(.005)
                self.assertIn('1'*22,service.catalog['tracks'])
                clock[0]=1046
                deadline=time.monotonic()+1
                while calls.count('playlist')<2 and time.monotonic()<deadline:
                    service.tick(selected=True);time.sleep(.005)
                self.assertEqual(calls.count('playlist'),2)
                self.assertEqual(calls.count('artist'),1)
            finally:release.set();service.close()

    def test_corrupt_private_catalog_is_ignored_at_the_file_boundary(self):
        import json
        mutations=[lambda c:c.update(version=2),lambda c:c.update(generated_at=float('nan')),
                   lambda c:c['groups'][0].update(verified_at=float('inf')),
                   lambda c:c['groups'][0]['track_ids'].append('1'*22),
                   lambda c:c['tracks']['1'*22].update(id='bad'),
                   lambda c:c['tracks']['1'*22].update(album=None),
                   lambda c:c['tracks']['1'*22]['artists'][0].update(id='bad')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for mutate in mutations:
                value=catalog();mutate(value);(root/'catalog.json').write_text(json.dumps(value))
                service=Catalogs(root/'approvals.sqlite3',root/'creds',root/'catalog.json')
                try:self.assertEqual(service.catalog['tracks'],{})
                finally:service.close()
