"""双账号路由、旧数据、relay 仲裁和手动降级的行为回归。"""
import concurrent.futures
from contextlib import closing
import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import pair
from scripts.pair_core import Daemon, FileLock, PairError, SCHEMA, Store
from scripts.pair_transport import DeliveryUnknown, TransportError, TransportPool, canonical_path
from tests.test_core import FakeTransport
from tests.test_integration import seed


def shared_claim(root, home, session, cwd, message):
    return Store('accounts',home=home,state_root=root).claim('B',session,cwd,message_id=message)['claimed']


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pair-accounts-')
        self.root=Path(self.temp.name)
        self.project=self.root/'project';seed(self.project)
        self.homes={m:self.root/('home-'+m) for m in ('A','B')}
        for home in self.homes.values():home.mkdir()
        self.state_root=self.project/'.codex-pair'/'state'
        self.common=['--pair','accounts','--state-root',str(self.state_root),'--codex-home',str(self.homes['A'])]
        pair.execute(self.args('init','--project',str(self.project)))
        self.store=Store('accounts',home=self.homes['A'],state_root=self.state_root)
        for index,m in enumerate(('A','B'),1):
            cwd=self.store.git_context()['worktrees'][m.lower()]['path']
            session='00000000-0000-0000-0000-'+str(index).zfill(12)
            self.store.join(m,session,cwd,backend='queue',owned=False,
                proof={'id':session,'cwd':cwd},member_codex_home=self.homes[m])

    def tearDown(self):self.temp.cleanup()

    def args(self,*args):return pair.build_parser().parse_args(self.common+list(args))

    def test_shared_state_reopened_from_other_account_and_envelope_routes_back(self):
        other=Store('accounts',home=self.homes['B'],state_root=self.state_root)
        message=other.send('A','B','中文问答')
        envelope=json.loads(other.envelope(other.member('B'),message))
        argv=envelope['claim_argv']
        self.assertEqual(Path(argv[argv.index('--state-root')+1]),self.state_root)
        self.assertEqual(canonical_path(argv[argv.index('--codex-home')+1]),canonical_path(self.homes['B']))
        self.assertEqual(other.member('A')['codex_home'],canonical_path(self.homes['A']))
        self.assertTrue((self.homes['B']/'codex-pair'/'session-bindings.sqlite3').is_file())
        self.assertEqual(list((self.homes['B']/'codex-pair').rglob('state.sqlite3')),[])
        self.assertEqual(other.path,self.store.path)

    def test_pool_routes_and_caches_by_account_without_fallback(self):
        pool=TransportPool(codex_home=self.homes['A'])
        with patch('scripts.pair_transport.QueueTransport') as factory:
            factory.side_effect=lambda **kw:Mock(_dead=False)
            a=pool.client(self.store.member('A'));b=pool.client(self.store.member('B'))
            self.assertIsNot(a,b)
            self.assertEqual([c.kwargs['codex_home'] for c in factory.call_args_list],
                [canonical_path(self.homes[m]) for m in ('A','B')])
            self.assertIs(pool.client(self.store.member('B')),b)
            altered=dict(self.store.member('B'),codex_home=str(self.homes['A']))
            self.assertIsNot(pool.client(altered),b)
        with patch('scripts.pair_transport.QueueTransport',side_effect=TransportError('wrong account')) as factory:
            failed=TransportPool(codex_home=self.homes['A'])
            with self.assertRaises(TransportError):failed.client(self.store.member('B'))
            self.assertEqual(factory.call_count,1)
            self.assertEqual(factory.call_args.kwargs['codex_home'],canonical_path(self.homes['B']))
            with self.assertRaisesRegex(TransportError,'account missing'):
                failed.client(dict(self.store.member('A'),codex_home=None))

    def test_cli_actor_and_relay_reject_wrong_home_before_contact(self):
        b=self.store.member('B')
        args=self.args('claim','--member','B','--session',b['session_uuid'],'--worktree',b['cwd'])
        with patch.dict('os.environ',{'CODEX_THREAD_ID':b['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=Path(b['cwd'])),patch('scripts.pair.AppServer') as server:
            with self.assertRaisesRegex(PairError,'account mismatch'):pair.execute(args)
            server.assert_not_called()
        with patch('scripts.pair.TransportPool') as pool:
            with self.assertRaisesRegex(PairError,'Relay account mismatch'):
                pair.execute(self.args('relay','--member','B'))
            pool.assert_not_called()

    def test_current_session_join_uses_environment_and_requires_real_cwd(self):
        b=self.store.member('B')
        args=self.args('join','--member','B','--current-session','--member-codex-home',str(self.homes['B']),'--worktree',b['cwd'])
        with patch.dict('os.environ',{'CODEX_THREAD_ID':b['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=self.project),patch('scripts.pair.AppServer') as server:
            with self.assertRaisesRegex(PairError,'Current session context'):pair.execute(args)
            server.assert_not_called()
        args=self.args('join','--member','B','--current-session','--member-codex-home',str(self.homes['B']),'--worktree',b['cwd'])
        with patch.dict('os.environ',{'CODEX_THREAD_ID':b['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=Path(b['cwd'])),patch('scripts.pair.AppServer') as server:
            server.return_value.__enter__.return_value.validate.return_value={'id':b['session_uuid'],'cwd':b['cwd'],'modelProvider':'openai'}
            result=pair.execute(args)
            self.assertEqual(result['session_uuid'],b['session_uuid'])
            self.assertEqual(canonical_path(server.call_args.kwargs['codex_home']),canonical_path(self.homes['B']))

    def test_cli_actor_rejects_other_thread_even_with_correct_home(self):
        b=self.store.member('B')
        args=self.args('claim','--member','B','--session',b['session_uuid'],'--worktree',b['cwd'],
                       '--member-codex-home',str(self.homes['B']))
        with patch.dict('os.environ',{'CODEX_THREAD_ID':self.store.member('A')['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=Path(b['cwd'])),patch('scripts.pair.AppServer') as server:
            with self.assertRaisesRegex(PairError,'Current original CLI required'):pair.execute(args)
            server.assert_not_called()

    def test_shared_database_two_home_concurrent_claim_once(self):
        message=self.store.send('A','B','只执行一次')
        b=self.store.member('B')
        with concurrent.futures.ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context('spawn')) as executor:
            outcomes=list(executor.map(shared_claim,
                [str(self.state_root)]*4,[str(self.homes[m]) for m in ('A','B','A','B')],
                [b['session_uuid']]*4,[b['cwd']]*4,[message['id']]*4))
        self.assertEqual(sum(outcomes),1)

    def test_relay_recovers_only_own_outbox_and_keeps_unknown(self):
        to_a=self.store.send('B','A','A outbox');to_b=self.store.send('A','B','B outbox')
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='delivering'")
        self.store.recover('relay-b','B')
        self.assertEqual(self.store.message(to_a['id'])['delivery_status'],'delivering')
        self.assertEqual(self.store.message(to_b['id'])['delivery_status'],'unknown')
        transport=FakeTransport(self.store)
        Daemon(self.store,transport,member_id='B').run(max_ticks=1)
        self.assertEqual(transport.calls,[])

    def test_relays_filter_account_and_dispatcher_cannot_overlap(self):
        to_a=self.store.send('B','A','A');to_b=self.store.send('A','B','B')
        fake=FakeTransport(self.store)
        Daemon(self.store,fake,member_id='B').run(max_ticks=1)
        self.assertEqual([m['id'] for m,_ in fake.calls],['B'])
        self.assertEqual(self.store.message(to_a['id'])['attempts'],0)
        self.assertEqual(self.store.message(to_b['id'])['attempts'],1)
        with FileLock(self.store.directory/'relay-A.lock'):
            with self.assertRaises(PairError):Daemon(self.store,FakeTransport()).run(max_ticks=1)
            with self.assertRaises(PairError):Daemon(self.store,FakeTransport(),member_id='A').run(max_ticks=1)
            Daemon(self.store,FakeTransport(),member_id='B').run(max_ticks=1)
        with FileLock(self.store.daemon_lock):
            with self.assertRaises(PairError):Daemon(self.store,FakeTransport(),member_id='B').run(max_ticks=1)

    def test_real_relay_processes_probe_stop_one_and_restart(self):
        script=str(Path(pair.__file__).resolve())
        processes=[]
        def start(member):
            process=subprocess.Popen([sys.executable,script,*self.common,'relay','--member',member,
                '--member-codex-home',str(self.homes[member]),'--poll-interval','.02','--json'],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            processes.append(process)
            return process
        def wait_relay(member):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status=self.store.status(probe_timeout=.15)
                if any(r['member']==member and r['probe_confirmed'] and r['lock_held'] for r in status['relays']):return
            self.fail('relay failed to acknowledge challenge')
        try:
            a=start('A');b=start('B');wait_relay('A');wait_relay('B')
            pair.execute(self.args('relay-stop','--member','B'))
            self.assertEqual(b.wait(timeout=10),0)
            self.assertIsNone(a.poll())
            b=start('B');wait_relay('B')
            self.store.control('paused')
            self.assertTrue(any(r['probe_confirmed'] for r in self.store.status(probe_timeout=1)['relays']))
            self.store.control('stopped')
            self.assertEqual(a.wait(timeout=10),0);self.assertEqual(b.wait(timeout=10),0)
        finally:
            self.store.control('stopped')
            for process in processes:
                try:process.communicate(timeout=10)
                except subprocess.TimeoutExpired:process.terminate();process.communicate(timeout=5)

    def test_manual_is_explicit_durable_and_never_calls_transport(self):
        with self.store.transaction() as db:db.execute("UPDATE members SET delivery_mode='manual' WHERE id='B'")
        message=self.store.send('A','B','手动交接')
        fake=FakeTransport(self.store)
        Daemon(self.store,fake).run(max_ticks=1)
        self.assertEqual(fake.calls,[])
        actual=self.store.message(message['id'])
        self.assertEqual(actual['delivery_status'],'manual_handoff')
        self.assertEqual(actual['remote_status'],'manual')
        self.assertEqual(actual['process_status'],'unclaimed')
        path=self.project/'.codex-pair'/'handoffs'/'accounts'/(message['id']+'.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['mode'],'manual')
        Daemon(self.store,fake).run(max_ticks=1)
        self.assertEqual(self.store.message(message['id'])['attempts'],1)
        self.store.control('paused')
        b=self.store.member('B');claim=self.store.claim('B',b['session_uuid'],b['cwd'],message_id=message['id'])
        self.assertTrue(claim['claimed'])
        self.store.complete('B',b['session_uuid'],b['cwd'],message['id'],claim['claim_token'],'手动完成')
        self.assertEqual(self.store.message(message['id'])['remote_status'],'completed')
        pending=self.store.send('A','B','暂停后不发')
        Daemon(self.store,fake).run(max_ticks=1)
        self.assertEqual(self.store.message(pending['id'])['attempts'],0)
        self.store.control('stopped')
        self.assertFalse(self.store.claim('B',b['session_uuid'],b['cwd'],message_id=pending['id'])['claimed'])

    def test_manual_receipt_crash_is_closed_only_by_explicit_user_completion(self):
        with self.store.transaction() as db:db.execute("UPDATE members SET delivery_mode='manual' WHERE id='B'")
        message=self.store.send('A','B','手动文件落盘后模拟崩溃')
        daemon=Daemon(self.store,FakeTransport())
        publish=daemon.manual_handoff
        def crash(member,payload):
            publish(member,payload)
            raise SystemExit('simulated death before SQLite receipt')
        with patch.object(daemon,'manual_handoff',side_effect=crash):
            with self.assertRaises(SystemExit):daemon.run(max_ticks=1)
        self.assertEqual(self.store.message(message['id'])['delivery_status'],'delivering')
        Daemon(self.store,FakeTransport()).run(max_ticks=1)
        self.assertEqual(self.store.message(message['id'])['delivery_status'],'unknown')
        self.assertEqual(self.store.message(message['id'])['attempts'],1)
        b=self.store.member('B')
        claimed=self.store.claim('B',b['session_uuid'],b['cwd'],message_id=message['id'])
        self.store.complete('B',b['session_uuid'],b['cwd'],message['id'],claimed['claim_token'],'用户明确完成手动交接')
        next_message=self.store.send('A','B','后续交接')
        Daemon(self.store,FakeTransport()).run(max_ticks=1)
        self.assertEqual(self.store.message(next_message['id'])['delivery_status'],'manual_handoff')

    def legacy(self, unresolved=False):
        directory=self.homes['A']/'codex-pair'/'legacy';directory.mkdir(parents=True)
        db=sqlite3.connect(directory/'state.sqlite3');db.executescript(SCHEMA)
        db.execute('INSERT INTO collaborations(id,project,base_commit,status,created) VALUES(?,?,?,?,?)',
            ('legacy',str(self.project),'a'*40,'paused',0))
        for m in ('A','B'):
            member=self.store.member(m)
            db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?)',
                (m,member['session_uuid'],member['cwd'],'queue',0,member['proof'],0))
        db.execute("INSERT INTO events(kind,data,created) VALUES('user_history','{}',0)")
        if unresolved:
            db.execute("INSERT INTO messages(id,sender,recipient,kind,body,dedupe_key,fingerprint,delivery_status,created,updated) VALUES('old','A','B','note','历史','old','hash','unknown',0,0)")
        db.commit();db.close()
        return directory

    def test_legacy_schema_backed_up_in_place_and_inherits_original_home(self):
        directory=self.legacy()
        store=Store('legacy',home=self.homes['A'])
        self.assertEqual(store.directory,directory)
        self.assertEqual(store.member('B')['codex_home'],str(self.homes['A'].resolve()))
        backups=list(directory.glob('schema-v1-*.backup.sqlite3'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        with store.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],13)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='user_history'").fetchone()[0],1)
        Store('legacy',home=self.homes['A'])
        self.assertEqual(len(list(directory.glob('*.backup.sqlite3'))),1)

    def test_legacy_unresolved_delivery_blocks_upgrade_without_changes(self):
        directory=self.legacy(unresolved=True)
        with self.assertRaisesRegex(PairError,'Legacy delivery unresolved'):
            Store('legacy',home=self.homes['A'])
        with closing(sqlite3.connect(directory/'state.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT delivery_status FROM messages').fetchone()[0],'unknown')


if __name__=='__main__':unittest.main()
