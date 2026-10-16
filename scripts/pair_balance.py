"""额度只表示周期内比例；未知数据退回本协作工作量，绝不推算套餐容量。"""
from __future__ import annotations

import hashlib
import json
import math
import time


def snapshot(payload, *, model=None, limit_id=None, account=None, now=None):
    now=time.time() if now is None else now
    result={'observed_at':now,'model':model,'pool':account,'reliability':'missing',
            'remaining':None,'windows':[],'limit_id':None}
    buckets=payload.get('rateLimitsByLimitId')
    if isinstance(buckets,dict) and buckets:
        key=limit_id or (model if model in buckets else None)
        if key is None and len(buckets)==1:key=next(iter(buckets))
        if key not in buckets:
            result['reliability']='bucket_unknown';return result
        bucket=buckets[key]
    else:bucket=payload.get('rateLimits')
    if not isinstance(bucket,dict):return result
    result['limit_id']=bucket.get('limitId') or (key if buckets else 'legacy')
    for name in ('primary','secondary'):
        window=bucket.get(name)
        if window is None:continue
        if not isinstance(window,dict):return result
        used,duration,reset=(window.get(k) for k in ('usedPercent','windowDurationMins','resetsAt'))
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (used,duration,reset)):
            return result
        if not 0<=used<=100 or duration<=0:return result
        result['windows'].append({'remaining':100-used,'duration_mins':duration,'resets_at':reset})
    if not result['windows']:return result
    result['remaining']=min(w['remaining'] for w in result['windows'])
    result['reliability']='reset_pending' if any(w['resets_at']<=now for w in result['windows']) else 'fresh'
    return result


def current(data, now=None):
    value=dict(data)
    now=time.time() if now is None else now
    value['age_seconds']=max(0,now-value.get('observed_at',0))
    if value.get('reliability')=='fresh':
        if value['age_seconds']>120:value['reliability']='stale'
        elif any(w['resets_at']<=now for w in value.get('windows',[])):value['reliability']='reset_pending'
    return value


def pool_key(account, home):
    # 身份仅用于本地资源池去重，不保存邮箱或认证响应。
    identity=account.get('account') or {}
    key=identity.get('accountId') or identity.get('id') or identity.get('email')
    return hashlib.sha256(str(key or home).lower().encode('utf-8')).hexdigest()


def choose(candidates, effort, quotas, loads):
    if not candidates:return None,'no_safe_member'
    eligible=[m for m in candidates if not (quotas[m].get('reliability')=='fresh' and quotas[m].get('remaining')==0)]
    if not eligible:return None,'quota_exhausted'
    if len(eligible)==1:return eligible[0],'only_safe_member'
    a,b=eligible[:2];qa,qb=quotas[a],quotas[b]
    comparable=(qa.get('reliability')==qb.get('reliability')=='fresh'
        and qa.get('pool')!=qb.get('pool') and qa.get('limit_id')==qb.get('limit_id')
        and sorted(w['duration_mins'] for w in qa['windows'])==sorted(w['duration_mins'] for w in qb['windows']))
    if effort>=5 and comparable and abs(qa['remaining']-qb['remaining'])>=20:
        return (a if qa['remaining']>qb['remaining'] else b),'quota_gap_at_least_20pp'
    selected=min(eligible,key=lambda m:(loads.get(m,0),m))
    return selected,('workload_balance' if comparable else 'workload_fallback')


def loads(db):
    rows=list(db.execute('SELECT member,task_id,sum(tokens) FROM turn_usage GROUP BY member,task_id'))
    # 同一协作中有实测的任务才参与校准；未经观测的任务继续使用 effort。
    measured={task:tokens for _,task,tokens in rows}
    efforts={r['id']:r['effort'] for r in db.execute('SELECT id,effort FROM tasks')}
    units=sum(measured.values())/sum(efforts[t] for t in measured) if measured and sum(measured.values()) else None
    result={'A':0.0,'B':0.0}
    for task in db.execute("SELECT id,assignee,effort FROM tasks WHERE status<>'pending' OR assignment_mode='fixed'"):
        result[task['assignee']]+=measured[task['id']]/units if units and task['id'] in measured else task['effort']
    return result
