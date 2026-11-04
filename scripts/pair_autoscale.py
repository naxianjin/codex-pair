"""辅助池按实际就绪队列、完成记录和资源逐步调节；不取消在途执行。"""
from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes


def resource_sample(previous=None):
    result={'observed_at':time.time(),'available_bytes':None,'cpu_busy_percent':None,'cpu_ticks':None}
    if os.name!='nt':return result
    class Memory(ctypes.Structure):
        _fields_=[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(name,ctypes.c_ulonglong) for name in
            ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(Memory)]
    kernel.GlobalMemoryStatusEx.restype=wintypes.BOOL
    memory=Memory();memory.length=ctypes.sizeof(memory)
    if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):result['available_bytes']=memory.available
    kernel.GetSystemTimes.argtypes=[ctypes.POINTER(wintypes.FILETIME)]*3
    kernel.GetSystemTimes.restype=wintypes.BOOL
    idle,kernel_time,user=(wintypes.FILETIME() for _ in range(3))
    if kernel.GetSystemTimes(ctypes.byref(idle),ctypes.byref(kernel_time),ctypes.byref(user)):
        values=[(v.dwHighDateTime<<32)|v.dwLowDateTime for v in (idle,kernel_time,user)]
        result['cpu_ticks']=values
        old=(previous or {}).get('cpu_ticks')
        if old and all(a>=b for a,b in zip(values,old)):
            total=values[1]-old[1]+values[2]-old[2]
            if total>0:result['cpu_busy_percent']=max(0,min(100,100*(1-(values[0]-old[0])/total)))
    return result


def autoscale_tick(store, sample, *, now=None):
    """只改变新准入上限；所有候选仍须经原reserve_auxiliary事务复验。"""
    try:from .pair_core import PairError
    except ImportError:from pair_core import PairError
    now=time.time() if now is None else now
    with store.transaction() as db:
        policy=store._auxiliary_policy(db)
        if not policy['enabled'] or not policy.get('automatic',False):return {'changed':False,'reason':'manual_or_disabled'}
        previous=store._option(db,'auxiliary_scale',{})
        if now-previous.get('observed_at',0)<30:return {'changed':False,'reason':'sample_interval'}
        current=policy['concurrency'];ceiling=policy.get('max_concurrency',4)
        active=store._auxiliary_active(db)
        unresolved=db.execute("SELECT count(*) FROM auxiliary_executions WHERE state IN ('starting','unknown')").fetchone()[0]
        completed=db.execute("SELECT count(*) FROM auxiliary_executions WHERE state='completed' AND charged=1 AND updated>?",
                             (previous.get('changed_at',now-180),)).fetchone()[0]
        ready=[];reliable=True;pool_pressure=False
        configs=[]
        for row in db.execute('SELECT id FROM auxiliary_configs WHERE enabled=1 ORDER BY id').fetchall():
            try:
                config,source=store._auxiliary_config(db,row[0]);store._auxiliary_quota(db,config,source)
            except PairError:
                pool_pressure=True;continue
            quota=store._quotas(db).get(source['id'],{})
            reliable=reliable and quota.get('reliability')=='fresh' and quota.get('remaining',0)>store._reserve(db,source['id'])+5
            configs.append(row[0])
        for row in db.execute("SELECT * FROM auxiliary_tasks WHERE status IN ('pending','waiting') AND requires_dynamic=0 ORDER BY created,id"):
            try:
                candidate=store._auxiliary_candidate(db,row['candidate_id'])
                origin=db.execute('SELECT task_id FROM review_stages WHERE id=?',(candidate['stage_id'],)).fetchone()[0]
                store._auxiliary_dispatchable(db,task_id=origin)
            except PairError:continue
            if any(store._auxiliary_task(db,key)['status']!='completed' for key in json.loads(row['dependencies'])):continue
            if any(db.execute('SELECT 1 FROM auxiliary_resources WHERE resource=?',(key,)).fetchone() for key in json.loads(row['resources'])):continue
            counts=store._auxiliary_budget(db,candidate['audit_id'])
            audit=db.execute('SELECT * FROM auxiliary_audits WHERE id=?',(candidate['audit_id'],)).fetchone()
            reserve=audit['completion_reserve'] if row['role'] in ('analysis','discovery') else 0
            if audit['budget']-counts['charged']-counts['reserved']<=reserve:continue
            ready.append(row['id'])
        memory=sample.get('available_bytes');cpu=sample.get('cpu_busy_percent')
        fresh=0<=now-sample.get('observed_at',0)<=60
        pressured=fresh and (memory is not None and memory<2*1024**3 or cpu is not None and cpu>=85)
        target=current;reason='hold'
        if pressured or pool_pressure:
            target=max(1,current-1);reason='resource_or_pool_pressure'
        elif not ready and active==0:
            target=1;reason='idle'
        elif (fresh and memory is not None and memory>=2*1024**3*(current+1)
              and cpu is not None and cpu<70 and reliable and configs and not unresolved
              and ready and active>=current and completed>=2 and now-previous.get('changed_at',0)>=60):
            target=min(ceiling,current+1);reason='measured_queue_and_throughput'
        target=min(target,ceiling)
        record={'observed_at':now,'changed_at':now if target!=current else previous.get('changed_at',now),
                'from':current,'to':target,'reason':reason,'active':active,'ready':ready,
                'completed_since_change':completed,'unknown_or_starting':unresolved,'reliable_quota':reliable,
                'resources':sample,'existing_executions_preserved':True}
        store._put(db,'auxiliary_scale',record)
        if target!=current:
            store._put(db,'auxiliary_policy',dict(policy,concurrency=target))
            store.event(db,'auxiliary_concurrency_changed',**record)
        return dict(record,changed=target!=current)
