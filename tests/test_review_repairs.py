"""维修清单回归：临时数据库和真实临时 Git，不接触历史协作或调用模型。"""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import Daemon, PairError
from tests.test_core import FakeTransport
from tests.test_handoff_balance import quota
from tests import test_members, test_integration

GPT={'modelProvider':'openai','model':'gpt-test'}
DEEPSEEK={'modelProvider':'deepseek','model':'deepseek-test'}


class ReviewRepairTests(unittest.TestCase):
    setUp=test_members.MembersTests.setUp
    tearDown=test_members.MembersTests.tearDown
    binding=test_members.MembersTests.binding
    add=test_members.MembersTests.add
    assign=test_members.MembersTests.assign

    def source(self, *, paths=('work.txt',)):
        with self.store.transaction() as db:
            db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(DEEPSEEK),'B'))
        self.assign('work','B',list(paths))
        message,owned=self.claim_task('work')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'无文件分析结论和验证依据')
        return next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))

    def claim_task(self,task):
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task' and m['task_id']==task)
        return message,self.store.claim(*self.binding(message['recipient']),message_id=message['id'])

    def ack(self,member):
        page=self.store.read_delivery(*self.binding(member),'delivery:work')
        self.assertIsNone(page['next_offset'])
        self.store.ack_delivery(*self.binding(member),'delivery:work',page['digest'])
        return page['digest']

    def version(self):
        return next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')

    def test_claimed_old_review_cannot_authorize_new_version(self):
        self.add('C');self.add('D')
        old=self.source();self.claim_task(old['id']);self.ack('C')
        self.store.record_contribution(*self.binding('D'),'work',GPT)
        self.ack('C')
        with self.assertRaisesRegex(ValueError,'Claimed review task required'):
            self.store.review_work(*self.binding('C'),'work',self.version(),'passed','审核V2',GPT)

    def test_new_version_requires_new_delivery_ack(self):
        self.add('C');self.add('D')
        self.claim_task(self.source()['id']);old=self.ack('C')
        self.store.record_contribution(*self.binding('D'),'work',GPT)
        page=self.store.read_delivery(*self.binding('C'),'delivery:work')
        self.assertNotEqual(page['digest'],old)
        with self.store.transaction() as db,self.assertRaisesRegex(PairError,'acknowledgement'):
            self.store._require_ack(db,'C','delivery:work')

    def test_pending_obsolete_review_is_closed_without_claim(self):
        self.add('C');self.add('D')
        self.assign('busy','C',['busy.txt'])
        self.store.setting('availability:D',{'state':'unreachable','observed_at':time.time()})
        old=self.source();self.assertEqual(old['status'],'pending')
        self.store.setting('availability:D',{'state':'accepted','observed_at':time.time()})
        self.store.record_contribution(*self.binding('C'),'work',GPT)
        self.store.schedule()
        self.assertEqual(self.store.task(old['id'])['status'],'completed')
        new=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-') and t['id']!=old['id'])
        self.assertEqual(new['assignee'],'D')
        message,owned=self.claim_task(new['id']);self.ack('D')
        self.store.review_work(*self.binding('D'),'work',self.version(),'passed','审核当前版本',GPT)
        self.store.complete(*self.binding('D'),message['id'],owned['claim_token'],'当前审核完成')
        with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))
        busy,owned=self.claim_task('busy')
        self.store.complete(*self.binding('C'),busy['id'],owned['claim_token'],'占用任务完成')
        for notice in self.store.status()['messages']:
            if notice['kind']!='result' or notice['process_status']=='completed':continue
            claimed=self.store.claim(*self.binding('A'),message_id=notice['id'])
            delivery='delivery:'+notice['task_id']
            page=self.store.read_delivery(*self.binding('A'),delivery)
            self.store.ack_delivery(*self.binding('A'),delivery,page['digest'])
            self.store.complete(*self.binding('A'),notice['id'],claimed['claim_token'],'已读取确认')
        self.store.setting('window_flow',{'goal':'核验过期审核不阻塞目标结束'})
        self.store.setting('scheduling_policy','quota-balanced')
        self.store.schedule()
        plan=next(t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-'))
        notice,owned=self.claim_task(plan['id'])
        with self.store.transaction() as db:
            records=[r[0] for r in db.execute("SELECT d.id FROM dependencies p JOIN deliveries d ON d.task_id=p.depends_on WHERE p.task_id=?",(plan['id'],))]
        for delivery in records:
            page=self.store.read_delivery(*self.binding('A'),delivery)
            self.store.ack_delivery(*self.binding('A'),delivery,page['digest'])
        self.store.complete(*self.binding('A'),notice['id'],owned['claim_token'],'全部成果及当前审核已验收',goal_complete=True)
        self.assertTrue(self.store.setting('goal_completed'))

    def test_pending_review_moves_to_available_independent_gpt(self):
        self.add('C');self.add('D')
        self.assign('busy','C',['busy.txt'])
        self.assign('busy-d','D',['busy-d.txt'])
        review=self.source();self.assertEqual(review['assignee'],'C')
        self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
        self.store.schedule()
        self.assertEqual(self.store.task(review['id'])['assignee'],'D')
        self.assertEqual(self.store.task(review['id'])['status'],'pending')

    def test_active_unsent_review_moves_with_message_and_regenerates_envelope(self):
        self.add('C');self.add('D')
        review=self.source();self.assertEqual(review['assignee'],'C')
        message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        self.assertEqual(review['status'],'active')
        self.assertEqual((message['delivery_status'],message['process_status'],message['attempts']),('pending','unclaimed',0))
        envelope=self.store.envelope(self.store.member('C'),self.store.message(message['id']))
        with self.store.transaction() as db:db.execute('UPDATE messages SET envelope=? WHERE id=?',(envelope,message['id']))
        self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
        self.store.schedule()
        moved=self.store.message(message['id'])
        self.assertEqual(self.store.task(review['id'])['assignee'],'D')
        self.assertEqual(moved['recipient'],'D')
        self.assertEqual(moved['dedupe_key'],'task:'+review['id'])
        self.assertIsNone(moved['envelope'])
        self.assertFalse(self.store.claim(*self.binding('C'),message_id=message['id'])['claimed'])
        # 接收方改变后，同一逻辑消息的去重指纹也必须匹配。
        self.assertEqual(self.store.send(moved['sender'],'D',moved['body'],kind='task',task_id=review['id'],dedupe_key=moved['dedupe_key'])['id'],message['id'])
        transport=FakeTransport(self.store)
        daemon=Daemon(self.store,transport,member_id='D')
        self.assertTrue(daemon.dispatch_one())
        member,delivered=transport.calls[0]
        self.assertEqual(member['id'],'D')
        payload=json.loads(delivered['envelope'])
        self.assertEqual(payload['recipient'],'D')
        self.assertEqual(payload['actor_cwd'],self.store.member('D')['cwd'])
        claimed=self.store.claim(*self.binding('D'),message_id=message['id'])
        self.assertTrue(claimed['claimed'])

    def test_active_unsent_review_waits_if_new_reviewer_has_other_work(self):
        self.add('C');self.add('D')
        self.assign('busy-d','D',['busy-d.txt'])
        busy,owned=self.claim_task('busy-d')
        review=self.source()
        self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
        self.store.schedule()
        self.assertEqual(self.store.task(review['id'])['assignee'],'D')
        self.assertEqual(self.store.task(review['id'])['status'],'pending')
        message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        self.assertEqual(message['recipient'],'D')
        self.assertFalse(self.store.claim(*self.binding('D'),message_id=message['id'])['claimed'])
        self.store.complete(*self.binding('D'),busy['id'],owned['claim_token'],'原任务已结束')
        self.assertEqual(self.store.task(review['id'])['status'],'active')
        self.assertTrue(self.store.claim(*self.binding('D'),message_id=message['id'])['claimed'])

    def test_review_with_transport_evidence_is_not_reassigned(self):
        evidence=[{'delivery_status':state,'attempts':1} for state in ('accepted','delivering','unknown','retry')]
        evidence.extend({field:value} for field,value in (('queue_id','queued'),('turn_id','turn'),('remote_status','queued'),('claim_token','retained-token')))
        for index,record in enumerate(evidence):
            with self.subTest(record=record):
                if index:self.tearDown();self.setUp()
                self.add('C');self.add('D')
                review=self.source()
                with self.store.transaction() as db:
                    db.execute('UPDATE messages SET '+','.join(field+'=?' for field in record)+' WHERE task_id=?',(*record.values(),review['id']))
                self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
                self.store.schedule()
                self.assertEqual(self.store.task(review['id'])['assignee'],'C')
                message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
                self.assertEqual(message['recipient'],'C')
                self.assertEqual(message['delivery_status'],record.get('delivery_status','pending'))

    def test_claimed_obsolete_review_closes_without_new_version_ack(self):
        self.add('C');self.add('D')
        review=self.source();message,owned=self.claim_task(review['id']);self.ack('C')
        self.store.record_contribution(*self.binding('D'),'work',GPT)
        self.assertEqual(self.store.task(review['id'])['status'],'active')
        self.store.complete(*self.binding('C'),message['id'],owned['claim_token'],'旧版本已过期，仅收尾，不登记审核通过')
        self.assertEqual(self.store.task(review['id'])['status'],'completed')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'work'))

    def test_obsolete_unknown_review_is_not_automatically_closed(self):
        self.add('C');self.add('D')
        review=self.source()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='unknown',attempts=1 WHERE task_id=?",(review['id'],))
        self.store.record_contribution(*self.binding('D'),'work',GPT)
        self.store.schedule()
        self.assertEqual(self.store.task(review['id'])['status'],'active')
        message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        self.assertEqual(message['delivery_status'],'unknown')
        self.assertEqual(message['process_status'],'unclaimed')

    def test_pending_review_moves_when_reviewer_quota_is_exhausted(self):
        self.add('C');self.add('D')
        for member in ('C','D'):
            home=self.root/('home-'+member);home.mkdir()
            with self.store.transaction() as db:db.execute('UPDATE members SET codex_home=? WHERE id=?',(str(home),member))
        self.assign('busy','C',['busy.txt']);self.assign('busy-d','D',['busy-d.txt'])
        review=self.source();self.assertEqual(review['assignee'],'C')
        self.store.save_quota('C',quota(0,pool='C'))
        self.store.save_quota('D',quota(80,pool='D'))
        self.store.schedule()
        self.assertEqual(self.store.task(review['id'])['assignee'],'D')

    def test_pending_review_moves_when_reviewer_loses_gpt_identity(self):
        self.add('C');self.add('D')
        self.assign('busy','C',['busy.txt']);self.assign('busy-d','D',['busy-d.txt'])
        review=self.source()
        with self.store.transaction() as db:db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(DEEPSEEK),'C'))
        self.store.schedule()
        self.assertEqual(self.store.task(review['id'])['assignee'],'D')

    def test_review_reassignment_does_not_move_claimed_or_unknown(self):
        for unknown in (False,True):
            with self.subTest(unknown=unknown):
                # 独立状态，避免上一项审核干扰。
                if self.store.member_ids()==['A','B']:self.add('C');self.add('D')
                review=self.source() if not unknown else review
                message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
                if not unknown:self.store.claim(*self.binding('C'),message_id=message['id'])
                with self.store.transaction() as db:
                    if unknown:db.execute("UPDATE messages SET process_status='unclaimed',claim_token=NULL,delivery_status='unknown',attempts=1 WHERE id=?",(message['id'],))
                self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
                self.store.schedule()
                self.assertEqual(self.store.task(review['id'])['assignee'],'C')

    def test_text_question_reply_still_works_without_file_artifact(self):
        question=self.store.send('A','B','不修改文件，解释边界条件',kind='question')
        owned=self.store.claim(*self.binding('B'),message_id=question['id'])
        reply=self.store.send('B','A','仅讨论，不交付文件',kind='reply',reply_to=question['id'],progress_note='已解释')
        self.store.complete(*self.binding('B'),question['id'],owned['claim_token'],'问题已回复')
        received=self.store.claim(*self.binding('A'),message_id=reply['id'])
        self.store.complete(*self.binding('A'),reply['id'],received['claim_token'],'答复已理解')
        self.assertEqual(self.store.message(reply['id'])['process_status'],'completed')

    def test_raised_reserve_blocks_immediate_claim_dispatch_and_pool(self):
        for adjustment in (False,True):
            with self.subTest(adjustment=adjustment):
                if adjustment:
                    self.tearDown();self.setUp()
                self.store.configure_assistance(*self.binding('A'),checkpoints=True)
                for m in ('A','B'):self.store.save_quota(m,quota(15,pool='shared'))
                self.store.assign('before','待领取业务','B',['before.txt'])
                message=next(m for m in self.store.status()['messages'] if m['task_id']=='before')
                if adjustment:self.store.adjust_reserve(*self.binding('A'),'B',20,'用户明确上调')
                else:self.store.suggest_reserve(*self.binding('B'),20,'现场交接需要更多余量')
                self.assertFalse(self.store.claim(*self.binding('B'),message_id=message['id'])['claimed'])
                self.assertEqual(self.store.assign('after','同池业务','A',['after.txt'])['status'],'pending')
                transport=FakeTransport(self.store)
                self.assertFalse(Daemon(self.store,transport).dispatch_one())
                self.assertFalse(transport.calls)


class ReviewArtifactTests(unittest.TestCase):
    setUp=test_integration.IntegrationTests.setUp
    tearDown=test_integration.IntegrationTests.tearDown
    commit=test_integration.IntegrationTests.commit
    arguments=test_integration.IntegrationTests.arguments

    def binding(self,m):
        member=self.store.member(m)
        return m,member['session_uuid'],member['cwd']

    def prepare(self):
        with self.store.transaction() as db:
            db.execute('UPDATE members SET proof=json_set(proof,\'$.modelProvider\',\'deepseek\',\'$.model\',\'deepseek-test\') WHERE id=\'B\'')
            db.execute('UPDATE members SET proof=json_set(proof,\'$.modelProvider\',\'openai\',\'$.model\',\'gpt-test\') WHERE id=\'A\'')
        self.store.assign('work','文件修改','B',['code.txt'])
        message=next(m for m in self.store.status()['messages'] if m['task_id']=='work')
        owned=self.store.claim(*self.binding('B'),message_id=message['id'])
        return message,owned

    def passed_work(self, *, path='code.txt',scope='code.txt'):
        message,owned=self.prepare()
        with self.store.transaction() as db:db.execute('UPDATE tasks SET allowed_paths=? WHERE id=?',(json.dumps([scope]),'work'))
        commit=self.commit('B',path,'checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        claimed=self.store.claim(*self.binding('A'),message_id=notice['id'])
        page=self.store.read_delivery(*self.binding('A'),'delivery:work')
        self.store.ack_delivery(*self.binding('A'),'delivery:work',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.store.review_work(*self.binding('A'),'work',version,'passed','检查具体文件差异和提交测试',GPT)
        self.store.complete(*self.binding('A'),notice['id'],claimed['claim_token'],'审核结束')
        return version,page['digest']

    def test_unrelated_commit_keeps_passed_review_and_allows_integration(self):
        version,digest=self.passed_work()
        self.commit('B','unrelated.txt','another task\n')
        with self.store.transaction() as db:
            self.assertTrue(self.store._review_clear(db,'work'))
            self.assertEqual(self.store._current_work_version(db,db.execute("SELECT * FROM tasks WHERE id='work'").fetchone()),version)
        self.assertEqual(self.store.read_delivery(*self.binding('A'),'delivery:work')['digest'],digest)
        from scripts import pair
        self.assertTrue(pair.execute(self.arguments('integrate','--task','work'))['success'])
        self.store.control('running')
        self.assertEqual(self.store.assign('after','后续任务','A',['after.txt'],dependencies=['work'])['status'],'active')

    def test_temporary_artifact_change_waits_and_restores_same_review(self):
        message,owned=self.prepare()
        commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        path=Path(self.trees['b']['path'])/'code.txt'
        path.write_text('temporary\n',encoding='utf-8')
        self.store.control('running')
        self.assertEqual(self.store.task(review['id'])['status'],'active')
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM messages WHERE id=?',(notice['id'],)).fetchone()
            self.assertFalse(self.store._assistance_dispatchable(db,row))
        self.assertFalse(self.store.claim(*self.binding('A'),message_id=notice['id'])['claimed'])
        path.write_text('checked\n',encoding='utf-8')
        self.store.control('running')
        self.assertEqual(len([t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-')]),1)
        resumed=self.store.claim(*self.binding('A'),message_id=notice['id'])
        self.assertTrue(resumed['claimed'])
        page=self.store.read_delivery(*self.binding('A'),'delivery:work')
        self.store.ack_delivery(*self.binding('A'),'delivery:work',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.store.review_work(*self.binding('A'),'work',version,'passed','恢复后检查原提交',GPT)
        self.store.complete(*self.binding('A'),notice['id'],resumed['claim_token'],'恢复审核完成')
        with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))

    def test_legacy_unsent_auto_closed_review_recovers_without_new_message(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?",('过期审核未投递，安全收尾；不代表审核通过',review['id']))
            db.execute("UPDATE messages SET process_status='completed',delivery_status='failed',error=? WHERE id=?",('Stale review closed before delivery / 过期审核在投递前收尾',notice['id']))
        self.store.control('running')
        self.assertEqual(self.store.task(review['id'])['status'],'active')
        self.assertTrue(self.store.claim(*self.binding('A'),message_id=notice['id'])['claimed'])

    def test_claimed_review_cannot_close_during_temporary_change(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        claim=self.store.claim(*self.binding('A'),message_id=notice['id'])
        path=Path(self.trees['b']['path'])/'code.txt';path.write_text('temporary\n',encoding='utf-8')
        self.store.control('running')
        with self.assertRaisesRegex(PairError,'Review artifact changed'):
            self.store.complete(*self.binding('A'),notice['id'],claim['claim_token'],'不能伪装过期关闭')
        path.write_text('checked\n',encoding='utf-8');self.store.control('running')
        self.assertEqual(self.store.message(notice['id'])['claim_token'],claim['claim_token'])
        self.assertFalse(self.store.claim(*self.binding('A'),message_id=notice['id'])['claimed'])

    def test_pending_review_survives_temporary_change(self):
        message,owned=self.prepare()
        self.store.assign('busy','占用审核者','A',['busy.txt'])
        commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        path=Path(self.trees['b']['path'])/'code.txt';path.write_text('temporary\n',encoding='utf-8')
        self.store.control('running')
        self.assertEqual(self.store.task(review['id'])['status'],'pending')
        path.write_text('checked\n',encoding='utf-8');self.store.control('running')
        self.assertEqual(self.store.task(review['id'])['status'],'pending')
        self.assertEqual(len([t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-')]),1)

    def test_legacy_auto_closed_review_with_transport_evidence_is_not_replayed(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?",('过期审核未投递，安全收尾；不代表审核通过',review['id']))
            db.execute("UPDATE messages SET process_status='completed',delivery_status='failed',attempts=1,error=? WHERE task_id=?",('Stale review closed before delivery / 过期审核在投递前收尾',review['id']))
        self.store.control('running')
        self.assertEqual(self.store.task(review['id'])['status'],'completed')

    def test_unrelated_untracked_and_staged_files_do_not_invalidate_review(self):
        _,digest=self.passed_work()
        workspace=Path(self.trees['b']['path'])
        for name in ('unrelated.txt','code.txt.extra'):
            with self.subTest(name=name):
                (workspace/name).write_text('unrelated\n',encoding='utf-8')
                with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))
                test_integration.git(workspace,'add','--',name)
                with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))
        self.assertEqual(self.store.read_delivery(*self.binding('A'),'delivery:work')['digest'],digest)

    def test_related_staged_change_cannot_be_hidden_by_reverting_working_file(self):
        self.passed_work()
        workspace=Path(self.trees['b']['path'])
        (workspace/'code.txt').write_text('staged change\n',encoding='utf-8')
        test_integration.git(workspace,'add','--','code.txt')
        (workspace/'code.txt').write_text('checked\n',encoding='utf-8')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'work'))

    def test_directory_scope_covers_new_ignored_files_but_not_similar_prefix(self):
        self.passed_work(path='src/code.py',scope='src/')
        workspace=Path(self.trees['b']['path'])
        self.commit('B','src-other/other.py','outside\n')
        with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))
        info=Path(test_integration.git(workspace,'rev-parse','--git-common-dir'))
        if not info.is_absolute():info=workspace/info
        with (info/'info/exclude').open('a',encoding='utf-8') as exclude:exclude.write('\n*.ignored\n')
        (workspace/'src/new.ignored').write_text('new related file\n',encoding='utf-8')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'work'))

    def test_integration_artifact_cli_accepts_unrelated_commit(self):
        from scripts import pair
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.setting('review-artifact:work',{'workspace':self.trees['b']['path'],'head':commit,'integration':'fixture'})
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已固定整合产物',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        claimed=self.store.claim(*self.binding('A'),message_id=notice['id'])
        page=self.store.read_delivery(*self.binding('A'),'delivery:work')
        self.store.ack_delivery(*self.binding('A'),'delivery:work',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.commit('B','unrelated.txt','unrelated\n')
        args=self.arguments('review-work','--member','A','--session',self.binding('A')[1],
            '--worktree',self.binding('A')[2],'--task','work','--version',version,'--decision','passed','--body','独立检查范围内产物')
        with patch('scripts.pair.validate_actor'),patch('scripts.pair.AppServer') as observer:
            observer.return_value.__enter__.return_value.validate.return_value=GPT
            self.assertEqual(pair.execute(args)['decision'],'passed')
        self.store.complete(*self.binding('A'),notice['id'],claimed['claim_token'],'整合产物审核结束')
        with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))

    def test_uncommitted_file_delivery_cannot_enter_review(self):
        message,owned=self.prepare()
        path=Path(self.trees['b']['path'])/'code.txt'
        path.write_text('未提交实现',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Committed artifact required'):
            self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'未提交代码交付')
        self.assertEqual(path.read_text(encoding='utf-8'),'未提交实现')
        self.assertEqual(self.store.message(message['id'])['process_status'],'claimed')

    def test_reviewed_committed_artifact_changed_blocks_downstream(self):
        message,owned=self.prepare()
        commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        claim=self.store.claim(*self.binding('A'),message_id=notice['id'])
        page=self.store.read_delivery(*self.binding('A'),'delivery:work')
        self.store.ack_delivery(*self.binding('A'),'delivery:work',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.store.review_work(*self.binding('A'),'work',version,'passed','核验提交和测试',GPT)
        self.store.complete(*self.binding('A'),notice['id'],claim['claim_token'],'审核任务收尾')
        with self.store.transaction() as db:self.assertTrue(self.store._review_clear(db,'work'))
        path=Path(self.trees['b']['path'])/'code.txt'
        path.write_text('审核后变化\n',encoding='utf-8')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'work'))
        self.store.assign('after','后续验收','A',['after.txt'],dependencies=['work'])
        self.assertEqual(self.store.task('after')['status'],'pending')
        from scripts import pair
        with self.assertRaisesRegex(PairError,'Independent review required'):
            pair.execute(self.arguments('integrate','--task','work'))

    def test_dirty_artifact_cannot_be_reviewed_or_reuse_delivery_ack(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        notice=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        self.store.claim(*self.binding('A'),message_id=notice['id'])
        page=self.store.read_delivery(*self.binding('A'),'delivery:work')
        self.store.ack_delivery(*self.binding('A'),'delivery:work',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        (Path(self.trees['b']['path'])/'code.txt').write_text('变化\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Review version changed'):
            self.store.review_work(*self.binding('A'),'work',version,'passed','不能沿用',GPT)
        with self.store.transaction() as db,self.assertRaisesRegex(PairError,'acknowledgement'):
            self.store._require_ack(db,'A','delivery:work')

    def test_uncommitted_deletion_is_not_misclassified_as_text_delivery(self):
        message,owned=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE tasks SET allowed_paths='[\"shared.txt\"]' WHERE id='work'")
        (Path(self.trees['b']['path'])/'shared.txt').unlink()
        with self.assertRaisesRegex(ValueError,'Committed artifact required'):
            self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'删除文件尚未提交')

    def test_committed_head_change_invalidates_review_without_registration(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.commit('B','code.txt','new version\n')
        with self.store.transaction() as db:
            self.assertNotEqual(self.store._current_work_version(db,db.execute("SELECT * FROM tasks WHERE id='work'").fetchone()),version)

    def test_uncommitted_contribution_still_invalidates_previous_review(self):
        message,owned=self.prepare();commit=self.commit('B','code.txt','checked\n')
        self.store.complete(*self.binding('B'),message['id'],owned['claim_token'],'已提交实现',commits=[commit])
        (Path(self.trees['b']['path'])/'code.txt').write_text('未提交的新贡献\n',encoding='utf-8')
        self.store.record_contribution(*self.binding('A'),'work',DEEPSEEK)
        row=next(r for r in self.store.status()['work_reviews'] if r['task_id']=='work')
        self.assertIn('A',json.loads(row['contributors']))
        self.assertEqual(row['status'],'pending')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'work'))


if __name__=='__main__':unittest.main()
