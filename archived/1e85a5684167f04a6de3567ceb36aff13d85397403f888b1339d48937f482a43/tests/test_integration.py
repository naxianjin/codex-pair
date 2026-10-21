"""真实 Git 的入口测试；可选双 CLI 验收保留完整项目和状态证据。"""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import pair, pair_git
from scripts.pair_core import PairError, Store, effective_home
from scripts.pair_transport import AppServer, codex_command

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/pair.py'


def git(path,*args):
    return subprocess.run(['git','-C',str(path),*args],capture_output=True,encoding='utf-8',check=True).stdout.strip()


def seed(path):
    path.mkdir()
    git(path,'init','-b','main')
    git(path,'config','user.name','Codex Pair Test')
    git(path,'config','user.email','pair-test@local.invalid')
    (path/'shared.txt').write_text('base\n',encoding='utf-8')
    git(path,'add','--','shared.txt');git(path,'commit','-m','base')


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pair-integration-')
        self.root=Path(self.temp.name);self.project=self.root/'project';seed(self.project)
        self.home=self.root/'codex-home';self.home.mkdir()
        self.state_root=self.project/'.codex-pair'/'state'
        self.common=['--pair','integration','--codex-home',str(self.home),'--state-root',str(self.state_root)]
        args=self.arguments('init','--project',str(self.project))
        pair.execute(args)
        self.store=Store('integration',home=self.home,state_root=self.state_root)
        self.trees=self.store.git_context()['worktrees']
        for index,member in enumerate(('A','B'),1):
            info=self.trees[member.lower()]
            session='00000000-0000-0000-0000-'+str(index).zfill(12)
            self.store.join(member,session,info['path'],backend='queue',owned=False,
                            proof={'id':session,'cwd':info['path'],'git':pair.worktree_proof(self.store,member,info['path'])})

    def tearDown(self):
        self.temp.cleanup()

    def arguments(self,*args):
        return pair.build_parser().parse_args(self.common+list(args))

    def commit(self,member,path,content):
        worktree=Path(self.trees[member.lower()]['path'])
        target=worktree/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content,encoding='utf-8')
        git(worktree,'add','--',path);git(worktree,'commit','-m','change '+path)
        return git(worktree,'rev-parse','HEAD')

    def deliver(self,paths,commits):
        self.store.assign('delivery','交付','B',paths)
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        member=self.store.member('B')
        claimed=self.store.claim('B',member['session_uuid'],member['cwd'],message_id=message['id'])
        self.store.complete('B',member['session_uuid'],member['cwd'],message['id'],claimed['claim_token'],'修改与验证已完成',commits=commits)

    def test_init_reuses_valid_worktrees_and_preserves_user_changes(self):
        a=Path(self.trees['a']['path']);(a/'user.txt').write_text('用户修改',encoding='utf-8')
        result=pair.execute(self.arguments('init','--project',str(self.project)))
        self.assertEqual(result['git']['worktrees'],self.trees)
        self.assertEqual((a/'user.txt').read_text(encoding='utf-8'),'用户修改')

    def test_multi_commit_delivery_scope_and_idempotent_integration(self):
        commits=[self.commit('B','src/one.py','one=1\n'),self.commit('B','src/two.py','two=2\n')]
        self.deliver(['src/'],commits)
        first=pair.execute(self.arguments('integrate','--task','delivery'))
        self.assertTrue(first['success'])
        self.assertTrue((Path(self.trees['a']['path'])/'src/two.py').is_file())
        second=pair.execute(self.arguments('integrate','--task','delivery'))
        self.assertEqual(first,second)
        self.assertEqual(self.store.task('delivery')['status'],'integrated')
        self.assertEqual(self.store.status()['pair']['status'],'paused')

    def test_dirty_receiver_preserved_and_invalid_scope_rejected(self):
        commit=self.commit('B','secret.py','secret=1\n')
        self.deliver(['allowed.py'],[commit])
        with self.assertRaisesRegex(PairError,'Delivery rejected'):pair.execute(self.arguments('integrate','--task','delivery'))
        with self.store.transaction() as db:db.execute('UPDATE tasks SET allowed_paths=?',(json.dumps(['secret.py']),))
        user=Path(self.trees['a']['path'])/'user.txt';user.write_text('保留',encoding='utf-8')
        with self.assertRaises(pair_git.GitError):pair.execute(self.arguments('integrate','--task','delivery'))
        self.assertEqual(user.read_text(encoding='utf-8'),'保留')

    def test_current_coordinator_can_integrate_from_own_receipt_turn(self):
        commit=self.commit('B','one.txt','one\n');self.deliver(['one.txt'],[commit])
        receipt=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        a=self.store.member('A')
        self.store.claim('A',a['session_uuid'],a['cwd'],message_id=receipt['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE id=?",(receipt['id'],))
        with self.assertRaises(PairError):pair.execute(self.arguments('integrate','--task','delivery'))
        with patch.dict(os.environ,{'CODEX_THREAD_ID':a['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=Path(a['cwd'])):
            self.assertTrue(pair.execute(self.arguments('integrate','--task','delivery'))['success'])

    def test_transferred_coordinator_b_is_default_integration_receiver(self):
        self.store.setting('coordinator','B')
        commit=self.commit('A','taken-over.txt','remaining work\n')
        self.store.assign('takeover','接管后交付','A',['taken-over.txt'])
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        a=self.store.member('A');b=self.store.member('B')
        claim=self.store.claim('A',a['session_uuid'],a['cwd'],message_id=message['id'])
        self.store.complete('A',a['session_uuid'],a['cwd'],message['id'],claim['claim_token'],'已实现并验证',commits=[commit])
        receipt=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        self.store.claim('B',b['session_uuid'],b['cwd'],message_id=receipt['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE id=?",(receipt['id'],))
        with patch.dict(os.environ,{'CODEX_THREAD_ID':b['session_uuid']}),patch('scripts.pair.Path.cwd',return_value=Path(b['cwd'])):
            result=pair.execute(self.arguments('integrate','--task','takeover'))
        self.assertTrue(result['success'])
        self.assertEqual(Path(result['receiver']).resolve(),Path(b['cwd']).resolve())
        self.assertEqual((Path(b['cwd'])/'taken-over.txt').read_text(encoding='utf-8'),'remaining work\n')

    def conflict(self):
        self.commit('A','shared.txt','A line\n')
        commits=[self.commit('B','shared.txt','B line\n'),self.commit('B','tail.txt','tail\n')]
        self.deliver(['shared.txt','tail.txt'],commits)
        result=pair.execute(self.arguments('integrate','--task','delivery'))
        self.assertEqual(result['status'],'conflict')
        self.assertEqual(result['remaining_commits'],commits[1:])
        return commits

    def test_conflict_continue_finishes_remaining_commits(self):
        self.conflict();a=Path(self.trees['a']['path'])
        (a/'shared.txt').write_text('resolved\n',encoding='utf-8');git(a,'add','--','shared.txt')
        result=pair.execute(self.arguments('integrate','--task','delivery','--continue'))
        self.assertTrue(result['success']);self.assertEqual((a/'tail.txt').read_text(),'tail\n')

    def test_conflict_abort_preserves_original_receiver_commit(self):
        self.conflict();a=Path(self.trees['a']['path'])
        result=pair.execute(self.arguments('integrate','--task','delivery','--abort'))
        self.assertEqual(result['status'],'aborted');self.assertEqual((a/'shared.txt').read_text(),'A line\n')
        self.assertFalse((a/'tail.txt').exists())
        self.assertEqual(self.store.task('delivery')['status'],'delivered')

    def test_recovery_after_git_commit_before_sqlite_receipt_never_reapplies(self):
        commits=[self.commit('B','one.txt','one\n'),self.commit('B','two.txt','two\n')]
        self.deliver(['one.txt','two.txt'],commits)
        original=pair_git.integrate_commits
        def crash_after_git(worktree,requested):
            result=original(worktree,requested)
            self.assertTrue(result['success'])
            raise RuntimeError('simulated process death after git')
        with patch('scripts.pair.pair_git.integrate_commits',side_effect=crash_after_git):
            with self.assertRaises(RuntimeError):pair.execute(self.arguments('integrate','--task','delivery'))
        result=pair.execute(self.arguments('integrate','--task','delivery','--continue'))
        self.assertTrue(result['success'])
        base=self.store.status()['pair']['base_commit']
        self.assertEqual(git(self.trees['a']['path'],'rev-list','--count',base+'..HEAD'),'2')

    def test_invalid_worktree_rejected_before_contacting_codex(self):
        other=self.root/'other';seed(other)
        with patch('scripts.pair.AppServer') as server:
            with self.assertRaises(PairError):pair.execute(self.arguments('join','--member','B','--session','00000000-0000-0000-0000-000000000002','--worktree',str(other)))
            server.assert_not_called()

    def test_delivery_rejects_merge_duplicates_base_and_unsafe_paths(self):
        b=self.trees['b']['path'];base=self.store.status()['pair']['base_commit']
        commit=self.commit('B','ok.txt','ok\n')
        self.assertFalse(pair_git.check_delivery(b,base,[commit,commit],['ok.txt'])['passed'])
        self.assertFalse(pair_git.check_delivery(b,base,[base],['shared.txt'])['passed'])
        for path in ('.git/','*','ok.txt:stream'):
            self.assertFalse(pair_git.check_delivery(b,base,[commit],[path])['passed'])
        git(b,'branch','side',base);git(b,'merge','--no-ff','-m','merge side','side')
        # 侧分支需要独立提交，才能形成真实 merge。
        git(b,'switch','side');self.commit('B','side.txt','side\n')
        git(b,'switch',self.trees['b']['branch']);git(b,'merge','--no-ff','-m','merge side','side')
        merge=git(b,'rev-parse','HEAD')
        self.assertFalse(pair_git.check_delivery(b,base,[merge],['ok.txt','side.txt'])['passed'])


@unittest.skipUnless(os.getenv('CODEX_PAIR_REAL_INTEGRATION')=='1','需要显式启用真实双 CLI 集成验收')
class RealIntegrationTests(unittest.TestCase):
    def test_two_real_clis_parallel_questions_duplicate_restart_and_integration(self):
        home=Path(os.environ.get('CODEX_PAIR_HOME_A',str(effective_home()))).resolve()
        self.assertIn('CODEX_PAIR_HOME_B',os.environ,'双账号验收须显式设置 CODEX_PAIR_HOME_B')
        home_b=Path(os.environ['CODEX_PAIR_HOME_B']).resolve()
        self.assertNotEqual(home,home_b)
        homes={'A':home,'B':home_b}
        evidence=Path(os.environ.get('CODEX_PAIR_EVIDENCE',str(SCRIPT.parents[1]/'.codex-pair'/'archives'/'live-integration')))
        evidence.mkdir(parents=True,exist_ok=True)
        root=evidence/('project-'+str(time.time_ns()));root.mkdir()
        project=root/'main';seed(project)
        state_root=project/'.codex-pair'/'state'
        account_proof={}
        for member,account_home in homes.items():
            with AppServer(codex_home=account_home,cwd=str(project)) as observer:
                account=observer.rpc('account/read',{}).get('account') or {}
                identity=account.get('id') or account.get('email')
                self.assertTrue(identity,'真实验收需要已登录账号')
                account_proof[member]={'home':str(account_home),'fingerprint':hashlib.sha256(identity.encode()).hexdigest(),'type':account.get('type')}
        self.assertNotEqual(account_proof['A']['fingerprint'],account_proof['B']['fingerprint'],'两个 home 须为不同真实账号')
        pair_id='real-integration-'+str(time.time_ns())
        common=[sys.executable,str(SCRIPT),'--json','--pair',pair_id,'--state-root',str(state_root),'--codex-home',str(home)]
        def cli(*args,cwd=None):
            result=subprocess.run(common+list(args),cwd=cwd,capture_output=True,encoding='utf-8',timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            parsed=json.loads(result.stdout);self.assertTrue(parsed['ok'],parsed);return parsed['result']
        initial=cli('init','--project',str(project))
        trees=initial['git']['worktrees']
        for member in ('A','B'):cli('join','--member',member,'--member-codex-home',str(homes[member]),'--new-session','--worktree',trees[member.lower()]['path'])
        store=Store(pair_id,home=home,state_root=state_root)
        goals={
            'A':('implementation','adder.py','实现 adder.py 的 add(a,b)，使用普通 Python 数字加法，支持负数。只修改 adder.py，测试后 git add -- adder.py 并提交；complete 原任务时附 --commit 完整SHA。若收到 B 的接口问题，按协议 claim 问题，send reply 关联原问题（task 使用问题的 task_id），带 progress-note 说明已确定接口，再 complete 问题。不要等待 B 才实现。使用 claim_argv[0] 的 Python 路径。'),
            'B':('tests','test_adder.py','先向 A 发送一条 kind=question，task=tests，询问 add(a,b) 是否支持负数；保留当前任务 claim_token，写在协作状态目录下的文件中，然后结束回合等待 reply。收到 reply 后领取并完成回复消息，继续原任务：只编写 test_adder.py，用 unittest 测试 add(2,3)==5、add(-2,1)==-1。不要写 adder.py 或桩，不整合 A 的分支。源码尚未整合时可先 py_compile 测试文件并说明等待整合后执行；git add -- test_adder.py 后提交，complete 原任务并附 --commit 完整SHA。使用 claim_argv[0] 的 Python 路径。')}
        for member,(task,path,body) in goals.items():
            handoffs=project/'.codex-pair'/'handoffs';handoffs.mkdir(exist_ok=True)
            body_file=handoffs/(task+'.txt');body_file.write_text(body,encoding='utf-8')
            cli('assign','--task',task,'--title',task,'--to',member,'--allow',path,'--body-file',str(body_file))
        log_root=project/'.codex-pair'/'logs';log_root.mkdir(exist_ok=True)
        logs=(log_root/'integration-daemon.stdout').open('wb');errors=(log_root/'integration-daemon.stderr').open('wb')
        def start_daemon():
            return subprocess.Popen(common+['daemon'],stdout=logs,stderr=errors,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        daemon=start_daemon()
        def settled(status):
            return status['messages'] and all(m['process_status']=='completed' and m['remote_status']=='completed' for m in status['messages'])
        def wait_until(predicate,timeout=360):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                self.assertIsNone(daemon.poll(),'daemon exited unexpectedly')
                current=store.status()
                (evidence/'integration-live-status.json').write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf-8')
                self.assertFalse(any(m['delivery_status']=='failed' for m in current['messages']),current['messages'])
                if predicate(current):return current
                time.sleep(.5)
            self.fail('真实验收等待超时（测试失败不代表目标失联），证据: '+str(evidence/'integration-live-status.json'))
        try:
            status=wait_until(lambda s:all(t['status'] in ('delivered','integrated') for t in s['tasks']) and settled(s))
            self.assertTrue(any(m['kind']=='question' for m in status['messages']))
            self.assertTrue(any(m['kind']=='reply' for m in status['messages']))
            # 两个任务确实在重叠时间内启动，而不是串行完成。
            with store.transaction() as db:
                task_messages=[dict(r) for r in db.execute("SELECT * FROM messages WHERE kind='task'")]
                task_ids={m['id'] for m in task_messages}
                starts=[r['created'] for r in db.execute("SELECT data,created FROM events WHERE kind='delivery_started'") if json.loads(r['data'])['message'] in task_ids]
                completed=[r['created'] for r in db.execute("SELECT data,created FROM events WHERE kind='message_completed'") if json.loads(r['data'])['message'] in task_ids]
            self.assertEqual(len(task_messages),2)
            self.assertLess(max(starts),min(completed))
            b_task=next(m for m in task_messages if m['recipient']=='B')
            before_b=git(trees['b']['path'],'rev-parse','HEAD')
            # 外部重复入队同一已完成信封，领取门必须阻止再次开发。
            duplicate=subprocess.run(codex_command()+['queue','--thread',store.member('B')['session_uuid'],'--message',b_task['envelope']],capture_output=True,encoding='utf-8',timeout=30,env=dict(os.environ,CODEX_HOME=str(home_b)))
            self.assertEqual(duplicate.returncode,0,duplicate.stderr)
            duplicate_proof=None
            with AppServer(codex_home=home_b,cwd=trees['b']['path']) as observer:
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    matching=[t for t in observer.turns(store.member('B')['session_uuid']) if any(
                        item.get('type')=='userMessage' and any(part.get('text')==b_task['envelope'] for part in item.get('content',[])) for item in t['items'])]
                    finished=[t for t in matching if t['status']=='completed' and t.get('completedAt') is not None]
                    if len(finished)>=2:duplicate_proof=finished;break
                    time.sleep(.5)
            self.assertIsNotNone(duplicate_proof)
            self.assertEqual(git(trees['b']['path'],'rev-parse','HEAD'),before_b)
            with store.transaction() as db:
                count=db.execute("SELECT count(*) FROM events WHERE kind='message_claimed' AND data LIKE ?",('%'+b_task['id']+'%',)).fetchone()[0]
            self.assertEqual(count,1)
            # 暂停后保留待投递消息，强制结束 daemon，再恢复同一批状态和会话。
            cli('pause')
            pending=store.send('A','B','重启验收：只按协议领取和完成本消息，实质结果为 PAIR_RESTART_OK，不改代码。',dedupe_key='restart-proof')
            time.sleep(.5);self.assertEqual(store.message(pending['id'])['attempts'],0)
            daemon.terminate();daemon.wait(timeout=20)
            daemon=start_daemon()
            self.assertTrue(store.status(probe_timeout=5)['runtime']['probe_confirmed'])
            self.assertEqual(store.message(pending['id'])['attempts'],0)
            cli('resume')
            status=wait_until(settled,180)
            self.assertEqual(store.message(pending['id'])['attempts'],1)
            self.assertEqual(git(trees['b']['path'],'rev-parse','HEAD'),before_b)
            # 测试项目的接收方制造明确的 add/add 冲突，保留现场并显式解决。
            (project/'adder.py').write_text('def add(a, b):\n    raise RuntimeError("fixture conflict")\n',encoding='utf-8')
            git(project,'add','--','adder.py');git(project,'commit','-m','receiver conflict fixture')
            conflict=cli('integrate','--task','implementation','--worktree',str(project))
            self.assertEqual(conflict['status'],'conflict')
            source=git(trees['a']['path'],'show','HEAD:adder.py')+'\n'
            (project/'adder.py').write_text(source,encoding='utf-8');git(project,'add','--','adder.py')
            cli('integrate','--task','implementation','--worktree',str(project),'--continue')
            cli('integrate','--task','tests','--worktree',str(project))
            verification=subprocess.run([sys.executable,'-m','unittest','test_adder','-v'],cwd=project,capture_output=True,encoding='utf-8')
            self.assertEqual(verification.returncode,0,verification.stdout+verification.stderr)
            final=store.status()
            self.assertTrue(all(t['status']=='integrated' for t in final['tasks']))
            final.update(verification={'command':[sys.executable,'-m','unittest','test_adder','-v'],'stdout':verification.stdout,'stderr':verification.stderr},
                         project=str(project),accounts=account_proof,duplicate_turns=[t['id'] for t in duplicate_proof],visible_tui_windows_tested=False,
                         mode='managed',central_dispatcher=True)
            (evidence/'integration-result.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            store.control('stopped')
            try:daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:daemon.terminate();daemon.wait(timeout=10)
            logs.close();errors.close()
        print('真实双 CLI 证据:',evidence/'integration-result.json')


if __name__=='__main__':unittest.main()
