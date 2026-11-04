"""仅替代隔离单测的外部恢复证据，仍调用真实恢复状态机。"""
import uuid

from scripts.pair_recovery import VerifiedRecoveryEvidence,digest


class ResumeEvidence:
    def verify(self,kind,context):
        if kind!='resume':raise AssertionError('Resume fixture cannot authorize other operations')
        return VerifiedRecoveryEvidence(kind,digest(context),('fixture:isolated-resume-observation',),
            {'source_is_current_user_instruction':True,'all_required_gates_verified':True,'full_scope_reconciled':True})


def resume_for_test(store,*,scope=None):
    actor=store.member('A')
    binding=('A',actor['session_uuid'],actor['cwd'])
    request=store.request_resume(*binding,instruction='隔离单测明确继续',idempotency_key='fixture:'+uuid.uuid4().hex,scope=scope)
    with store.transaction() as db:
        tasks=[row['id'] for row in db.execute("SELECT id,assignee FROM tasks WHERE status NOT IN ('cancelled','completed','integrated')")
               if scope is None or scope=={'kind':'pair'} or scope=={'kind':'member','id':row['assignee']} or scope=={'kind':'task','id':row['id']}]
    previous=getattr(store,'_recovery_evidence_adapter',None)
    store._recovery_evidence_adapter=ResumeEvidence()
    try:
        result=store.reconcile_resume(*binding,request['id'],tasks=tasks)
        store.schedule()
        return result
    finally:
        store._recovery_evidence_adapter=previous
