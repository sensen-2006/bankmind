"""Signed periods are immutable until a separate authorized person approves reopening."""
from sqlalchemy import select
from fastapi import HTTPException
from .db import PeriodControl

def period_record(db,task):
    return db.scalar(select(PeriodControl).where(PeriodControl.task_id==task.id,PeriodControl.tenant_id==task.tenant_id))

def period_json(db,task):
    p=period_record(db,task)
    if p:return {k:getattr(p,k) for k in ['state','version','requester_id','reason','report_id']}
    return {'state':'LOCKED' if task.status=='COMPLETED' else 'OPEN','version':1,'requester_id':'','reason':'','report_id':''}

def require_open_period(db,task):
    if period_json(db,task)['state']!='OPEN':
        raise HTTPException(409,'本期已锁定；请先提交期间重开申请，由另一位授权审核者批准后修改')

def seal_period(db,task,report_id):
    p=period_record(db,task)
    if p is None:
        p=PeriodControl(tenant_id=task.tenant_id,task_id=task.id,state='LOCKED',version=1,report_id=report_id);db.add(p)
    else:
        p.state='LOCKED';p.version+=1;p.report_id=report_id;p.requester_id='';p.reason=''
    return p
