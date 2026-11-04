"""辅助池隔离回归规格；本轮只编写，未执行，不代表真实CLI或Hyper-V验收。"""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import PairError
from scripts.pair_auxiliary import upgrade_auxiliary_v14
from scripts.pair_auxiliary_runtime import STATIC_OVERRIDES, verify_static_controls, read_source_page
from tests import test_core


class AuxiliaryTests(unittest.TestCase):
    tearDown = test_core.CoreTests.tearDown

    def setUp(self):
        test_core.CoreTests.setUp(self)
        with self.store.transaction() as db:
            upgrade_auxiliary_v14(db)
            for name in ('A', 'B'):
                proof = {'id': self.sessions[name], 'cwd': self.directories[name],
                         'modelProvider': 'openai', 'model': 'gpt-6.1-sol'}
                db.execute('UPDATE members SET proof=?,verified=1,online=1 WHERE id=?', (json.dumps(proof), name))
        self.store.auxiliary_policy(*self.actor(), enabled=True, reason='隔离测试辅助队列')
        self.store.allow_auxiliary_config(*self.actor(), 'allowed-a', 'A', '仅本测试已核验配置')
        self._index = 10

    def actor(self, member='A'):
        return member, self.sessions[member], self.directories[member]

    def candidate(self, name='product', head='b' * 40):
        self.store.assign(name, '固定候选', 'B', ['app.py'], acceptance=['准确结果'],
                          required_checks=[{'name': 'unit', 'command': 'python -B check.py'}])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='delivered',commits_json=? WHERE id=?", (json.dumps([head]), name))
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed',process_status='completed' WHERE task_id=?", (name,))
        stage = self.store.submit_stage(*self.actor('B'), name, {'kind': 'commit', 'head': head},
            [{'member': 'B', 'provider': 'openai', 'model': 'gpt-6.1-sol', 'session_uuid': self.sessions['B']}],
            [], {'objective': '准确结果', 'acceptance': ['准确结果'], 'environment': {}, 'lockfiles': [], 'baseline': 'a' * 40})
        return self.store.register_audit(*self.actor(), 'audit-' + name, stage['id'],
            tracks=['ocr'], coverage_units=[{'id': 'source', 'track': 'ocr', 'required': True}])

    def queue(self, key='aux', *, candidate=None, role='analysis', **kwargs):
        candidate = candidate or self.candidate()
        self.store.queue_auxiliary(*self.actor(), key, candidate['id'], 'ocr', role, ['source'], **kwargs)
        return candidate

    def reserve(self, key='aux', **kwargs):
        return self.store.reserve_auxiliary(*self.actor(), key, 'allowed-a', **kwargs)

    def start_fixture(self, ticket, *, context=None):
        """只模拟官方适配器交回的边界；不是官方身份或真实执行证明。"""
        self._index += 1
        context = context or '00000000-0000-0000-0000-' + str(self._index).zfill(12)
        with self.store.transaction() as db:
            reviewer = dict(db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (ticket['reviewer_id'],)).fetchone())
            Path(reviewer['cwd']).mkdir(parents=True, exist_ok=True)
            proof = {'id': context, 'cwd': reviewer['cwd'], 'model': reviewer['model'], 'modelProvider': 'openai'}
            self.store._begin_auxiliary_verified(db, ticket['execution_id'], ticket['lease'], proof, 'unit-pool', {'unit_fixture': True})
            self.store._record_auxiliary_turn(db, ticket['execution_id'], ticket['lease'], 'unit-turn-' + str(self._index))
        return context

    def end_fixture(self, ticket):
        with self.store.transaction() as db:
            row = self.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease'])
            self.store._end_auxiliary(db, row, 'completed', {'unit_fixture_terminal': True})
            db.execute("UPDATE auxiliary_tasks SET status='completed' WHERE id=?", (row['task_id'],))

    def formal_parent_fixture(self, candidate):
        """固定已有正式审核的数据库夹具，不代表本测试取得真实官方回执。"""
        with self.store.transaction() as db:
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()
            report = {'candidate_digest': stage['digest'], 'verdict': 'passed', 'checks': [], 'findings': []}
            db.execute("UPDATE review_stages SET status='passed',verdict='passed',evidence=? WHERE id=?", (json.dumps(report), stage['id']))
            db.execute("UPDATE tasks SET status='completed' WHERE id=?", (stage['task_id'],))

    def settled_auxiliary_fixture(self, candidate):
        """以受信适配器边界的隔离证据调用真实保存和结算逻辑。"""
        self.formal_parent_fixture(candidate)
        self.queue('final-fixture', candidate=candidate, role='final_verification')
        ticket = self.reserve('final-fixture')
        self.start_fixture(ticket)
        with self.store.transaction() as db:
            execution = self.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease'])
            report = {'verdict': 'passed', 'findings': [], 'checks': []}
            self.store._save_auxiliary_report(db, execution, report, report,
                {'errors': [], 'coverage_complete': True}, {'formal_checks_verified': True, 'fixture_only': True})
        return self.store.settle_audit(*self.actor(), candidate['id'])

    def test_capacity_ranges_and_downshift_preserve_inflight(self):
        for value in (0, 5, True):
            with self.assertRaises(PairError):
                self.store.auxiliary_policy(*self.actor(), concurrency=value, reason='invalid')
        for value in (1, 5, True):
            with self.assertRaises(PairError):
                self.store.auxiliary_policy(*self.actor(), task_capacity=value, reason='invalid')
        policy = self.store.auxiliary_policy(*self.actor(), concurrency=4, task_capacity=3, stage_capacity=4, reason='实际处理能力的隔离夹具')
        self.assertEqual((policy['concurrency'], policy['task_capacity'], policy['stage_capacity']), (4, 3, 4))
        candidate = self.candidate()
        for index in range(4):
            self.queue('aux-' + str(index), candidate=candidate)
            self.reserve('aux-' + str(index))
        self.store.auxiliary_policy(*self.actor(), concurrency=1, reason='收敛新增，不抢占已有预留')
        state = self.store.auxiliary_status(*self.actor())
        self.assertEqual(state['active_slots'], 4)
        self.queue('fifth', candidate=candidate)
        with self.assertRaisesRegex(PairError, 'pool full'):
            self.reserve('fifth')

    def test_unknown_retains_slot_and_budget_reservation(self):
        self.queue()
        ticket = self.reserve()
        self.store.auxiliary_unknown(*self.actor(), ticket['execution_id'], ticket['lease'], '创建回包丢失')
        state = self.store.auxiliary_status(*self.actor())
        self.assertEqual(state['active_slots'], 1)
        self.assertEqual((state['audits'][0]['charged'], state['audits'][0]['reserved']), (0, 1))
        with self.assertRaisesRegex(PairError, 'may have occurred'):
            self.store.cancel_auxiliary_reservation(*self.actor(), ticket['execution_id'], ticket['lease'], '不能猜测退款')

    def test_actual_start_idempotent_and_not_refunded(self):
        self.queue()
        ticket = self.reserve()
        self.start_fixture(ticket)
        with self.store.transaction() as db:
            row = self.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease'])
            self.store._record_auxiliary_turn(db, row['id'], ticket['lease'], row['turn_id'])
        self.end_fixture(ticket)
        state = self.store.auxiliary_status(*self.actor())
        self.assertEqual(state['active_slots'], 0)
        self.assertEqual((state['audits'][0]['charged'], state['audits'][0]['reserved']), (1, 0))

    def test_cancel_definite_unstarted_reservation_is_idempotent(self):
        self.queue()
        ticket = self.reserve()
        first = self.store.cancel_auxiliary_reservation(*self.actor(), ticket['execution_id'], ticket['lease'], '明确未发RPC')
        second = self.store.cancel_auxiliary_reservation(*self.actor(), ticket['execution_id'], ticket['lease'], '重复结算')
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.store.auxiliary_status(*self.actor())['audits'][0]['reserved'], 0)

    def test_started_task_cannot_be_cancelled_as_unstarted(self):
        self.queue()
        ticket = self.reserve()
        self.start_fixture(ticket)
        with self.assertRaises(PairError):
            self.store.cancel_auxiliary_reservation(*self.actor(), ticket['execution_id'], ticket['lease'], '不得退款')

    def test_author_cannot_use_new_role_name_to_self_review(self):
        self.queue()
        ticket = self.reserve()
        with self.assertRaisesRegex(PairError, 'identity mismatch|Independent reviewer'):
            self.start_fixture(ticket, context=self.sessions['B'])

    def test_new_context_required_for_independent_verification(self):
        candidate = self.queue('discovery', role='discovery')
        first = self.reserve('discovery')
        self.start_fixture(first)
        self.end_fixture(first)
        self.queue('validation', candidate=candidate, role='validation', independent_of=['discovery'])
        with self.assertRaisesRegex(PairError, 'independence|Fresh context'):
            self.reserve('validation', reviewer_id=first['reviewer_id'])

    def test_unrelated_session_cannot_release_lease(self):
        self.queue()
        ticket = self.reserve()
        with self.assertRaisesRegex(PairError, 'lease mismatch'):
            self.store.cancel_auxiliary_reservation(*self.actor(), ticket['execution_id'], 'wrong', '原执行保留')
        self.assertEqual(self.store.auxiliary_status(*self.actor())['active_slots'], 1)

    def test_config_change_revokes_new_admission(self):
        self.queue()
        with self.store.transaction() as db:
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
        with self.assertRaisesRegex(PairError, 'config changed'):
            self.reserve()

    def test_paused_collaboration_cannot_reserve(self):
        self.queue()
        with self.store.transaction() as db:
            db.execute("UPDATE collaborations SET status='paused'")
        with self.assertRaisesRegex(PairError, 'not running'):
            self.reserve()

    def test_shared_resources_not_released_by_other_reservation(self):
        candidate = self.queue('first', resources=['port:8080'])
        self.store.auxiliary_policy(*self.actor(), concurrency=2, reason='隔离两个不同任务')
        self.reserve('first')
        self.queue('second', candidate=candidate, resources=['port:8080'])
        with self.assertRaisesRegex(PairError, 'resource held'):
            self.reserve('second')

    def test_parent_wait_retains_budget_and_resumes_via_new_admission(self):
        candidate = self.queue('parent')
        parent = self.reserve('parent')
        self.start_fixture(parent)
        self.queue('child', candidate=candidate, parent_task='parent', role='validation')
        with self.assertRaisesRegex(PairError, 'Parent execution unresolved'):
            self.store.wait_auxiliary(*self.actor(), 'parent', ['child'], '尚不能释放')
        self.end_fixture(parent)
        self.store.wait_auxiliary(*self.actor(), 'parent', ['child'], '读子结果后仅完成剩余核验')
        child = self.reserve('child')
        self.start_fixture(child)
        self.end_fixture(child)
        again = self.reserve('parent')
        self.assertNotEqual(parent['execution_id'], again['execution_id'])
        self.assertEqual(self.store.auxiliary_status(*self.actor())['audits'][0]['charged'], 2)

    def test_parent_child_wait_cycle_rejected(self):
        candidate = self.queue('parent')
        parent = self.reserve('parent')
        self.start_fixture(parent)
        self.end_fixture(parent)
        self.queue('child', candidate=candidate, parent_task='parent', dependencies=['parent'])
        with self.assertRaisesRegex(PairError, 'dependency cycle'):
            self.store.wait_auxiliary(*self.actor(), 'parent', ['child'], '不能形成父子循环')

    def test_audit_cannot_drop_required_track_or_units(self):
        candidate = self.candidate()
        with self.assertRaisesRegex(PairError, 'scope immutable'):
            self.store.register_audit(*self.actor(), 'renamed-audit', candidate['stage_id'], tracks=['ocr'],
                coverage_units=[{'id': 'source', 'track': 'ocr', 'required': False}])

    def test_budget_reserves_completion_and_caps_actual_starts(self):
        candidate = self.candidate()
        self.store.queue_auxiliary(*self.actor(), 'hunter', candidate['id'], 'ocr', 'discovery', ['source'])
        with self.store.transaction() as db:
            # 缩小隔离夹具的总预算，验证保留逻辑；生产默认仍是64且不提供此写入口。
            db.execute('UPDATE auxiliary_audits SET budget=2,completion_reserve=1 WHERE id=?', (candidate['audit_id'],))
        ticket = self.reserve('hunter')
        self.start_fixture(ticket)
        self.end_fixture(ticket)
        self.queue('next-hunter', candidate=candidate, role='discovery')
        with self.assertRaisesRegex(PairError, 'budget reserved'):
            self.reserve('next-hunter')
        self.queue('final', candidate=candidate, role='final_verification')
        final = self.reserve('final')
        self.assertIsNotNone(final['execution_id'])

    def test_subreports_do_not_promote_parent_stage(self):
        candidate = self.queue()
        result = self.store.settle_audit(*self.actor(), candidate['id'])
        self.assertEqual(result['verdict'], 'needs_validation')
        self.assertIn('parent_fixed_stage_official_review_required', result['reasons'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()[0], 'pending')

    def test_ordinary_pass_does_not_bypass_required_pending_auxiliary_audit(self):
        candidate = self.candidate()
        self.formal_parent_fixture(candidate)
        with self.assertRaisesRegex(PairError, 'Independent review required'):
            self.store.require_reviewed('product')
        self.store.assign('dependent', '后续依赖不得使用未完成辅助审核成果', 'A', ['consumer.py'], dependencies=['product'])
        self.store.schedule()
        self.assertEqual(self.store.task('dependent')['status'], 'pending')
        with self.assertRaisesRegex(PairError, 'Source not approved'):
            self.store.integration_candidate(*self.actor(), [candidate['stage_id']],
                {'kind': 'commit', 'head': 'c' * 40}, [],
                {'required_checks': [{'name': 'unit', 'command': 'python -B check.py'}]})

    def test_disabling_auxiliary_pool_does_not_remove_registered_gate(self):
        candidate = self.candidate()
        self.formal_parent_fixture(candidate)
        self.store.auxiliary_policy(*self.actor(), enabled=False, reason='暂停新增辅助，不撤销原审核义务')
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')

    def test_legacy_stage_without_registered_auxiliary_obligation_keeps_original_gate(self):
        candidate = self.candidate()
        self.formal_parent_fixture(candidate)
        with self.store.transaction() as db:
            db.execute('DELETE FROM auxiliary_candidates WHERE id=?', (candidate['id'],))
            db.execute('DELETE FROM auxiliary_contributions WHERE audit_id=?', (candidate['audit_id'],))
            db.execute('DELETE FROM auxiliary_audits WHERE id=?', (candidate['audit_id'],))
        self.assertEqual(self.store.require_reviewed('product')['id'], candidate['stage_id'])

    def test_settlement_reads_parent_evidence_without_recursive_stage_gate(self):
        candidate = self.candidate()
        self.formal_parent_fixture(candidate)
        with patch.object(type(self.store), '_stage_passed', side_effect=AssertionError('不得递归调用业务门禁')):
            result = self.store.settle_audit(*self.actor(), candidate['id'])
        self.assertEqual(result['verdict'], 'needs_validation')
        self.assertNotIn('parent_fixed_stage_official_review_required', result['reasons'])

    def test_exact_settled_auxiliary_candidate_unlocks_shared_review_gate(self):
        candidate = self.candidate()
        result = self.settled_auxiliary_fixture(candidate)
        self.assertEqual(result['verdict'], 'passed')
        self.assertEqual(self.store.require_reviewed('product')['id'], candidate['stage_id'])
        self.queue('new-required-review', candidate=candidate, role='validation')
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')
        ticket = self.reserve('new-required-review')
        self.start_fixture(ticket)
        with self.store.transaction() as db:
            execution = self.store._auxiliary_execution(db, ticket['execution_id'], ticket['lease'])
            report = {'verdict': 'needs_fix', 'findings': [], 'checks': []}
            self.store._save_auxiliary_report(db, execution, report, report,
                {'errors': [], 'coverage_complete': True}, {'formal_checks_verified': True, 'fixture_only': True})
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')
        result = self.store.settle_audit(*self.actor(), candidate['id'])
        self.assertNotEqual(result['verdict'], 'passed')

    def test_auxiliary_pass_cannot_be_reused_by_changed_contract_or_digest(self):
        candidate = self.candidate()
        self.settled_auxiliary_fixture(candidate)
        with self.store.transaction() as db:
            db.execute('UPDATE auxiliary_candidates SET contract_digest=? WHERE id=?', ('0' * 64, candidate['id']))
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')
        with self.store.transaction() as db:
            db.execute('UPDATE auxiliary_candidates SET contract_digest=?,candidate_digest=? WHERE id=?',
                       (candidate['contract_digest'], '1' * 64, candidate['id']))
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')

    def test_latest_stage_requires_its_own_auxiliary_settlement(self):
        candidate = self.candidate()
        self.settled_auxiliary_fixture(candidate)
        stage = self.store.submit_stage(*self.actor('B'), 'product', {'kind': 'commit', 'head': 'c' * 40},
            [{'member': 'B', 'provider': 'openai', 'model': 'gpt-6.1-sol', 'session_uuid': self.sessions['B']}],
            [], {'objective': '准确结果', 'acceptance': ['准确结果'], 'environment': {}, 'lockfiles': [], 'baseline': 'a' * 40})
        self.formal_parent_fixture({'stage_id': stage['id']})
        with self.assertRaises(PairError):
            self.store.require_reviewed('product')

    def test_json_report_is_not_execution_evidence(self):
        self.queue()
        ticket = self.reserve()
        with self.assertRaisesRegex(PairError, 'receipt unavailable'):
            self.store.finish_auxiliary(*self.actor(), ticket['execution_id'], ticket['lease'], {'verdict': 'passed', 'checks': [], 'findings': []})

    def test_no_live_hyperv_claim_from_task_flag(self):
        self.queue(requires_dynamic=True)
        ticket = self.reserve()
        with patch('scripts.pair_auxiliary_runtime.AppServer', side_effect=AssertionError('不得启动客户端')):
            result = self.store.begin_auxiliary(*self.actor(), ticket['execution_id'], ticket['lease'])
        self.assertEqual(result['reason'], 'hyperv_environment_unverified')
        self.assertFalse(result['started'])

    def test_status_never_exposes_run_tokens_or_producer(self):
        self.queue()
        ticket = self.reserve()
        value = json.dumps(self.store.auxiliary_status(*self.actor()))
        self.assertNotIn(ticket['lease'], value)
        self.assertNotIn('producer', value)

    def quota_fixture(self, *, reset=None):
        value = {'observed_at': time.time(), 'reliability': 'fresh', 'pool': 'unit-pool',
                 'limit_id': 'unit-limit', 'remaining': 5,
                 'windows': [{'duration_mins': 300, 'resets_at': reset or time.time() + 3600, 'remaining': 5}]}
        with self.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO quota_snapshots VALUES(?,?)', ('A', json.dumps(value)))
        return value

    def test_closed_scope_invalidates_old_quota_proposal_and_grant(self):
        from tests.test_scoped_closure import ScopedClosureTests
        closure=ScopedClosureTests();closure.__dict__.update(self.__dict__)
        closure.prepare()
        self.quota_fixture()
        waiting=self.store.prepare_auxiliary_quota_consent(*self.actor(),'allowed-a')
        approved=self.store.prepare_auxiliary_quota_consent(*self.actor(),'allowed-a')
        def authorize(proposal,event):
            with self.store.transaction() as db:
                return self.store._record_auxiliary_quota_user_event(db,'A',proposal['confirmation_text'],
                    event_id=event,source_identity='fixture:direct-user',session=self.sessions['A'],
                    binding_version=self.store._member(db,'A')['binding_version'])
        self.assertTrue(authorize(approved,'before-close')['enabled'])
        with self.store.transaction() as db:
            config,source=self.store._auxiliary_config(db,'allowed-a')
            self.store._auxiliary_quota(db,config,source)
        closure.close()
        self.store.auxiliary_policy(*self.actor(),enabled=True,reason='用户授权新范围辅助工作，不代表忽略额度')
        self.assertEqual(authorize(waiting,'old-proposal-after-close')['reason'],'displayed_scope_changed')
        with self.assertRaisesRegex(PairError,'Direct auxiliary consent required'):
            self.store.auxiliary_quota_policy(*self.actor(),'allowed-a',True,'不能沿用旧范围授权',source_event='before-close')
        with self.store.transaction() as db:
            config,source=self.store._auxiliary_config(db,'allowed-a')
            with self.assertRaisesRegex(PairError,'Auxiliary quota protected'):
                self.store._auxiliary_quota(db,config,source)
        current=self.store.prepare_auxiliary_quota_consent(*self.actor(),'allowed-a')
        self.assertIn('closed_scope_digest',current['scope'])
        self.assertTrue(authorize(current,'new-scope-consent')['enabled'])
        self.assertTrue(self.store.auxiliary_quota_policy(*self.actor(),'allowed-a',True,'确认新范围明确用户消息',
                                                        source_event='new-scope-consent')['enabled'])
        with self.store.transaction() as db:
            config,source=self.store._auxiliary_config(db,'allowed-a')
            self.store._auxiliary_quota(db,config,source)

    def test_plain_json_does_not_authorize_auxiliary_soft_limit(self):
        self.quota_fixture()
        with self.assertRaisesRegex(PairError, 'Direct auxiliary consent required'):
            self.store.auxiliary_quota_policy(*self.actor(), 'allowed-a', True, '用户已经同意')

    def test_auxiliary_soft_limit_requires_displayed_scope_and_direct_user_event(self):
        self.quota_fixture()
        proposal = self.store.prepare_auxiliary_quota_consent(*self.actor(), 'allowed-a')
        with self.store.transaction() as db:
            actor = self.store._member(db, 'A')
            result = self.store._record_auxiliary_quota_user_event(db, 'A', proposal['confirmation_text'],
                event_id='unit-official-user-boundary', source_identity='unit-source',
                session=self.sessions['A'], binding_version=actor['binding_version'])
        self.assertTrue(result['enabled'])
        result = self.store.auxiliary_quota_policy(*self.actor(), 'allowed-a', True, '读取已经记录的授权',
                                                 source_event='unit-official-user-boundary')
        self.assertTrue(result['enabled'])
        self.store.auxiliary_quota_policy(*self.actor(), 'allowed-a', False, '恢复软保护')
        with self.assertRaises(PairError):
            self.store.auxiliary_quota_policy(*self.actor(), 'allowed-a', True, '不能复活已撤销授权',
                                             source_event='unit-official-user-boundary')

    def test_cycle_change_does_not_accept_old_auxiliary_proposal(self):
        first = self.quota_fixture()
        proposal = self.store.prepare_auxiliary_quota_consent(*self.actor(), 'allowed-a')
        self.quota_fixture(reset=first['windows'][0]['resets_at'] + 3600)
        with self.store.transaction() as db:
            actor = self.store._member(db, 'A')
            result = self.store._record_auxiliary_quota_user_event(db, 'A', proposal['confirmation_text'],
                event_id='unit-stale-cycle', source_identity='unit-source',
                session=self.sessions['A'], binding_version=actor['binding_version'])
        self.assertFalse(result['enabled'])

    def test_migration_preserves_legacy_and_is_idempotent(self):
        with self.store.transaction() as db:
            before = list(db.execute('SELECT * FROM reviewer_runtime'))
            version = db.execute('PRAGMA user_version').fetchone()[0]
            upgrade_auxiliary_v14(db)
            upgrade_auxiliary_v14(db)
            self.assertEqual([tuple(r) for r in before], [tuple(r) for r in db.execute('SELECT * FROM reviewer_runtime')])
            self.assertEqual(version, db.execute('PRAGMA user_version').fetchone()[0])


class StaticAuxiliaryBoundaryTests(unittest.TestCase):
    def test_read_only_label_does_not_prove_shell_disabled(self):
        with self.assertRaisesRegex(PairError, 'Static tools not verified'):
            verify_static_controls({'sandbox_mode': 'read-only'}, STATIC_OVERRIDES)

    def test_extra_mcp_entry_is_not_silently_allowed(self):
        config = {'mcp_servers': {'external': {'enabled': True}}}
        for dotted, value in STATIC_OVERRIDES.items():
            target = config
            parts = dotted.split('.')
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        with self.assertRaisesRegex(PairError, 'mcp_servers.external.enabled'):
            verify_static_controls(config, STATIC_OVERRIDES)

    def test_source_reader_rejects_undeclared_or_escape_paths(self):
        with self.assertRaises(ValueError):
            read_source_page('.', {'app.py': '0' * 64}, {'path': '../auth.json', 'offset': 0, 'limit': 100})
        with self.assertRaises(ValueError):
            read_source_page('.', {}, {'path': 'app.py', 'offset': True, 'limit': 100})


if __name__ == '__main__':
    unittest.main()
