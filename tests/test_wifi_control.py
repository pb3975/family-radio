"""Synthetic broker/USB control checks. No daemon, hardware or privileges."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from host.wifi_broker import Broker, Server, UNIT
from host.wifi_control import WifiControl, request_broker, wifi_payload
from host.wifi_network import private_write

ID1='1'*32
ID2='2'*32
SESSION='1234567890abcdef'


def status(identity=ID1, state='starting', revision=1, terminal=False, ap_up=False):
    return dict(version=1, setup_id=identity, revision=revision, state=state, deadline=1600,
                error='-', terminal=terminal, ap_up=ap_up)


def view(identity=ID1, state='starting', revision=1, terminal=False):
    return dict(version=1, setup_id=identity, revision=revision, state=state,
                remaining=590, terminal=terminal, error='-')


class Unit:
    def __init__(self, directory):
        self.directory=directory
        self.fields=dict(ActiveState='inactive', MainPID='0', ControlPID='0', Job='', InvocationID='')
        self.calls=[]
        self.starts=0
        self.assign=True
        self.queue_only=False

    def run(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[:2]==['systemctl','show']:
            return '\n'.join(k+'='+v for k,v in self.fields.items())
        if arguments[:2]==['systemctl','start']:
            self.starts+=1
            if self.queue_only:
                self.fields['Job']='1 /job/1'
                return ''
            self.fields.update(ActiveState='activating',Job='1 /job/1')
            if self.assign:
                identity=f'{self.starts:032x}'
                self.fields.update(ActiveState='active',MainPID='123',Job='',InvocationID=identity)
                private_write(self.directory/'status.json',json.dumps(status(identity)))
        elif arguments[:2]==['systemctl','stop']:
            if self.fields['ActiveState'] in ('inactive','failed'):
                self.fields['Job']=''
            else:
                self.fields.update(ActiveState='deactivating',Job='2 /job/2')
        else:
            raise AssertionError(arguments)
        return ''

    def complete(self, state='cancelled'):
        identity=self.fields['InvocationID']
        private_write(self.directory/'status.json',json.dumps(status(identity,state,2,True)))
        self.fields.update(ActiveState='inactive',MainPID='0',ControlPID='0',Job='')


class BrokerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.data=self.root/'data';self.state=self.root/'status';self.runtime=self.root/'runtime'
        for p in (self.data,self.state,self.runtime):p.mkdir()
        self.unit=Unit(self.state);self.now=1000
        self.broker=Broker(self.data,self.state,self.runtime,self.unit.run,lambda:self.now,lambda _:None)

    def start(self, nonce=ID1):
        return self.broker.request(dict(op='start',request_id=nonce))

    def ready(self):
        identity=self.unit.fields['InvocationID']
        private_write(self.state/'status.json',json.dumps(status(identity,'ready',2,False,True)))
        private_write(self.runtime/'access.json',json.dumps(dict(setup_id=identity,ssid='Synthetic setup',password='synthetic-secret')))
        return identity

    def test_prepare_is_read_only_and_credential_requires_ready_matching_invocation(self):
        self.assertEqual(self.broker.request(dict(op='prepare'))['state'],'idle')
        self.assertEqual(self.unit.starts,0)
        current=self.start()['setup_id']
        with self.assertRaises(ValueError):self.broker.request(dict(op='credential',setup_id=current))
        self.ready()
        safe=self.broker.request(dict(op='status',setup_id=current))
        self.assertNotIn('access',safe)
        access=self.broker.request(dict(op='credential',setup_id=current))
        self.assertEqual(access['access']['password'],'synthetic-secret')
        self.assertNotIn('synthetic-secret',(self.state/'status.json').read_text())
        with self.assertRaises(ValueError):self.broker.request(dict(op='credential',setup_id=ID2))

    def test_duplicate_nonce_survives_unit_exit_and_never_starts_again(self):
        current=self.start()
        self.assertEqual(self.start()['setup_id'],current['setup_id'])
        self.unit.complete('connected')
        terminal=self.start()
        self.assertEqual((terminal['state'],terminal['terminal']),('connected',True))
        self.assertEqual(self.unit.starts,1)
        self.assertEqual(self.broker.request(dict(op='prepare'))['state'],'idle')

    def test_stale_cancel_cannot_stop_a_later_invocation(self):
        first=self.start()['setup_id'];self.unit.complete()
        second=self.start(ID2)['setup_id']
        before=len(self.unit.calls)
        with self.assertRaises(ValueError):self.broker.request(dict(op='stop',setup_id=first))
        self.assertFalse(any(c[:2]==['systemctl','stop'] for c in self.unit.calls[before:]))
        self.assertEqual(self.broker.request(dict(op='stop',setup_id=second))['state'],'stopping')

    def test_pending_job_and_unclean_journal_block_start(self):
        self.unit.fields['Job']='1 /job/1'
        with self.assertRaises(ValueError):self.start()
        self.unit.fields['Job']=''
        (self.data/'session.json').write_text('{}')
        self.assertEqual(self.broker.request(dict(op='prepare'))['state'],'recovery_failed')
        with self.assertRaises(ValueError):self.start()
        self.assertEqual(self.unit.starts,0)

    def test_no_invocation_assignment_is_a_terminal_failure_not_an_automatic_retry(self):
        self.unit.assign=False
        self.assertEqual(self.start()['error'],'START')
        self.assertEqual(self.start()['error'],'START')
        self.assertEqual(self.unit.starts,1)
        with self.assertRaises(ValueError):self.broker.request(dict(op='stop',setup_id=ID1))

    def test_queued_start_cancels_the_job_before_reporting_failure(self):
        self.start();self.unit.complete('connected')
        self.unit.queue_only=True
        result=self.start(ID2)
        self.assertEqual((result['state'],result['terminal']),('error',True))
        self.assertEqual(self.unit.fields['Job'],'')
        self.assertEqual(self.unit.fields['ActiveState'],'inactive')
        self.assertEqual(self.start(ID2)['state'],'error')
        self.assertEqual(self.unit.starts,2)

    def test_queued_job_never_exposes_or_stops_the_previous_invocation(self):
        previous=self.start()['setup_id'];self.unit.complete('connected')
        self.unit.fields['Job']='1 /job/1'
        result=self.broker.request(dict(op='prepare'))
        self.assertEqual((result['state'],result['setup_id'],result['terminal']),('starting',None,False))
        before=len(self.unit.calls)
        with self.assertRaises(ValueError):self.broker.request(dict(op='stop',setup_id=previous))
        self.assertFalse(any(c[:2]==['systemctl','stop'] for c in self.unit.calls[before:]))

    def test_recent_completed_nonce_ledger_is_bounded_without_evicting_for_new_start(self):
        for n in range(64):
            self.start(f'{n+100:032x}');self.unit.complete()
        with self.assertRaises(ValueError):self.start('f'*32)
        self.assertEqual(self.unit.starts,64)
        self.now+=86401
        self.start('f'*32)
        self.assertEqual(self.unit.starts,65)

    def test_unknown_fields_and_command_selectors_are_rejected(self):
        for request in ({'op':'shell'},{'op':'start','request_id':ID1,'unit':'other.service'},
                        {'op':'start','request_id':'bad'},{'op':'credential','setup_id':ID1,'path':'/etc/passwd'}):
            with self.assertRaises(ValueError):self.broker.request(request)
        self.assertEqual(self.unit.starts,0)

    def server(self):
        path=str(self.root/'broker.sock')
        server=Server(path,self.broker,os.getuid())
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return path

    def test_portal_uid_is_denied_before_any_operation(self):
        path=self.server()
        with patch('host.wifi_broker.struct.unpack',return_value=(123,9876,9876)):
            with self.assertRaises(OSError):request_broker(dict(op='prepare'),path)
        self.assertEqual(self.unit.calls,[])

    def test_malformed_credential_response_never_logs_or_returns_the_secret(self):
        identity=self.start()['setup_id'];self.ready()
        secret='DO_NOT_LOG_SENTINEL\n'
        private_write(self.runtime/'access.json',json.dumps(dict(setup_id=identity,ssid='Synthetic setup',password=secret)))
        path=self.server();errors=io.StringIO()
        with contextlib.redirect_stderr(errors),socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(2);connection.connect(path)
            connection.sendall(json.dumps(dict(op='credential',setup_id=identity)).encode()+b'\n')
            answer=connection.recv(4096).decode()
        self.assertEqual(json.loads(answer),dict(error='Setup request was not accepted.'))
        self.assertNotIn('DO_NOT_LOG_SENTINEL',answer+errors.getvalue())


class ClientTests(unittest.TestCase):
    def wait(self, client, predicate):
        for _ in range(300):
            client.tick()
            if predicate():return
            time.sleep(.005)
        self.fail('Synthetic broker operation did not complete')

    def test_duplicate_start_and_cancel_before_reply_use_the_bound_invocation(self):
        gate=threading.Event();calls=[]
        def rpc(request):
            calls.append(request)
            if request['op']=='start':gate.wait(2);return view()
            if request['op']=='stop':return view(state='cancelled',revision=2,terminal=True)
            raise AssertionError(request)
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.action(SESSION,1,'S',None)
        client.action(SESSION,1,'S',None)
        client.action(SESSION,2,'T',None)
        self.assertIsNone(client.record());gate.set()
        self.wait(client,lambda:client.record() is not None)
        self.assertEqual([c['op'] for c in calls],['start','stop'])
        self.assertEqual(calls[-1]['setup_id'],ID1)
        self.assertIn(b'\t2\t',client.record())
        self.assertIn(b'\tCANCELLED\t',client.record())

    def test_usb_session_change_discards_a_queued_start_and_stale_reply(self):
        gate=threading.Event();calls=[]
        def rpc(request):
            calls.append(request)
            if len(calls)==1:gate.wait(2)
            return dict(version=1,setup_id=None,revision=0,state='idle',remaining=0,terminal=True,error='-')
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.action(SESSION,1,'P',None)
        # Ensure the first read is actually in flight before queuing start.
        for _ in range(100):
            if calls:break
            time.sleep(.001)
        client.action(SESSION,2,'S',None)
        client.close();client.action('fedcba0987654321',1,'P',None)
        self.assertIsNone(client.record());gate.set()
        self.wait(client,lambda:client.record() is not None)
        self.assertTrue(all(c['op']=='prepare' for c in calls))
        self.assertIn(b'fedcba0987654321',client.record())
        self.assertNotIn(SESSION.encode(),client.record())

    def test_panel_exit_does_not_discard_an_explicit_cancel_queued_after_start(self):
        gate=threading.Event();calls=[]
        def rpc(request):
            calls.append(request)
            if request['op']=='start':gate.wait(2);return view()
            if request['op']=='stop':return view(state='cancelled',revision=2,terminal=True)
            raise AssertionError(request)
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.action(SESSION,1,'S',None)
        for _ in range(100):
            if calls:break
            time.sleep(.001)
        client.action(SESSION,2,'T',None)
        client.action(SESSION,3,'X',None)
        gate.set()
        self.wait(client,lambda:any(c['op']=='stop' for c in calls))
        self.assertEqual([c['op'] for c in calls],['start','stop'])
        self.assertEqual(calls[-1]['setup_id'],ID1)
        self.assertNotIn('access',client.view)

    def test_only_explicit_open_panel_gets_credentials_and_close_drops_them(self):
        calls=[]
        def rpc(request):
            calls.append(request)
            result=view(state='ready',revision=2)
            if request['op']=='credential':result['access']=dict(ssid='Synthetic setup',password='synthetic-secret')
            return result
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.tick();self.assertEqual(calls,[])
        client.action(SESSION,1,'P',None)
        self.wait(client,lambda:client.record() is not None)
        self.assertEqual([c['op'] for c in calls],['prepare','credential'])
        self.assertIn('synthetic-secret',client.view['access']['password'])
        self.assertLess(len(client.record()),1153)
        client.action(SESSION,2,'X',ID1)
        self.assertIsNone(client.record());self.assertNotIn('access',client.view)
        self.assertNotIn('synthetic-secret',repr(client.__dict__))

    def test_sensitive_errors_become_fixed_safe_wire_errors(self):
        def rpc(_):raise ValueError('DO_NOT_LOG_SENTINEL')
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.action(SESSION,1,'P',None)
        self.wait(client,lambda:client.record() is not None)
        self.assertIn(b'UNAVAILABLE',client.record())
        self.assertNotIn(b'DO_NOT_LOG_SENTINEL',client.record())

    def test_bound_status_and_cancel_transport_errors_are_consumed_by_firmware(self):
        compiler=shutil.which('c++')
        if not compiler:self.skipTest('native firmware parser check needs c++')
        for action in ('Q','T'):
            with self.subTest(action=action):
                def rpc(request):
                    if request['op']=='prepare':return view(state='connecting',revision=7)
                    raise OSError('DO_NOT_LOG_SENTINEL')
                client=WifiControl(rpc);self.addCleanup(client.shutdown)
                client.action(SESSION,1,'P',None)
                self.wait(client,lambda:client.record() is not None)
                client.action(SESSION,2,action,ID1)
                self.wait(client,lambda:client.record() is not None)
                record=client.record().decode().rstrip('\n')
                self.assertNotIn('DO_NOT_LOG_SENTINEL',record)
                source="""#include <cassert>
#include <cstring>
#include <string>
#include <vector>
#include "firmware/family_dial/wifi_ui.h"
int main(){
 WifiUI ui;ui.request('P');strcpy(ui.setupId,"IDENTITY");ui.revision=7;
 ui.request('ACTION');
 std::string record=RECORD;
 std::vector<char> line(record.begin(),record.end());line.push_back(0);
 char* fields[12];char* part=line.data();
 for(int i=0;i<12;++i){fields[i]=part;char* tab=strchr(part,'\\t');
  if(i<11){assert(tab);*tab=0;part=tab+1;}else assert(!tab);}
 assert(ui.receive(fields,100));assert(!ui.pending);
 assert(!strcmp(ui.setupId,"IDENTITY")&&ui.revision==7);
 assert(!strcmp(ui.state,"ERROR")&&!strcmp(ui.error,"UNAVAILABLE"));
 assert(!ui.hasAccess()&&!ui.password[0]&&!ui.payload[0]);
}
""".replace('IDENTITY',ID1).replace('ACTION',action).replace('RECORD',json.dumps(record))
                with tempfile.TemporaryDirectory() as directory:
                    path=Path(directory);(path/'check.cpp').write_text(source)
                    subprocess.run([compiler,'-std=c++11','-I',str(Path(__file__).parents[1]),str(path/'check.cpp'),'-o',str(path/'check')],check=True)
                    subprocess.run([str(path/'check')],check=True)

    def test_credential_race_observes_joining_instead_of_displaying_a_stale_qr(self):
        calls=[]
        def rpc(request):
            calls.append(request)
            if request['op']=='credential':raise ValueError('No longer ready')
            return view(state='connecting' if request['op']=='status' else 'ready',revision=2)
        client=WifiControl(rpc);self.addCleanup(client.shutdown)
        client.action(SESSION,1,'P',None)
        self.wait(client,lambda:client.record() is not None)
        self.assertEqual([c['op'] for c in calls],['prepare','credential','status'])
        self.assertIn(b'CONNECTING',client.record())
        self.assertNotIn('access',client.view)

    def test_wifi_qr_escaping_preserves_standard_special_characters(self):
        self.assertEqual(wifi_payload('room;name','key,:"\\secret'),
                         'WIFI:T:WPA;S:room\\;name;P:key\\,\\:\\"\\\\secret;;')


class ControllerWireTests(unittest.TestCase):
    def test_b7_wifi_commands_are_session_bound_ordered_and_do_not_change_playback(self):
        from host.radio import Controller
        class Player:
            def __init__(self):self.calls=[]
            def pause(self,value):self.calls.append(value)
        class Wifi:
            def __init__(self):self.calls=[]
            def close(self):pass
            def action(self,*args):self.calls.append(args)
        player=Player();wifi=Wifi();controller=Controller(player,lambda _:None,wifi=wifi)
        controller.receive('B6\tREADY',0)
        self.assertEqual(controller.session,'')
        controller.receive('B8\tREADY',0)
        session=controller.session;controller.state='PLAYING';calls=player.calls.copy()
        request=f'B8\tW\t{session}\t1\t1\tS\t-'
        controller.receive(request,1)
        self.assertEqual(len(wifi.calls),1)
        self.assertEqual(controller.state,'PLAYING')
        self.assertEqual(player.calls,calls)
        for bad in (request,request.replace(session,'0'*16),request.replace('\t1\t1\t','\t2\t0\t'),
                    request.replace('\tS\t','\tZ\t'),request.replace('\t-','\tbad')):
            controller.receive(bad,1.1)
        self.assertEqual(len(wifi.calls),1)
        self.assertEqual(controller.last_seen,1)
        self.assertEqual(controller.snapshot().split(b'\t')[0],b'B8')
        self.assertNotIn(b'WIFI:',controller.snapshot())


class SerialSecretTests(unittest.TestCase):
    def test_abandoned_partial_credential_is_scrubbed_before_a_new_snapshot(self):
        from host.radio import SerialLink
        link=object.__new__(SerialLink);link.fd=123
        record=(f'B8\tW\t{SESSION}\t1\t{ID1}\t1\tREADY\t600\tssid\tsecret-hex\tpayload-hex\t-\n').encode()
        link.pending=bytearray(record)
        link.pending_wifi=link.wifi_identity(record)
        with patch('host.radio.os.write',return_value=10):link.send(b'')
        abandoned=link.pending
        link.scrub_wifi(None)
        self.assertEqual(link.pending,b'\n')
        self.assertTrue(all(value==0 for value in abandoned))
        self.assertIsNone(link.pending_wifi)

    def test_same_view_finishes_its_partial_record_and_fixture_is_bounded(self):
        from host.radio import SerialLink
        fixture=json.loads((Path(__file__).parents[1]/'docs/fixtures/protocol-b8.json').read_text())
        record=fixture['wifi_ready'].encode()
        self.assertLessEqual(len(record)-1,1152)
        self.assertEqual(len(record.rstrip(b'\n').split(b'\t')),12)
        link=object.__new__(SerialLink);link.pending=bytearray(record[10:]);link.pending_wifi=link.wifi_identity(record)
        link.scrub_wifi(record)
        self.assertEqual(link.pending,record[10:])


if __name__=='__main__':unittest.main()
