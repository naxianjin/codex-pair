from tests.schema_fixture import remove_team_extensions
"""隔离 SQLite 的审核者生命周期测试，不冒充真实隐藏 CLI 验收。"""
import concurrent.futures
from contextlib import closing
import json
import sqlite3
import unittest

from scripts.pair_core import PairError, Store
from tests import test_core


class ReviewerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown

    def parent(self, **changes):
        with self.store.transaction() as db:
            p = json.loads(db.execute("SELECT proof FROM members WHERE id='A'").fetchone()[0])
            p.update(modelProvider='openai', model='gpt-6.1-sol')
            p.update(changes)
            db.execute("UPDATE members SET proof=? WHERE id='A'", (json.dumps(p),))

    def reserve(self, **kwargs):
        return self.store.reserve_reviewer('A',self.sessions['A'],self.directories['A'],**kwargs)

    def ready(self):
        self.parent()
        ticket = self.reserve()
        proof = {'id':'00000000-0000-0000-0000-000000000099', 'cwd':self.directories['A'],
                 'modelProvider':'openai', 'model':'gpt-6.1-sol', 'ephemeral':False}
        self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],
                                epoch=ticket['epoch'],attempt=ticket['attempt'],proof=proof,
                                reviewer_cwd=self.directories['A'])
        return ticket, proof

    def test_hidden_reviewer_does_not_consume_visible_member_or_approval_role(self):
        ticket, proof = self.ready()
        status = self.store.reviewer_status()
        self.assertEqual(status['reviewer']['session_uuid'],proof['id'])
        self.assertEqual([r['id'] for r in self.store.status()['members']],['A','B'])
        self.assertFalse(status['visible_seat'])
        self.assertFalse(status['approval_authority'])
        self.assertFalse(status['release_gate_implemented'])
        self.assertNotIn('attempt',status['reviewer'])
        self.assertNotIn('owner',status['reviewer'])

    def test_only_original_official_gpt_a_can_reserve(self):
        self.parent(modelProvider='deepseek')
        with self.assertRaisesRegex(PairError,'Official GPT'): self.reserve()
        self.parent(model='not-gpt')
        with self.assertRaisesRegex(PairError,'Persistent GPT'): self.reserve()
        self.parent()
        with self.assertRaisesRegex(PairError,'Original A'):
            self.store.reserve_reviewer('B',self.sessions['B'],self.directories['B'])

    def test_creation_intent_has_single_atomic_winner(self):
        self.parent()
        def reserve(_):
            try: self.reserve(); return True
            except PairError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(4))),1)

    def test_unknown_create_never_automatically_creates_another_session(self):
        self.parent(); t=self.reserve()
        self.assertTrue(self.store.reviewer_creation_unknown(t['epoch'],t['attempt'])['recorded'])
        with self.assertRaisesRegex(PairError,'already reserved'): self.reserve()
        with self.assertRaisesRegex(PairError,'Retirement evidence'): self.reserve(replacement=True,evidence='超时不是结束')
        self.assertEqual(self.store.reviewer_status()['reviewer']['state'],'unknown')

    def test_wrong_model_or_visible_session_or_wrong_worktree_never_binds(self):
        self.parent(); t=self.reserve()
        base={'id':'00000000-0000-0000-0000-000000000099','cwd':self.directories['A'],
              'modelProvider':'openai','model':'gpt-6.1-sol'}
        for changes in ({'modelProvider':'deepseek'},{'model':'gpt-5.6-sol'},
                        {'id':self.sessions['A']},{'id':self.sessions['B']},
                        {'cwd':self.directories['B']},{'ephemeral':True}):
            with self.subTest(changes=changes), self.assertRaises(PairError):
                self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],
                    epoch=t['epoch'],attempt=t['attempt'],proof=dict(base,**changes),reviewer_cwd=self.directories['A'])

    def test_single_lease_idle_process_does_not_hold_business_task(self):
        t,p=self.ready()
        lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        with self.assertRaisesRegex(PairError,'lease held'): self.store.acquire_reviewer(t['epoch'],p['id'],p)
        task=self.store.assign('independent','不依赖审核的独立任务','B',['safe.txt'])
        self.assertEqual(task['status'],'active')
        result=self.store.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='completed',evidence='回合有持久化完成记录')
        self.assertFalse(result['work_approved'])
        self.assertEqual(result['state'],'idle')

    def test_unknown_turn_not_replayed_and_cannot_be_replaced_while_running(self):
        t,p=self.ready(); lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        with self.assertRaisesRegex(PairError,'unresolved'):
            self.store.retire_reviewer('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],execution_resolved=True,evidence='没有回执')
        self.store.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='unknown',evidence='回执遗失，保持结果未知')
        with self.assertRaisesRegex(PairError,'Stale'): self.store.acquire_reviewer(t['epoch'],p['id'],p)

    def test_replacement_fences_late_identity_and_old_owner(self):
        t,p=self.ready(); lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        self.store.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='completed',evidence='完成')
        self.store.retire_reviewer('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],execution_resolved=True,evidence='已核实原回合结束及证据已保留')
        new=self.reserve(replacement=True,evidence='明确重建同配置')
        self.assertEqual(new['epoch'],2)
        with self.assertRaisesRegex(PairError,'Stale'): self.store.acquire_reviewer(t['epoch'],p['id'],p)
        with self.assertRaisesRegex(PairError,'mismatch'):
            self.store.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='completed',evidence='迟到')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reviewer_history').fetchone()[0],1)

    def test_parent_model_change_revokes_next_acquire(self):
        t,p=self.ready();self.parent(model='gpt-5.6-sol')
        with self.assertRaisesRegex(PairError,'configuration changed'): self.store.acquire_reviewer(t['epoch'],p['id'],p)

    def test_hidden_session_cannot_join_visible_member_after_binding(self):
        with self.store.transaction() as db: db.execute("DELETE FROM members WHERE id='B'")
        t,p=self.ready()
        with self.assertRaisesRegex(PairError,'Hidden reviewer'):
            self.store.join('B',p['id'],p['cwd'],backend='queue',owned=False,proof=p)

    def test_binding_visible_and_hidden_session_race_has_single_winner(self):
        self.parent()
        with self.store.transaction() as db: db.execute("DELETE FROM members WHERE id='B'")
        t=self.reserve()
        p={'id':'00000000-0000-0000-0000-000000000099','cwd':self.directories['A'],
           'modelProvider':'openai','model':'gpt-6.1-sol'}
        def bind(kind):
            try:
                if kind=='visible':
                    self.store.join('B',p['id'],p['cwd'],backend='queue',owned=False,proof=p)
                else:
                    self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],attempt=t['attempt'],proof=p,reviewer_cwd=p['cwd'])
                return True
            except PairError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(bind,('hidden','visible'))),1)

    def test_lost_lease_receipt_can_only_be_reconciled_by_a_for_exact_execution(self):
        t,p=self.ready();lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        public=self.store.reviewer_status()['reviewer']
        self.assertNotIn('owner',public)
        with self.assertRaisesRegex(PairError,'Original A'):
            self.store.reconcile_reviewer_execution('B',self.sessions['B'],self.directories['B'],epoch=t['epoch'],reviewer_session=p['id'],execution_id=public['execution_id'],outcome='completed',evidence='已核对准确回合完成')
        with self.assertRaises(PairError):
            self.store.reconcile_reviewer_execution('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],reviewer_session=p['id'],execution_id='other',outcome='completed',evidence='错误执行版本')
        with self.assertRaises(PairError):
            self.store.reconcile_reviewer_execution('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],reviewer_session=p['id'],execution_id=public['execution_id'],outcome='unknown',evidence='无回执不是结束证据')
        result=self.store.reconcile_reviewer_execution('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],reviewer_session=p['id'],execution_id=public['execution_id'],outcome='completed',evidence='官方持久化记录已匹配该会话、执行ID与回合结束；原工具副作用已核查')
        self.assertEqual(result['state'],'idle')
        self.assertFalse(result['replayed'])
        with self.assertRaisesRegex(PairError,'mismatch'):
            self.store.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='completed',evidence='旧owner迟到')
        new=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        with self.assertRaises(PairError):
            self.store.reconcile_reviewer_execution('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],reviewer_session=p['id'],execution_id=public['execution_id'],outcome='completed',evidence='旧对账不能结束新执行')
        self.assertNotEqual(new['owner'],lease['owner'])

    def test_stop_pause_and_goal_completion_block_creation_or_acquire(self):
        self.parent()
        for state in ('paused','stopped'):
            self.store.control(state)
            with self.assertRaisesRegex(PairError,'not running'): self.reserve()
        self.store.control('running');t,p=self.ready()
        self.store.control('paused')
        with self.assertRaisesRegex(PairError,'not running'): self.store.acquire_reviewer(t['epoch'],p['id'],p)
        self.store.control('running');self.store.setting('goal_completed',True)
        with self.assertRaisesRegex(PairError,'Goal completed'): self.store.acquire_reviewer(t['epoch'],p['id'],p)

    def test_late_create_after_stop_cannot_activate(self):
        self.parent();t=self.reserve();self.store.control('stopped')
        p={'id':'00000000-0000-0000-0000-000000000099','cwd':self.directories['A'],
           'modelProvider':'openai','model':'gpt-6.1-sol'}
        with self.assertRaisesRegex(PairError,'not running'):
            self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],attempt=t['attempt'],proof=p,reviewer_cwd=self.directories['A'])

    def test_ten_minute_reminder_never_reclaims_or_fails_task(self):
        t,p=self.ready();lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        initial=self.store.reviewer_status()['reviewer']['last_progress']
        self.assertFalse(self.store.reviewer_reminder(now=initial+599)['notify'])
        self.assertTrue(self.store.reviewer_reminder(now=initial+600)['notify'])
        self.assertFalse(self.store.reviewer_reminder(now=initial+999)['notify'])
        self.assertEqual(self.store.reviewer_status()['reviewer']['state'],'running')
        with self.assertRaisesRegex(PairError,'lease held'): self.store.acquire_reviewer(t['epoch'],p['id'],p)
        self.store.reviewer_progress(t['epoch'],p['id'],lease['owner'],'已核对输入版本与复现日志')
        self.assertFalse(self.store.reviewer_reminder()['notify'])

    def test_schema_ten_execution_migration_preserves_lost_running_lease(self):
        t,p=self.ready();lease=self.store.acquire_reviewer(t['epoch'],p['id'],p)
        message=self.store.send('A','B','迁移保留原令牌')
        claim=self.store.claim('B',self.sessions['B'],self.directories['B'],message_id=message['id'])
        with self.store.transaction() as db:
            remove_team_extensions(db)
            db.execute('ALTER TABLE reviewer_runtime DROP COLUMN execution_id')
            db.execute('PRAGMA user_version=10')
            original=dict(db.execute('SELECT * FROM reviewer_runtime').fetchone())
        class Interrupted(Store):
            def _upgrade_reviewer_execution(self,db):
                def deny_version(action,first,second,*rest):
                    return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_PRAGMA and first=='user_version' and second=='11' else sqlite3.SQLITE_OK
                db.set_authorizer(deny_version)
                try: super()._upgrade_reviewer_execution(db)
                finally: db.set_authorizer(None)
        with self.assertRaises(sqlite3.DatabaseError): Interrupted('test',home=self.home)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],10)
            self.assertNotIn('execution_id',{r[1] for r in db.execute('PRAGMA table_info(reviewer_runtime)')})
        upgraded=Store('test',home=self.home)
        current=upgraded.reviewer_status()['reviewer']
        self.assertEqual(current['state'],'running')
        self.assertTrue(current['execution_id'])
        with upgraded.transaction() as db:
            row=dict(db.execute('SELECT * FROM reviewer_runtime').fetchone())
            for key,value in original.items(): self.assertEqual(row[key],value)
        self.assertEqual(upgraded.message(message['id'])['claim_token'],claim['claim_token'])
        with self.assertRaises(PairError): upgraded.acquire_reviewer(t['epoch'],p['id'],p)
        result=upgraded.reconcile_reviewer_execution('A',self.sessions['A'],self.directories['A'],epoch=t['epoch'],reviewer_session=p['id'],execution_id=current['execution_id'],outcome='completed',evidence='迁移后显式核对原会话持久回合和工具结果均已终止')
        self.assertEqual(result['state'],'idle')
        with self.assertRaises(PairError): upgraded.release_reviewer(t['epoch'],p['id'],lease['owner'],outcome='completed',evidence='旧回执')
        backups=list(upgraded.directory.glob('schema-v10-*.backup.sqlite3'))
        self.assertTrue(backups)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT owner FROM reviewer_runtime').fetchone()[0],lease['owner'])

    def test_schema_eight_migration_preserves_claims_and_rolls_back_on_failure(self):
        message=self.store.send('A','B','保留原令牌')
        owned=self.store.claim('B',self.sessions['B'],self.directories['B'],message_id=message['id'])
        with self.store.transaction() as db:
            remove_team_extensions(db)
            db.execute('DROP TABLE reviewer_runtime');db.execute('DROP TABLE reviewer_history');db.execute('PRAGMA user_version=8')
        class Broken(Store):
            def _upgrade_reviewer(self,db):
                with db:
                    db.execute('BEGIN IMMEDIATE');db.execute('CREATE TABLE partial_probe(id)')
                    raise sqlite3.OperationalError('disk full')
        with self.assertRaisesRegex(sqlite3.OperationalError,'disk full'): Broken('test',home=self.home)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial_probe'").fetchone())
        upgraded=Store('test',home=self.home)
        self.assertEqual(upgraded.message(message['id'])['claim_token'],owned['claim_token'])
        self.assertEqual(upgraded.message(message['id'])['process_status'],'claimed')
        self.assertTrue(list(upgraded.directory.glob('schema-v8-*.backup.sqlite3')))
        with upgraded.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],13)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])


if __name__=='__main__': unittest.main()
