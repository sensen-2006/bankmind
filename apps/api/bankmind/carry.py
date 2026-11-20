"""BM-006: explicit authoritative periods, itemized opening bridge, independently reviewed clearing.
SQLite writers use the existing BEGIN IMMEDIATE transaction. Production concurrency remains an external gate.
"""
from datetime import date,timedelta
from collections import defaultdict
from fastapi import Depends,HTTPException
from pydantic import Field
from sqlalchemy import select
from .db import *
from .schemas import Input,Decision
from .security import current_user,role,scoped,digest,audit
from .parsing import money,amount_text
from .periods import require_open_period,period_json
TIMING=['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']
EFFECTIVE=['APPROVED','REVERSAL_PENDING']

def authorities(db,t):return list(db.scalars(select(AccountPeriod).where(AccountPeriod.tenant_id==t.tenant_id,AccountPeriod.account_id==t.account_id,AccountPeriod.currency==t.currency,AccountPeriod.status.in_(EFFECTIVE))))
def effective_clearings(db):return list(db.scalars(select(Clearing).where(Clearing.status.in_(EFFECTIVE))))
def used_transactions(db,task_id):
    sums=defaultdict(int)
    for c in db.scalars(select(Clearing).where(Clearing.task_id==task_id,Clearing.status.in_(EFFECTIVE))):
        for a in c.allocations:sums[a['transaction_id']]+=int(a['amount_minor'])
    return sums

def cleared_ids(db,t):
    used=used_transactions(db,t.id)
    return {i for i,n in used.items() if (x:=db.get(Transaction,i)) and n==abs(x.amount)}
def remaining(db,item,as_of='9999-12-31'):
    used=0
    for c in db.scalars(select(Clearing).where(Clearing.item_id==item.id,Clearing.status.in_(EFFECTIVE))):
        for a in c.allocations:
            tx=db.get(Transaction,a['transaction_id'])
            if tx.date<=as_of:used+=int(a['amount_minor'])
    return item.amount-used

def prior_authority(db,t):
    prior=[a for a in authorities(db,t) if a.end_date<t.start_date]
    if not prior:return None
    a=max(prior,key=lambda x:x.end_date)
    if date.fromisoformat(a.end_date)+timedelta(days=1)!=date.fromisoformat(t.start_date):raise HTTPException(422,'权威账户期间不连续，先补齐中间期间，不能跳月桥接')
    report=db.get(Report,a.report_id)
    if report.status!='SIGNED' or period_json(db,db.get(Task,a.task_id))['state']!='LOCKED':raise HTTPException(409,'上期权威报告已失效或重开')
    return a

def bridge_scope(db,t,prior):
    report=db.get(Report,prior.report_id);prior_task=db.get(Task,prior.task_id)
    # Prior balances come from immutable signed report, not mutable task fields.
    controls=report.snapshot['summary']['balance_control'];opening={}
    for side in ['BANK','BOOK']:
        value=t.balances.get(side,{}).get('opening')
        if value is None or money(value,t.currency)!=money(controls[side]['closing'],t.currency):raise HTTPException(422,'期初必须逐侧承接上期已签署期末余额，禁止用差额抵销')
        opening[side]=str(money(value,t.currency))
    allowed={a.id for a in authorities(db,t) if a.end_date<t.start_date}
    items=[]
    for item in db.scalars(select(OutstandingItem).where(OutstandingItem.tenant_id==t.tenant_id,OutstandingItem.account_id==t.account_id,OutstandingItem.authority_id.in_(allowed)).order_by(OutstandingItem.id)):
        n=remaining(db,item,prior.end_date)
        if n:items.append({'id':item.id,'resolution':item.resolution,'remaining_minor':str(n),'origin':item.origin,'authority_id':item.authority_id})
    totals=defaultdict(int)
    for x in items:totals[x['resolution']]+=int(x['remaining_minor'])
    gap=int(opening['BANK'])+totals['BOOK_RECEIPT']-totals['BOOK_PAYMENT']-int(opening['BOOK'])-totals['BANK_RECEIPT']+totals['BANK_PAYMENT']
    if gap:raise HTTPException(422,'期初差异不能由逐项未达证据完整解释，不允许凑平桥接')
    return {'task_id':t.id,'account_id':t.account_id,'currency':t.currency,'start_date':t.start_date,'run_id':t.current_run,
      'prior_authority_id':prior.id,'prior_report_id':report.id,'prior_snapshot_sha256':report.sha256,'prior_end':prior.end_date,'opening_minor':opening,'items':items}

def latest_bridge(db,t):return db.scalar(select(OpeningBridge).where(OpeningBridge.task_id==t.id).order_by(OpeningBridge.created_at.desc(),OpeningBridge.id.desc()))
def assert_bridge(db,t):
    prior=prior_authority(db,t)
    if not prior:return None
    b=latest_bridge(db,t)
    if not b or b.status!='APPROVED' or b.requester_id==b.reviewer_id or not b.reviewer_id:raise HTTPException(422,'缺少本期间有效的独立期初逐项桥接')
    if b.scope_hash!=digest(bridge_scope(db,t,prior)):raise HTTPException(409,'期初桥接输入或上期证据已变化，请重新申请独立复核')
    return b

def carry_totals(db,t):
    totals=defaultdict(int);b=latest_bridge(db,t)
    if not b or b.status!='APPROVED':return totals
    for x in b.scope['items']:
        item=db.get(OutstandingItem,x['id']);n=int(x['remaining_minor'])
        for c in db.scalars(select(Clearing).where(Clearing.task_id==t.id,Clearing.item_id==item.id,Clearing.status.in_(EFFECTIVE))):
            n-=sum(int(a['amount_minor']) for a in c.allocations)
        if n<0:raise HTTPException(409,'清算金额超过期初未达余额')
        totals[item.resolution]+=n
    return totals

def clearing_scope(db,t,item,allocations):
    origin_task=db.get(Task,item.task_id)
    if item.tenant_id!=t.tenant_id or item.account_id!=t.account_id or item.currency!=t.currency or origin_task.end_date>=t.start_date:raise HTTPException(422,'只能清算同账户币种的以前期间未达')
    authority=db.get(AccountPeriod,item.authority_id)
    if authority.status!='APPROVED' or db.get(Report,authority.report_id).status!='SIGNED':raise HTTPException(409,'原始未达权威期间已失效或待撤销')
    if len({a['transaction_id'] for a in allocations})!=len(allocations):raise HTTPException(422,'清算分配不得重复交易')
    used=used_transactions(db,t.id);rows=[]
    for a in allocations:
        x=db.get(Transaction,a['transaction_id']);n=int(a['amount_minor'])
        side='BANK' if item.resolution.startswith('BOOK') else 'BOOK'
        if not x or x.task_id!=t.id or x.tenant_id!=t.tenant_id or x.quality!='VALID' or x.account_id!=item.account_id or x.currency!=item.currency or x.side!=side or (1 if x.amount>0 else -1)!=item.sign or not t.start_date<=x.date<=t.end_date:raise HTTPException(422,'清算交易侧别、方向、账户、币种、日期或质量不符')
        if n<=0 or n>abs(x.amount):raise HTTPException(422,'分配须为正且不得超过源交易绝对额')
        if db.scalar(select(MatchMember).where(MatchMember.transaction_id==x.id,MatchMember.active==True)):raise HTTPException(409,'交易已被逐笔匹配占用')
        rows.append((x.id,x.side,x.date,str(x.amount),x.fingerprint,str(n)))
    return {'item_id':item.id,'origin':item.origin,'task_id':t.id,'run_id':t.current_run,'rows':rows}

def assert_clear_capacity(db,t,item,allocations):
    total=sum(int(a['amount_minor']) for a in allocations)
    if total>remaining(db,item):raise HTTPException(409,'未达余额不足，可能已被其他清算占用')
    used=used_transactions(db,t.id)
    for a in allocations:
        x=db.get(Transaction,a['transaction_id'])
        if used[x.id]+int(a['amount_minor'])>abs(x.amount):raise HTTPException(409,'源交易剩余清算容量不足')

def assert_carry_sign(db,t):
    b=assert_bridge(db,t)
    if any(c.status in ['SUBMITTED','REVERSAL_PENDING'] for c in db.scalars(select(Clearing).where(Clearing.task_id==t.id))):raise HTTPException(422,'清算或撤销仍待独立复核')
    for c in db.scalars(select(Clearing).where(Clearing.task_id==t.id,Clearing.status=='APPROVED')):
        if digest(clearing_scope(db,t,db.get(OutstandingItem,c.item_id),c.allocations))!=c.evidence_hash:raise HTTPException(409,'清算证据已变化')
        if not b or c.item_id not in {x['id'] for x in b.scope['items']}:raise HTTPException(422,'清算项目未列入期初桥接')
    return b

def authority_dependencies(db,a):
    for b in db.scalars(select(OpeningBridge).where(OpeningBridge.tenant_id==a.tenant_id,OpeningBridge.status.in_(EFFECTIVE+['SUBMITTED']))):
        if b.prior_authority_id==a.id or any(x.get('authority_id')==a.id for x in b.scope.get('items',[])):return True
    return False

def forbid_reopen_with_dependencies(db,t):
    active=list(db.scalars(select(AccountPeriod).where(AccountPeriod.task_id==t.id,AccountPeriod.status.in_(EFFECTIVE))))
    if active:raise HTTPException(409,'该任务是权威账户期间，须先逆序撤销后期桥接/清算，再独立撤销权威登记后重开')

def item_json(db,x):
    n=remaining(db,x)
    return {k:getattr(x,k) for k in ['id','task_id','authority_id','case_id','account_id','currency','resolution','origin','version']}|{'amount':amount_text(x.amount,x.currency),'remaining':amount_text(n,x.currency),'cleared':amount_text(x.amount-n,x.currency),'clearance_status':'CLEARED' if n==0 else 'PARTIAL' if n<x.amount else 'OPEN'}
def serial(x):return {col.name:getattr(x,col.name) for col in x.__table__.columns}
def carry_snapshot(db,t):return {'outstanding_control':{'bridges':[serial(x) for x in db.scalars(select(OpeningBridge).where(OpeningBridge.task_id==t.id).order_by(OpeningBridge.id))],
    'clearings':[serial(x) for x in db.scalars(select(Clearing).where(Clearing.task_id==t.id).order_by(Clearing.id))]}}

class Reason(Input):reason:str=Field(min_length=8,max_length=2000)
class VersionReason(Reason):version:int=Field(ge=1)
class AuthorityRequest(Reason):report_id:str
class Allocation(Input):transaction_id:str;amount:str=Field(max_length=40)
class ClearingRequest(Reason):item_id:str;allocations:list[Allocation]=Field(min_length=1,max_length=30)

def install_carry(app):
    from .main import lock,related_task,version_guard,receipt_get,allowed_tasks
    from .services import invalidate_reports,case_event
    DB=Depends(get_db);USER=Depends(current_user)
    @app.get('/api/tasks/{oid}/carry')
    def get_carry(oid:str,user:User=USER,db=DB):
        t=scoped(db,Task,oid,user)
        return {'authorities':[serial(a) for a in db.scalars(select(AccountPeriod).where(AccountPeriod.tenant_id==t.tenant_id,AccountPeriod.account_id==t.account_id).order_by(AccountPeriod.created_at))],'bridge':serial(b) if (b:=latest_bridge(db,t)) else None,
          'clearings':[serial(c) for c in db.scalars(select(Clearing).where(Clearing.task_id==t.id).order_by(Clearing.created_at))],
          'items':[item_json(db,x) for x in db.scalars(select(OutstandingItem).where(OutstandingItem.account_id==t.account_id,OutstandingItem.tenant_id==user.tenant_id))]}
    @app.post('/api/tasks/{oid}/authority')
    def request_authority(oid:str,body:AuthorityRequest,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,oid,user);r=scoped(db,Report,body.report_id,user)
        if r.task_id!=t.id or r.status!='SIGNED' or period_json(db,t)['state']!='LOCKED':raise HTTPException(422,'只能登记本任务已签署锁定的报告作为权威期间')
        if any(a.start_date<=t.end_date and a.end_date>=t.start_date for a in authorities(db,t)):raise HTTPException(409,'账户币种已有重叠权威期间')
        if db.scalar(select(AccountPeriod).where(AccountPeriod.task_id==t.id,AccountPeriod.status=='SUBMITTED')):raise HTTPException(409,'已有权威登记待审核')
        a=AccountPeriod(id=uid(),tenant_id=t.tenant_id,task_id=t.id,report_id=r.id,account_id=t.account_id,currency=t.currency,start_date=t.start_date,end_date=t.end_date,requester_id=user.id,reason=body.reason)
        db.add(a);audit(db,user,'AUTHORITY_REQUEST',t.id,{}, {'authority_id':a.id,'report_id':r.id},body.reason);db.commit();return serial(a)
    @app.post('/api/authorities/{oid}/reversal')
    def revoke_authority(oid:str,body:VersionReason,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);a,t=related_task(db,AccountPeriod,oid,user);version_guard(a,body.version)
        if a.status!='APPROVED':raise HTTPException(409,'权威登记不是有效状态')
        if authority_dependencies(db,a):raise HTTPException(409,'须先撤销后期桥接')
        items=list(db.scalars(select(OutstandingItem).where(OutstandingItem.authority_id==a.id)))
        if any(db.scalar(select(Clearing).where(Clearing.item_id==x.id,Clearing.status.in_(EFFECTIVE+['SUBMITTED']))) for x in items):raise HTTPException(409,'须先撤销后期清算')
        a.status='REVERSAL_PENDING';a.requester_id=user.id;a.reason=body.reason;a.version+=1;a.reviewer_id='';audit(db,user,'AUTHORITY_REVERSAL_REQUEST',t.id,{},serial(a),body.reason);db.commit();return serial(a)
    @app.post('/api/authorities/{oid}/decision')
    def decide_authority(oid:str,body:Decision,user:User=USER,db=DB):
        role(user,'Admin','Reviewer');lock(db);
        if body.action not in ['APPROVE','REJECT']:raise HTTPException(422,'仅支持批准或拒绝本申请')
        a,t=related_task(db,AccountPeriod,oid,user);payload=['decide_authority',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        version_guard(a,body.version)
        if a.requester_id==user.id:raise HTTPException(403,'禁止自审权威期间登记或撤销')
        if a.status not in ['SUBMITTED','REVERSAL_PENDING']:raise HTTPException(409,'登记状态已变化')
        reversal=a.status=='REVERSAL_PENDING'
        if reversal:
            if authority_dependencies(db,a):raise HTTPException(409,'后期桥接仍有效')
            items=list(db.scalars(select(OutstandingItem).where(OutstandingItem.authority_id==a.id)))
            if any(db.scalar(select(Clearing).where(Clearing.item_id==x.id,Clearing.status.in_(EFFECTIVE+['SUBMITTED']))) for x in items):raise HTTPException(409,'后期清算仍有效')
        elif body.action=='APPROVE':
            from .assurance import assert_report_evidence
            assert_report_evidence(db,t)
            if not db.scalar(select(ReportSignature).where(ReportSignature.report_id==a.report_id)):raise HTTPException(422,'历史签署缺少身份封套，不能静默登记权威期间；需受控重开复核')
            if db.get(Report,a.report_id).status!='SIGNED' or period_json(db,t)['state']!='LOCKED':raise HTTPException(409,'签署期间已变化')
            if any(x.id!=a.id and x.start_date<=a.end_date and x.end_date>=a.start_date for x in authorities(db,t)):raise HTTPException(409,'权威期间重叠')
        a.status=('REVERSED' if reversal else 'APPROVED') if body.action=='APPROVE' else ('APPROVED' if reversal else 'REJECTED');a.reviewer_id=user.id;a.review_reason=body.reason;a.version+=1
        if a.status=='APPROVED' and not reversal:
            r=db.get(Report,a.report_id);tx={x['id']:x for x in r.snapshot['transactions']}
            for c in r.snapshot['cases']:
                if c['status']=='CLOSED' and c['resolution'] in TIMING:
                    amounts=[money(tx[i]['amount'],t.currency) for i in c['transaction_ids']]
                    if not amounts or any((n>0)!=(amounts[0]>0) for n in amounts):raise HTTPException(422,'未达原记录方向不一致')
                    item=db.scalar(select(OutstandingItem).where(OutstandingItem.case_id==c['id']))
                    origin={'case':c,'transactions':[tx[i] for i in c['transaction_ids']],'report_id':r.id,'snapshot_sha256':r.sha256,'period_end':t.end_date}
                    if item:
                        if item.origin!=origin:raise HTTPException(409,'旧未达源版本变化，需显式修订项目链')
                        item.authority_id=a.id;item.version+=1
                    else:db.add(OutstandingItem(tenant_id=t.tenant_id,task_id=t.id,authority_id=a.id,case_id=c['id'],account_id=t.account_id,currency=t.currency,resolution=c['resolution'],amount=sum(abs(n) for n in amounts),sign=1 if amounts[0]>0 else -1,origin=origin))
        audit(db,user,'AUTHORITY_'+a.status,t.id,{},serial(a),body.reason);result=serial(a);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result
    @app.post('/api/tasks/{oid}/opening-bridge')
    def request_bridge(oid:str,body:Reason,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,oid,user);require_open_period(db,t)
        prior=prior_authority(db,t)
        if not prior or not t.current_run:raise HTTPException(422,'先完成本期匹配运行及上期权威期间登记')
        old=latest_bridge(db,t)
        if old and old.status in EFFECTIVE+['SUBMITTED']:raise HTTPException(409,'先处理或撤销已有期初桥接')
        scope=bridge_scope(db,t,prior);b=OpeningBridge(id=uid(),tenant_id=t.tenant_id,task_id=t.id,prior_authority_id=prior.id,requester_id=user.id,scope=scope,scope_hash=digest(scope),reason=body.reason)
        db.add(b);t.version+=1;invalidate_reports(db,t.id);audit(db,user,'OPENING_BRIDGE_REQUEST',t.id,{},scope,body.reason);db.commit();return serial(b)
    @app.post('/api/bridges/{oid}/reversal')
    def reverse_bridge(oid:str,body:VersionReason,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);b,t=related_task(db,OpeningBridge,oid,user);version_guard(b,body.version);require_open_period(db,t)
        if b.status!='APPROVED':raise HTTPException(409,'桥接不是已批准状态')
        if db.scalar(select(Clearing).where(Clearing.task_id==t.id,Clearing.status.in_(EFFECTIVE+['SUBMITTED']))):raise HTTPException(409,'先处理或撤销本期清算')
        b.status='REVERSAL_PENDING';b.requester_id=user.id;b.reason=body.reason;b.version+=1;b.reviewer_id='';invalidate_reports(db,t.id);audit(db,user,'BRIDGE_REVERSAL_REQUEST',t.id,{},serial(b),body.reason);db.commit();return serial(b)
    @app.post('/api/bridges/{oid}/decision')
    def decide_bridge(oid:str,body:Decision,user:User=USER,db=DB):
        role(user,'Admin','Reviewer');lock(db);
        if body.action not in ['APPROVE','REJECT']:raise HTTPException(422,'仅支持批准或拒绝本申请')
        b,t=related_task(db,OpeningBridge,oid,user);require_open_period(db,t);payload=['decide_bridge',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        version_guard(b,body.version)
        if b.requester_id==user.id:raise HTTPException(403,'禁止自审期初桥接')
        if b.status not in ['SUBMITTED','REVERSAL_PENDING']:raise HTTPException(409,'桥接状态已变化')
        reversal=b.status=='REVERSAL_PENDING'
        if body.action=='APPROVE' and not reversal:
            prior=prior_authority(db,t)
            if not prior or b.scope_hash!=digest(bridge_scope(db,t,prior)):raise HTTPException(409,'桥接证据版本已变化')
            for other in db.scalars(select(OpeningBridge).where(OpeningBridge.tenant_id==t.tenant_id,OpeningBridge.task_id!=t.id,OpeningBridge.status.in_(EFFECTIVE))):
                ot=db.get(Task,other.task_id)
                if ot.account_id==t.account_id and ot.currency==t.currency and ot.start_date<=t.end_date and ot.end_date>=t.start_date:raise HTTPException(409,'同账户币种的重叠期间已由另一任务承接，先受控撤销其桥接')
        if reversal and db.scalar(select(Clearing).where(Clearing.task_id==t.id,Clearing.status.in_(EFFECTIVE+['SUBMITTED']))):raise HTTPException(409,'本期清算尚未撤销')
        b.status=('REVERSED' if reversal else 'APPROVED') if body.action=='APPROVE' else ('APPROVED' if reversal else 'REJECTED');b.version+=1;b.reviewer_id=user.id;b.review_reason=body.reason;t.version+=1;invalidate_reports(db,t.id);audit(db,user,'BRIDGE_'+b.status,t.id,{},serial(b),body.reason);result=serial(b);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result
    @app.post('/api/tasks/{oid}/clearings')
    def request_clearing(oid:str,body:ClearingRequest,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,oid,user);require_open_period(db,t);b=assert_bridge(db,t);item=scoped(db,OutstandingItem,body.item_id,user)
        if not b or item.id not in {x['id'] for x in b.scope['items']}:raise HTTPException(422,'未达项目未列入本期有效期初桥接')
        allocations=[{'transaction_id':x.transaction_id,'amount_minor':str(money(x.amount,t.currency))} for x in body.allocations]
        scope=clearing_scope(db,t,item,allocations);assert_clear_capacity(db,t,item,allocations)
        c=Clearing(id=uid(),tenant_id=t.tenant_id,task_id=t.id,item_id=item.id,requester_id=user.id,allocations=allocations,evidence_hash=digest(scope),reason=body.reason)
        db.add(c);t.version+=1;invalidate_reports(db,t.id);audit(db,user,'CLEARING_REQUEST',t.id,{},serial(c),body.reason);db.commit();return serial(c)
    @app.post('/api/clearings/{oid}/reversal')
    def request_reverse_clearing(oid:str,body:VersionReason,user:User=USER,db=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);c,t=related_task(db,Clearing,oid,user);version_guard(c,body.version);require_open_period(db,t)
        if c.status!='APPROVED':raise HTTPException(409,'清算不是已批准状态')
        c.status='REVERSAL_PENDING';c.reversal_requester_id=user.id;c.reversal_reason=body.reason;c.version+=1;t.version+=1;invalidate_reports(db,t.id);audit(db,user,'CLEARING_REVERSAL_REQUEST',t.id,{},serial(c),body.reason);db.commit();return serial(c)
    @app.post('/api/clearings/{oid}/decision')
    def decide_clearing(oid:str,body:Decision,user:User=USER,db=DB):
        role(user,'Admin','Reviewer');lock(db);
        if body.action not in ['APPROVE','REJECT']:raise HTTPException(422,'仅支持批准或拒绝本申请')
        c,t=related_task(db,Clearing,oid,user);require_open_period(db,t)
        payload=['clearing',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        version_guard(c,body.version);reversal=c.status=='REVERSAL_PENDING'
        if (c.reversal_requester_id if reversal else c.requester_id)==user.id:raise HTTPException(403,'禁止自审清算或撤销')
        if c.status not in ['SUBMITTED','REVERSAL_PENDING']:raise HTTPException(409,'清算状态已变化')
        item=scoped(db,OutstandingItem,c.item_id,user)
        if body.action=='APPROVE' and not reversal:
            assert_bridge(db,t)
            if digest(clearing_scope(db,t,item,c.allocations))!=c.evidence_hash:raise HTTPException(409,'清算证据已变化')
            assert_clear_capacity(db,t,item,c.allocations)
        before=c.status;c.status=('REVERSED' if reversal else 'APPROVED') if body.action=='APPROVE' else ('APPROVED' if reversal else 'REJECTED');c.reviewer_id=user.id;c.review_reason=body.reason;c.version+=1;item.version+=1;t.version+=1;db.flush()
        ids={x['transaction_id'] for x in c.allocations};done=cleared_ids(db,t)
        # Only fully allocated source rows can close their bank/book-only cases.
        for case in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True)):
            if not ids.intersection(case.transaction_ids):continue
            if set(case.transaction_ids)<=done and case.kind in ['BANK_ONLY','BOOK_ONLY','FEE_REVIEW']:
                if case.status!='CLOSED' or case.resolution=='PRIOR_CLEARING':
                    if case.status=='CLOSED':continue
                    from .assurance import assert_case_evidence
                    assert_case_evidence(db,case,resolution='PRIOR_CLEARING',require_material=False)
                    state=case.status;case.status='CLOSED';case.resolution='PRIOR_CLEARING';case.submitter_id=c.requester_id;case.version+=1;case_event(db,case,user,'CASE_CLEARED_PRIOR_ITEM',state,body.reason)
            elif case.resolution=='PRIOR_CLEARING' and case.status=='CLOSED':
                state=case.status;case.status='INVESTIGATING';case.resolution='';case.version+=1;case_event(db,case,user,'CASE_CLEARING_REVERSED',state,body.reason)
        invalidate_reports(db,t.id);audit(db,user,'CLEARING_'+c.status,t.id,{'status':before},serial(c),body.reason)
        result=serial(c);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result
