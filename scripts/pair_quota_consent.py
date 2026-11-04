"""仅由准确原会话的用户事件登记软额度授权；不改变服务限额。"""
from __future__ import annotations

import hashlib
import json
import time


class QuotaConsentMixin:
    def _quota_consent_identity(self, db, member, quota):
        actor = self._member(db, member)
        proof = json.loads(actor['proof'])
        windows = quota.get('windows') or []
        if (quota.get('reliability') != 'fresh' or not quota.get('pool') or not quota.get('limit_id')
                or not windows or any(not isinstance(w.get('resets_at'), (int, float)) or w['resets_at'] <= time.time() for w in windows)):
            return None
        facts = {'member': member, 'session': actor['session_uuid'], 'binding_version': actor['binding_version'],
                 'provider': proof.get('modelProvider'), 'model': proof.get('model'),
                 'account': proof.get('accountFingerprint'), 'pool': quota['pool'], 'limit_id': quota['limit_id'],
                 'windows': sorted((w['duration_mins'], w['resets_at']) for w in windows)}
        if self._option(db, 'closed_scope'):
            facts['closed_scope'] = self._option(db, 'closed_scope')
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    def _record_quota_user_event(self, db, member, text, *, event_id, source_identity, session, binding_version):
        if text.strip() not in ('忽略额度', '恢复额度保护'):
            return {'recognized': False}
        actor = self._member(db, member)
        if actor['session_uuid'] != session or actor['binding_version'] != binding_version:
            return {'recognized': True, 'enabled': False, 'reason': 'stale_binding'}
        event_key = 'quota_user_event:'+event_id
        previous = self._option(db, event_key)
        if previous is not None:
            return dict(previous, duplicate=True)
        quota = self._quotas(db).get(member, {})
        identity = self._quota_consent_identity(db, member, quota)
        enabled = text.strip() == '忽略额度' and identity is not None
        record = {'enabled': enabled, 'identity': identity, 'source_identity': source_identity,
                  'source_event': event_id, 'created_at': time.time(), 'scope': 'visible_member_current_cycle_only',
                  'reason': 'direct_user_event' if identity else 'quota_cycle_unverified'}
        self._put(db, 'quota_consent:'+member, record)
        self._put(db, event_key, record)
        self.event(db, 'quota_consent_recorded', member=member, enabled=enabled, source_event=event_id,
                   reason=record['reason'], auxiliary_authorized=False)
        return record

    def _soft_quota_ignored(self, db, member, quota=None):
        consent = self._option(db, 'quota_consent:'+member, {})
        if not consent.get('enabled'):
            return False
        actor = self._member(db, member)
        if (actor['lifecycle'] not in ('active', 'draining') or not actor['online']
                or db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running'
                or self._option(db, 'goal_completed', False)):
            return False
        quota = self._quotas(db).get(member, {}) if quota is None else quota
        return (isinstance(quota.get('remaining'), (int, float)) and quota['remaining'] > 0
                and consent['identity'] == self._quota_consent_identity(db, member, quota))

    def quota_consent_status(self, member, session, cwd):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            record = self._option(db, 'quota_consent:'+member, {})
            return {'member': member, 'active': self._soft_quota_ignored(db, member),
                    'source_event': record.get('source_event'), 'reason': record.get('reason'),
                    'auxiliary_authorized': False, 'hard_limits_preserved': True}
