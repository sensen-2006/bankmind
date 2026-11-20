"""Explicit pilot effort evidence. Timed presence is not verified productive work."""
import math
from datetime import datetime,timezone
from typing import Literal
from fastapi import Depends,HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import EffortSession,Task,User,get_db,now
from .schemas import Input
from .security import current_user,scoped,role,audit

Phase=Literal['DOWNLOAD','PREPARE','IMPORT','INVESTIGATE','REVIEW','REPORT','ARCHIVE']
Method=Literal['BANKMIND','EXCEL_BASELINE','ERP_BASELINE']
PHASES=['DOWNLOAD','PREPARE','IMPORT','INVESTIGATE','REVIEW','REPORT','ARCHIVE']

class StartEffort(Input):
    method: Method
    phase: Phase
    cycle: str=Field(min_length=1,max_length=40,pattern=r'^[A-Za-z0-9_-]+$')

class StopEffort(Input):
    action: Literal['STOP','ABANDON']
    note: str=Field(min_length=8,max_length=2000)

def effort_json(e):
    return {k:getattr(e,k) for k in ['id','task_id','actor_id','method','phase','cycle','created_at','ended_at','seconds','status','note']}

def install_effort(app):
    from .main import lock,related_task
    DB=Depends(get_db);USER=Depends(current_user)

    @app.get('/api/tasks/{task_id}/effort')
    def effort(task_id:str,user:User=USER,db:Session=DB):
        t=scoped(db,Task,task_id,user)
        items=list(db.scalars(select(EffortSession).where(EffortSession.task_id==t.id).order_by(EffortSession.created_at.desc(),EffortSession.id)))
        # Keep cycles separate. Summing repeated runs as one comparable workflow
        # would create an invalid efficiency claim.
        groups={}
        for e in items:
            key=e.cycle+':'+e.method
            g=groups.setdefault(key,{'cycle':e.cycle,'method':e.method,'seconds':0,'phases':{},'records':0})
            if e.status=='STOPPED':g['seconds']+=e.seconds;g['phases'][e.phase]=g['phases'].get(e.phase,0)+e.seconds;g['records']+=1
        for g in groups.values():g['missing_phases']=[p for p in PHASES if p not in g['phases']]
        return {'items':[effort_json(e) for e in items],'cycles':list(groups.values()),'disclaimer':'服务器记录开始与结束的时长，不证明期间均为有效劳动；操作人说明用于补充证据。对比必须同任务、同轮次、同范围且覆盖全部阶段。释放工时不等于工资支出减少，不自动推导ROI。'}

    @app.post('/api/tasks/{task_id}/effort/start')
    def start(task_id:str,body:StartEffort,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,task_id,user)
        running=db.scalar(select(EffortSession).where(EffortSession.actor_id==user.id,EffortSession.status=='RUNNING'))
        if running:raise HTTPException(409,'已有正在计时的工作阶段，请回到对应任务结束或作废计时')
        e=EffortSession(tenant_id=user.tenant_id,task_id=t.id,actor_id=user.id,method=body.method,phase=body.phase,cycle=body.cycle)
        db.add(e);db.flush();audit(db,user,'EFFORT_START',t.id,{}, {'id':e.id,'method':e.method,'phase':e.phase,'cycle':e.cycle},'自主开始试点工时计量；不改变财务结果')
        db.commit();return effort_json(e)

    @app.get('/api/tasks/{task_id}/effort/export')
    def export_effort(task_id:str,user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer','Auditor');lock(db);scoped(db,Task,task_id,user)
        result={'task_id':task_id,'captured_at':now(),**effort(task_id,user,db)}
        audit(db,user,'EFFORT_EXPORT',task_id,{}, {'records':len(result['items'])},'导出独立工时台账；不覆盖已签署财务快照');db.commit()
        return JSONResponse(result,headers={'Content-Disposition':f'attachment; filename="BankMind-effort-{task_id}.json"'})

    @app.post('/api/effort/{oid}/stop')
    def stop(oid:str,body:StopEffort,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);e,t=related_task(db,EffortSession,oid,user)
        if e.actor_id!=user.id:raise HTTPException(403,'仅操作人能结束自己的计时；管理员不能代改工时')
        if e.status!='RUNNING':raise HTTPException(409,'计时已结束，原记录不允许覆盖')
        ended=now();elapsed=(datetime.fromisoformat(ended)-datetime.fromisoformat(e.created_at)).total_seconds()
        if elapsed>86400 and body.action!='ABANDON':raise HTTPException(422,'计时超过一天，不计入有效工时；请说明原因并作废留档')
        e.ended_at=ended;e.seconds=max(1,math.ceil(elapsed)) if body.action=='STOP' else 0;e.status='STOPPED' if body.action=='STOP' else 'ABANDONED';e.note=body.note
        audit(db,user,'EFFORT_'+e.status,t.id,{'status':'RUNNING'},{'id':e.id,'seconds':e.seconds,'method':e.method,'phase':e.phase,'cycle':e.cycle},body.note)
        db.commit();return effort_json(e)
