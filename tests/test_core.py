import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from scripts.pair_core import Daemon, FileLock, PairError, Store, scopes_overlap
from scripts.pair_transport import DeliveryUnknown, TransportError


class FakeTransport:
    def __init__(self,store=None,error=None):
        self.store,self.error=store,error
        self.calls=[]
        self.observation=None
        self.closed=False

    def start(self,member,message):
        if self.store:
            with self.store.transaction() as db:
                persisted=db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()
                assert persisted['delivery_status']=='delivering'
                assert persisted['envelope']==message['envelope']
                assert db.execute('SELECT count(*) FROM attempts WHERE message_id=?',(message['id'],)).fetchone()[0]>0
        self.calls.append((member,message))
        if self.error:raise self.error
        return {'turn_id':'turn-'+message['id'],'remote_status':'inProgress'}

    def observe(self,member,message):
        return self.observation

    def close(self):
        self.closed=True


def concurrent_claim(home,pair_id,session,cwd,message_id,barrier,results):
    try:
        store=Store(pair_id,home=home)
        barrier.wait(timeout=15)
        result=store.claim('B',session,cwd,message_id=message_id)
        results.put({'claimed':result['claimed']})
    except BaseException as exc:
        results.put({'error':repr(exc)})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='pair-unit-')
        self.root=Path(self.temporary.name)
        self.project=self.root/'project';self.project.mkdir()
        self.home=self.root/'codex-home';self.home.mkdir()
        self.store=Store.initialize('test',str(self.project),'a'*40,home=self.home)
        self.sessions={'A':'00000000-0000-0000-0000-000000000001','B':'00000000-0000-0000-0000-000000000002'}
        self.directories={}
        for member in ('A','B'):
            cwd=self.root/member;cwd.mkdir();self.directories[member]=str(cwd)
            self.store.join(member,self.sessions[member],str(cwd),backend='queue',owned=False,
                            proof={'id':self.sessions[member],'cwd':str(cwd)})

    def tearDown(self):
        self.temporary.cleanup()

    def message(self,**kwargs):
        return self.store.send('A','B','中文\n"引号" C:\\用户\\项目',**kwargs)

    def task(self,name='first',to='B',paths=('src/first.py',),dependencies=()):
        return self.store.assign(name,'任务 '+name,to,paths,dependencies=dependencies)

    def claim(self,message_id,member='B'):
        return self.store.claim(member,self.sessions[member],self.directories[member],message_id=message_id)

    def complete(self,message,commits=()):
        claimed=self.claim(message['id'])
        return self.store.complete('B',self.sessions['B'],self.directories['B'],message['id'],claimed['claim_token'],'完成了具体修改与检查',commits=commits)

    def state(self,message_id):
        with self.store.transaction() as db:
            return dict(db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone())

    def test_concurrent_process_claim_has_exactly_one_winner(self):
        message=self.message()
        context=multiprocessing.get_context('spawn')
        barrier=context.Barrier(4);results=context.Queue()
        processes=[context.Process(target=concurrent_claim,args=(str(self.home),'test',self.sessions['B'],self.directories['B'],message['id'],barrier,results)) for _ in range(4)]
        for process in processes:process.start()
        outcomes=[results.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():process.terminate();process.join()
            self.assertEqual(process.exitcode,0)
        self.assertFalse(any('error' in r for r in outcomes),outcomes)
        self.assertEqual(sum(r['claimed'] for r in outcomes),1)
        results.close();results.join_thread()

    def test_duplicate_message_does_not_execute_twice(self):
        first=self.message(dedupe_key='same-request')
        second=self.message(dedupe_key='same-request')
        self.assertEqual(first['id'],second['id'])
        claimed=self.claim(first['id']);self.assertTrue(claimed['claimed'])
        self.assertFalse(self.claim(first['id'])['claimed'])
        self.store.complete('B',self.sessions['B'],self.directories['B'],first['id'],claimed['claim_token'],'验证完成')
        self.assertFalse(self.claim(first['id'])['claimed'])
        self.assertTrue(self.store.complete('B',self.sessions['B'],self.directories['B'],first['id'],claimed['claim_token'],'验证完成')['duplicate'])
        with self.assertRaises(PairError):self.store.send('A','B','不同正文',dedupe_key='same-request')

    def test_dependency_not_dispatched_until_business_completion(self):
        self.task()
        task=self.task('dependent',to='A',paths=('src/other.py',),dependencies=('first',))
        self.assertEqual(task['status'],'pending')
        with self.store.transaction() as db:
            message=dict(db.execute("SELECT * FROM messages WHERE task_id='first'").fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE task_id='dependent'").fetchone()[0],0)
        fake=FakeTransport(self.store);daemon=Daemon(self.store,fake)
        daemon.dispatch_one()
        fake.observation={'turn_id':'remote','remote_status':'completed'}
        daemon.reconcile()
        self.store.schedule()
        self.assertEqual(next(t for t in self.store.status()['tasks'] if t['id']=='dependent')['status'],'pending')
        self.complete(message)
        self.assertEqual(next(t for t in self.store.status()['tasks'] if t['id']=='dependent')['status'],'active')

    def test_delivered_commits_require_integration_before_dependencies(self):
        self.task()
        self.task('dependent',to='A',paths=('other.py',),dependencies=('first',))
        with self.store.transaction() as db:
            message=dict(db.execute("SELECT * FROM messages WHERE task_id='first'").fetchone())
        self.complete(message,commits=['b'*40])
        tasks={t['id']:t for t in self.store.status()['tasks']}
        self.assertEqual(tasks['first']['status'],'delivered')
        self.assertEqual(tasks['dependent']['status'],'pending')

    def test_overlapping_scopes_not_active_together(self):
        self.task(paths=('Src/Shared/',))
        self.assertEqual(self.task('overlap',to='A',paths=('src/shared/file.py',))['status'],'pending')
        self.assertEqual(self.task('independent',to='A',paths=('src/separate.py',))['status'],'active')
        self.assertTrue(scopes_overlap(['src/a*.py'],['src/anything.py']))
        self.assertFalse(scopes_overlap(['src/a.py'],['src/ab.py']))

    def test_invalid_paths_and_missing_dependencies_rejected(self):
        for path in ('../outside','C:\\outside','/root','.git/config','a/../b','src//a'):
            with self.subTest(path=path),self.assertRaises(PairError):self.task(name='invalid',paths=(path,))
        with self.assertRaises(PairError):self.task(dependencies=('missing',))
        self.assertEqual(self.store.status()['tasks'],[])

    def test_timeout_is_unknown_not_retried_even_after_restart(self):
        message=self.message()
        fake=FakeTransport(self.store,DeliveryUnknown('timeout'))
        daemon=Daemon(self.store,fake)
        self.assertTrue(daemon.dispatch_one())
        self.assertEqual(self.state(message['id'])['delivery_status'],'unknown')
        for _ in range(5):self.assertFalse(daemon.dispatch_one())
        restarted=Daemon(self.store,fake);restarted.run(max_ticks=1)
        self.assertEqual(len(fake.calls),1)
        self.assertEqual(self.state(message['id'])['attempts'],1)

    def test_retry_only_on_definite_pre_delivery_failure_and_is_bounded(self):
        message=self.message()
        fake=FakeTransport(self.store,TransportError('not started',retryable=True))
        daemon=Daemon(self.store,fake,max_attempts=3)
        for _ in range(8):
            daemon.dispatch_one()
            with self.store.transaction() as db:db.execute('UPDATE messages SET next_attempt=0')
        self.assertEqual(len(fake.calls),3)
        self.assertEqual(self.state(message['id'])['delivery_status'],'failed')

    def test_persist_before_delivery_and_distinguish_remote_from_business(self):
        message=self.message()
        fake=FakeTransport(self.store);daemon=Daemon(self.store,fake)
        daemon.dispatch_one()
        state=self.state(message['id'])
        self.assertEqual(state['delivery_status'],'accepted')
        self.assertEqual(state['process_status'],'unclaimed')
        fake.observation={'turn_id':state['turn_id'],'remote_status':'completed','result':['模型完成']}
        daemon.reconcile()
        state=self.state(message['id'])
        self.assertEqual(state['remote_status'],'completed')
        self.assertEqual(state['process_status'],'unclaimed')

    def test_unknown_receipt_reconciles_without_resending(self):
        message=self.message()
        fake=FakeTransport(self.store,DeliveryUnknown('after write'))
        daemon=Daemon(self.store,fake);daemon.dispatch_one()
        fake.observation={'turn_id':'recovered','remote_status':'completed'}
        daemon.reconcile()
        self.assertEqual(self.state(message['id'])['turn_id'],'recovered')
        self.assertFalse(daemon.dispatch_one())
        self.assertEqual(len(fake.calls),1)

    def test_daemon_crash_recovery_releases_os_lock_but_never_replays(self):
        message=self.message()
        code='from scripts.pair_core import Store,Daemon,FileLock; import os,sys; s=Store("test",home=sys.argv[1]); lock=FileLock(s.daemon_lock); lock.acquire(); s.recover("crashed"); d=Daemon(s,None); assert d._begin(); os._exit(23)'
        process=subprocess.run([sys.executable,'-c',code,str(self.home)],cwd=Path(__file__).resolve().parents[1],capture_output=True)
        self.assertEqual(process.returncode,23,process.stderr)
        self.assertEqual(self.state(message['id'])['delivery_status'],'delivering')
        fake=FakeTransport(self.store)
        Daemon(self.store,fake).run(max_ticks=1)
        self.assertEqual(self.state(message['id'])['delivery_status'],'unknown')
        self.assertEqual(fake.calls,[])

    def test_daemon_single_instance_and_nonce_liveness(self):
        fake=FakeTransport(self.store);daemon=Daemon(self.store,fake,poll_interval=.01)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            running=executor.submit(daemon.run)
            deadline=time.monotonic()+5
            while not self.store.status()['runtime']['lock_held'] and time.monotonic()<deadline:time.sleep(.01)
            try:
                with self.assertRaises(PairError):Daemon(self.store,FakeTransport()).run(max_ticks=1)
                status=self.store.status(probe_timeout=2)
                self.assertTrue(status['runtime']['lock_held'])
                self.assertTrue(status['runtime']['probe_confirmed'])
            finally:
                self.store.control('stopped')
                running.result(timeout=5)
        self.assertFalse(self.store.status()['runtime']['lock_held'])

    def test_pause_waits_for_dispatch_boundary_and_starts_nothing_after_return(self):
        self.message();self.store.send('B','A','另一个消息')
        entered=threading.Event();release=threading.Event();paused=threading.Event()
        fake=FakeTransport(self.store)
        original=fake.start
        def start(member,message):
            entered.set();self.assertTrue(release.wait(5));return original(member,message)
        fake.start=start
        daemon=Daemon(self.store,fake)
        def pause():
            value=self.store.control('paused');paused.set();return value
        with concurrent.futures.ThreadPoolExecutor() as executor:
            delivery=executor.submit(daemon.dispatch_one)
            self.assertTrue(entered.wait(5))
            request=executor.submit(pause)
            self.assertFalse(paused.wait(.05))
            release.set();delivery.result(timeout=5);request.result(timeout=5)
        before=len(fake.calls)
        for _ in range(3):self.assertFalse(daemon.dispatch_one())
        self.assertEqual(len(fake.calls),before)
        self.store.control('running')
        self.assertTrue(daemon.dispatch_one())

    def test_stop_keeps_records_and_code_and_does_not_claim(self):
        artifact=self.project/'kept.txt';artifact.write_text('用户代码',encoding='utf-8')
        message=self.message()
        self.store.control('stopped')
        fake=FakeTransport();Daemon(self.store,fake).run()
        self.assertEqual(fake.calls,[])
        self.assertFalse(self.claim(message['id'])['claimed'])
        self.assertEqual(artifact.read_text(encoding='utf-8'),'用户代码')
        self.assertEqual(len(self.store.status()['messages']),1)
        self.store.control('running')
        self.assertTrue(self.claim(message['id'])['claimed'])

    def test_no_reply_does_not_release_or_reassign_active_task(self):
        self.task()
        self.task('next',to='B',paths=('another.py',))
        with self.store.transaction() as db:db.execute('UPDATE tasks SET updated=0')
        self.store.schedule()
        tasks={t['id']:t['status'] for t in self.store.status()['tasks']}
        self.assertEqual(tasks,{'first':'active','next':'pending'})

    def test_invalid_session_cwd_and_completion_owner_rejected(self):
        message=self.message()
        with self.assertRaises(TransportError):self.store.claim('B','wrong',self.directories['B'])
        with self.assertRaises(PairError):self.store.claim('B',self.sessions['A'],self.directories['B'])
        with self.assertRaises(PairError):self.store.claim('B',self.sessions['B'],self.directories['A'])
        claimed=self.claim(message['id'])
        with self.assertRaises(PairError):self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claimed['claim_token'],'越权')
        with self.assertRaises(PairError):self.store.complete('B',self.sessions['B'],self.directories['B'],message['id'],'bad-token','错误令牌')

    def test_three_rounds_without_progress_blocks_once_and_notifies_a(self):
        self.task()
        for i in range(3):
            question=self.store.send('A','B','问题 '+str(i),kind='question',task_id='first')
            kwargs=dict(kind='reply',task_id='first',reply_to=question['id'],dedupe_key='reply-'+str(i))
            self.store.send('B','A','仍无进展',**kwargs)
            self.store.send('B','A','仍无进展',**kwargs)
        task=self.store.status()['tasks'][0]
        self.assertEqual(task['status'],'blocked')
        self.assertEqual(task['no_progress_rounds'],3)
        notices=[m for m in self.store.status()['messages'] if m['kind']=='blocked']
        self.assertEqual(len(notices),1);self.assertEqual(notices[0]['recipient'],'A')

    def test_progress_evidence_resets_question_rounds(self):
        self.task()
        for i in range(3):
            q=self.store.send('A','B','问题 '+str(i),kind='question',task_id='first')
            self.store.send('B','A','答复',kind='reply',task_id='first',reply_to=q['id'],progress_note='已定位错误并给出日志' if i==1 else None)
        self.assertEqual(self.store.status()['tasks'][0]['no_progress_rounds'],1)
        self.assertEqual(self.store.status()['tasks'][0]['status'],'active')

    def test_state_outside_repo_and_existing_state_never_overwritten(self):
        with self.assertRaises(PairError):Store.initialize('inside',str(self.project),'a'*40,home=self.project/'bad-home')
        self.assertFalse((self.project/'bad-home').exists())
        self.message()
        with self.assertRaises(PairError):Store.initialize('test',str(self.project),'different',home=self.home)
        self.assertEqual(len(self.store.status()['messages']),1)

    def test_cli_utf8_file_and_json_flags_at_either_position(self):
        body='中文\r\n第二行 "双引号" \'单引号\' C:\\用户\\项目 a\\a.py\r\n$(whoami) & echo x'
        source=self.root/'body.txt';source.write_bytes(body.encode('utf-8'))
        script=str(Path(__file__).resolve().parents[1]/'scripts/pair.py')
        for index in range(2):
            common=['--pair','test','--codex-home',str(self.home)]
            argv=[sys.executable,script]+(['--json']+common if index==0 else [])+['send','--from','A','--to','B','--body-file',str(source)]+(['--json']+common if index else [])
            result=subprocess.run(argv,capture_output=True,encoding='utf-8',check=True)
            self.assertEqual(json.loads(result.stdout)['result']['body'],body)


@unittest.skipUnless(os.getenv('CODEX_PAIR_REAL_CORE_TEST')=='1','需要显式启用真实业务闭环测试')
class RealCoreTests(unittest.TestCase):
    def test_real_daemon_claim_and_complete(self):
        from scripts.pair_core import effective_home
        home=effective_home()
        evidence=Path(os.environ.get('CODEX_PAIR_EVIDENCE',str(home/'codex-pair'/'live-core-validation')))
        evidence.mkdir(parents=True,exist_ok=True)
        root=evidence/('core-project-'+str(time.time_ns()));root.mkdir()
        project=root/'main';project.mkdir()
        def git(*argv):
            return subprocess.run(['git','-C',str(project),*argv],capture_output=True,encoding='utf-8',check=True).stdout.strip()
        git('init','-b','main');git('config','user.name','Codex Pair Test');git('config','user.email','codex-pair-test@local.invalid')
        (project/'seed.txt').write_text('seed\n',encoding='utf-8');git('add','seed.txt');git('commit','-m','test seed')
        base=git('rev-parse','HEAD')
        for member in ('a','b'):git('worktree','add','-b',member,str(root/member),base)
        pair_id='real-core-'+str(time.time_ns())
        store=Store.initialize(pair_id,str(project),base,home=home)
        script=str(Path(__file__).resolve().parents[1]/'scripts/pair.py')
        common=[sys.executable,script,'--json','--pair',pair_id,'--codex-home',str(home)]
        for member in ('A','B'):
            result=subprocess.run(common+['join','--member',member,'--new-session','--worktree',str(root/member.lower())],capture_output=True,encoding='utf-8',timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        store.control('paused')
        store.assign('proof','真实领取完成闭环','B',['probe.txt'],body='本任务仅验证通信，不修改项目代码，不运行 Git。先按 claim_argv 领取，claimed=true 后用 complete 提交实质结果 PAIR_BUSINESS_OK。结果文件写入协作状态目录，不写入 worktree。不要发送收到或谢谢。')
        logs=(evidence/'core-daemon.stdout').open('wb');errors=(evidence/'core-daemon.stderr').open('wb')
        daemon=subprocess.Popen(common+['daemon'],stdout=logs,stderr=errors,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            self.assertTrue(store.status(probe_timeout=5)['runtime']['probe_confirmed'])
            self.assertEqual(store.status()['messages'],[])
            store.control('running')
            deadline=time.monotonic()+180
            status=None
            while time.monotonic()<deadline:
                self.assertIsNone(daemon.poll(),'daemon exited early')
                status=store.status()
                if len(status['messages'])>=2 and all(m['process_status']=='completed' and m['remote_status']=='completed' for m in status['messages']):break
                time.sleep(.25)
            (evidence/'core-result.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
            self.assertTrue(status and len(status['messages'])>=2,status)
            self.assertTrue(all(m['process_status']=='completed' and m['remote_status']=='completed' for m in status['messages']),status)
            self.assertEqual(status['tasks'][0]['status'],'completed')
            self.assertIn('PAIR_BUSINESS_OK',status['tasks'][0]['result'])
            self.assertTrue(all(m['attempts']==1 for m in status['messages']))
        finally:
            store.control('stopped')
            try:daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:daemon.terminate();daemon.wait(timeout=10)
            logs.close();errors.close()
        print('真实业务证据:',evidence/'core-result.json')


if __name__=='__main__':
    unittest.main()
