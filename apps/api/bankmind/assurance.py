"""BM-001/BM-003: shared current-scope source and evidence controls."""
import hashlib
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import *
from .security import digest,role,scoped,current_user,audit
from .schemas import Input,Decision
from .parsing import money
from .periods import require_open_period

POLICY_VERSION='bankmind-controls-2'

def task_source_scope(db,t):
    rows=list(db.scalars(select(Transaction).where(Transaction.task_id==t.id).order_by(Transaction.id)))
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.task_id==t.id).order_by(ImportBatch.id)))
    balances={}
    for side in ['BANK','BOOK']:
        values=t.balances.get(side,{})
        balances[side]={k:None if values.get(k) is None else str(money(values[k],t.currency)) for k in ['opening','closing']}
    return {'task_id':t.id,'account_id':t.account_id,'currency':t.currency,'start_date':t.start_date,'end_date':t.end_date,
        'run_id':t.current_run,'rules':t.rule_config,'balances_minor':balances,
        'imports':[(b.id,b.sha256,b.status,b.mapping) for b in batches],
        'rows_hash':digest([(x.id,x.side,x.row_no,x.date,str(x.amount),x.currency,x.reference,x.counterparty,x.description,x.group_key,x.quality,x.fingerprint) for x in rows])}

def bind_source(db,t,s):
    scope=task_source_scope(db,t)
    b=SourceControlBinding(tenant_id=t.tenant_id,task_id=t.id,source_control_id=s.id,guard_hash=digest(scope),scope=scope)
    db.add(b);return b

def require_source_binding(db,t,s):
    b=db.scalar(select(SourceControlBinding).where(SourceControlBinding.source_control_id==s.id,SourceControlBinding.tenant_id==t.tenant_id))
    if not b or b.guard_hash!=digest(task_source_scope(db,t)):
        raise HTTPException(409,'来源声明未绑定当前输入、期间、规则与余额版本，请重新声明并独立复核')
    return b

def assert_source_control(db,t):
    from .controls import check_source,SourceDeclaration
    s=db.scalar(select(SourceControl).where(SourceControl.task_id==t.id,SourceControl.tenant_id==t.tenant_id).order_by(SourceControl.created_at.desc(),SourceControl.id.desc()))
    if not s:raise HTTPException(422,'缺少来源控制声明，演示任务也不能跳过后签署正式完成结论')
    if s.status!='VERIFIED' or not s.reviewer_id or s.reviewer_id==s.creator_id:
        raise HTTPException(422,'最新来源声明尚未通过有效的独立复核')
    require_source_binding(db,t,s)
    if any(v['status']!='PASS' for v in check_source(db,t,SourceDeclaration.model_validate(s.declaration)).values()):
        raise HTTPException(422,'来源控制行数、净额或质量不一致，需重新声明并复核')
    return s

def requirements(db,c):
    return list(db.scalars(select(EvidenceRequirement).where(EvidenceRequirement.case_id==c.id,EvidenceRequirement.tenant_id==c.tenant_id).order_by(EvidenceRequirement.created_at,EvidenceRequirement.id)))

def ensure_requirements(db,c):
    qs=requirements(db,c);seen={i for q in qs for i in q.attachment_history+([q.current_attachment_id] if q.current_attachment_id else [])}
    for a in db.scalars(select(EvidenceAttachment).where(EvidenceAttachment.case_id==c.id,EvidenceAttachment.tenant_id==c.tenant_id)):
        if a.id not in seen:
            q=EvidenceRequirement(id=uid(),tenant_id=c.tenant_id,task_id=c.task_id,case_id=c.id,purpose=a.purpose,required=True,current_attachment_id=a.id,attachment_history=[])
            db.add(q);qs.append(q)
    if not any(q.required for q in qs):
        q=EvidenceRequirement(id=uid(),tenant_id=c.tenant_id,task_id=c.task_id,case_id=c.id,purpose='SUPPORT',required=True,current_attachment_id='',attachment_history=[])
        db.add(q);qs.append(q)
    db.flush();return qs

def attach_requirement(db,c,a,necessary=True,replaces=''):
    qs=ensure_requirements(db,c)
    if replaces:
        q=next((q for q in qs if q.current_attachment_id==replaces),None)
        if not q:raise HTTPException(409,'被替换材料已不是当前版本，请刷新')
        old=db.get(EvidenceAttachment,replaces)
        if not old or old.case_id!=c.id or old.purpose!=a.purpose:raise HTTPException(422,'替换件必须属于同一异常和材料类别')
        generated=next((x for x in qs if x.current_attachment_id==a.id and x.id!=q.id),None)
        if generated:db.delete(generated)
        q.attachment_history=[*q.attachment_history,replaces];q.current_attachment_id=a.id;q.waiver_id='';q.version+=1
    else:
        # Ignore the just-created attachment when locating its generated legacy slot.
        generated=next((q for q in qs if q.current_attachment_id==a.id),None)
        empty=next((q for q in qs if q.required and not q.current_attachment_id and q.purpose==a.purpose),None)
        if necessary and empty:
            if generated:db.delete(generated)
            q=empty;q.current_attachment_id=a.id;q.version+=1
        elif generated:q=generated;q.required=necessary
        else:
            q=EvidenceRequirement(id=uid(),tenant_id=c.tenant_id,task_id=c.task_id,case_id=c.id,purpose=a.purpose,required=necessary,current_attachment_id=a.id,attachment_history=[])
            db.add(q)
    return q

def evidence_scope(db,c,resolution=None):
    t=db.get(Task,c.task_id)
    rows=[db.get(Transaction,i) for i in sorted(set(c.transaction_ids+c.candidates))]
    return {'case_id':c.id,'kind':c.kind,'task_id':t.id,'account_id':t.account_id,'currency':t.currency,
        'start_date':t.start_date,'end_date':t.end_date,'run_id':c.run_id,'resolution':resolution if resolution is not None else c.resolution,
        'rows':[(x.id,x.side,x.date,str(x.amount),x.currency,x.quality,x.fingerprint) for x in rows]}

def attachment_integrity(a):
    p=(STORAGE/a.object_key).resolve()
    return p.is_relative_to(STORAGE.resolve()) and p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==a.sha256

def requirement_json(db,q):
    w=db.get(EvidenceWaiver,q.waiver_id) if q.waiver_id else None
    return {**{k:getattr(q,k) for k in ['id','task_id','case_id','purpose','required','current_attachment_id','attachment_history','version','waiver_id']},
        'waiver':{k:getattr(w,k) for k in ['id','requester_id','reviewer_id','status','version','reason','review_reason','scope','scope_hash','requirement_version','approved_requirement_version','case_version_at_request','created_at','decided_at']} if w else None}

def attachment_metadata(db,a):
    qs=list(db.scalars(select(EvidenceRequirement).where(EvidenceRequirement.case_id==a.case_id)))
    q=next((q for q in qs if q.current_attachment_id==a.id or a.id in q.attachment_history),None)
    return {'requirement_id':q.id if q else '', 'necessary':q.required if q else True,
        'current_evidence':not q or q.current_attachment_id==a.id,
        'replaced_by':q.current_attachment_id if q and q.current_attachment_id!=a.id else '',
        'requirement_version':q.version if q else None}

def assert_case_evidence(db,c,resolution=None,require_material=True):
    resolution=resolution if resolution is not None else c.resolution
    qs=requirements(db,c);covered={i for q in qs for i in q.attachment_history+([q.current_attachment_id] if q.current_attachment_id else [])}
    legacy=[a for a in db.scalars(select(EvidenceAttachment).where(EvidenceAttachment.case_id==c.id,EvidenceAttachment.tenant_id==c.tenant_id)) if a.id not in covered]
    required=[q for q in qs if q.required]
    if require_material and not required and not legacy:
        raise HTTPException(422,'缺少必要支持材料；请补充可独立核验的材料，或申请有范围的独立证据豁免')
    for q in required:
        a=db.get(EvidenceAttachment,q.current_attachment_id) if q.current_attachment_id else None
        verified=bool(a and a.case_id==c.id and a.tenant_id==c.tenant_id and a.status=='VERIFIED' and a.reviewer_id and a.reviewer_id!=a.uploader_id and attachment_integrity(a))
        if verified:continue
        w=db.get(EvidenceWaiver,q.waiver_id) if q.waiver_id else None
        waived=bool(w and w.status=='APPROVED' and w.reviewer_id and w.reviewer_id!=w.requester_id
            and w.approved_requirement_version==q.version and w.scope_hash==digest(evidence_scope(db,c,resolution)))
        if not waived:raise HTTPException(422,'必要材料缺失、待核验、已退回或豁免已过期；请提交合格替换件或独立豁免')
    for a in legacy:
        if a.status!='VERIFIED' or not a.reviewer_id or a.reviewer_id==a.uploader_id or not attachment_integrity(a):
            raise HTTPException(422,'历史必要材料未通过独立核验；需替换或显式豁免，不能把退回视为通过')

def assert_report_evidence(db,t):
    assert_source_control(db,t)
    for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.tenant_id==t.tenant_id,Case.active==True,Case.status=='CLOSED')):
        assert_case_evidence(db,c,require_material=c.resolution not in ['MATCH_APPROVED','PRIOR_CLEARING'])

def assurance_snapshot(db,t):
    case_ids=list(db.scalars(select(Case.id).where(Case.task_id==t.id)))
    invocation_ids=list(db.scalars(select(AIInvocation.id).where(AIInvocation.object_id.in_(case_ids))))
    return {'policy_version':POLICY_VERSION,
        'ai_provider_outcomes':[{col.name:getattr(o,col.name) for col in o.__table__.columns} for o in db.scalars(select(AIOutcome).where(AIOutcome.invocation_id.in_(invocation_ids)).order_by(AIOutcome.id))],
        'source_bindings':[{k:getattr(b,k) for k in ['id','source_control_id','guard_hash','scope','reviewer_snapshot','created_at']} for b in db.scalars(select(SourceControlBinding).where(SourceControlBinding.task_id==t.id).order_by(SourceControlBinding.id))],
        'evidence_requirements':[requirement_json(db,q) for q in db.scalars(select(EvidenceRequirement).where(EvidenceRequirement.task_id==t.id).order_by(EvidenceRequirement.id))],
        'evidence_waivers':[{k:getattr(w,k) for k in ['id','requirement_id','case_id','requester_id','reviewer_id','status','version','scope','scope_hash','reason','review_reason','requirement_version','approved_requirement_version','case_version_at_request','created_at','decided_at']} for w in db.scalars(select(EvidenceWaiver).where(EvidenceWaiver.task_id==t.id).order_by(EvidenceWaiver.id))]}

class RequirementInit(Input):
    case_version:int=Field(ge=1)
    reason:str=Field(min_length=8,max_length=2000)

class WaiverRequest(Input):
    case_version:int=Field(ge=1)
    requirement_version:int=Field(ge=1)
    resolution:Literal['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT','DATA_ERROR','OTHER','MATCH_APPROVED']
    reason:str=Field(min_length=12,max_length=2000)

class WaiverDecision(Decision):
    case_version:int=Field(ge=1)

def install_assurance(app):
    from .main import lock,related_task,version_guard,receipt_get
    from .services import invalidate_reports,case_event
    DB=Depends(get_db);USER=Depends(current_user)
    @app.post('/api/tasks/{oid}/revisions')
    def revise_task(oid:str,body:RequirementInit,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant');lock(db);old=scoped(db,Task,oid,user);version_guard(old,body.case_version)
        t=Task(id=uid(),tenant_id=user.tenant_id,account_id=old.account_id,name=old.name+' · 修订',currency=old.currency,start_date=old.start_date,end_date=old.end_date,creator_id=user.id,rule_config=dict(old.rule_config),balances={'BANK':{},'BOOK':{}})
        db.add(t);db.add(TaskRevision(tenant_id=user.tenant_id,task_id=t.id,parent_task_id=old.id,actor_id=user.id,reason=body.reason));audit(db,user,'TASK_REVISION_CREATE',old.id,{'task_id':old.id,'version':old.version},{'revision_id':t.id,'state':'DRAFT'},body.reason);db.commit();return {'id':t.id,'parent_task_id':old.id}
    @app.get('/api/tasks/{oid}/revision-chain')
    def revision_chain(oid:str,user:User=USER,db:Session=DB):
        t=scoped(db,Task,oid,user);rows=list(db.scalars(select(TaskRevision).where(TaskRevision.tenant_id==user.tenant_id)))
        ids={oid};changed=True
        while changed:
            changed=False
            for x in rows:
                if (x.task_id in ids or x.parent_task_id in ids) and not {x.task_id,x.parent_task_id}<=ids:ids.update([x.task_id,x.parent_task_id]);changed=True
        return [{k:getattr(x,k) for k in ['task_id','parent_task_id','actor_id','reason','created_at']} for x in rows if x.task_id in ids and db.get(Task,x.task_id).account_id in user.account_ids]
    @app.get('/api/cases/{oid}/evidence-requirements')
    def list_requirements(oid:str,user:User=USER,db:Session=DB):
        c,t=related_task(db,Case,oid,user)
        return [requirement_json(db,q) for q in requirements(db,c)]
    @app.post('/api/cases/{oid}/evidence-requirements')
    def initialize(oid:str,body:RequirementInit,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);c,t=related_task(db,Case,oid,user);require_open_period(db,t);version_guard(c,body.case_version)
        if not c.active or c.status=='CLOSED':raise HTTPException(409,'请先重新调查有效异常')
        qs=ensure_requirements(db,c);c.version+=1;t.version+=1;invalidate_reports(db,t.id)
        case_event(db,c,user,'EVIDENCE_REQUIREMENTS_INITIALIZED',c.status,body.reason);db.commit()
        return {'case_version':c.version,'items':[requirement_json(db,q) for q in qs]}
    @app.post('/api/evidence-requirements/{oid}/waivers')
    def request_waiver(oid:str,body:WaiverRequest,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);q,t=related_task(db,EvidenceRequirement,oid,user);require_open_period(db,t)
        c=scoped(db,Case,q.case_id,user);version_guard(c,body.case_version);version_guard(q,body.requirement_version)
        if not c.active or c.status=='CLOSED' or not q.required:raise HTTPException(409,'仅有效未结案的必要材料可申请豁免')
        if q.waiver_id:
            old=db.get(EvidenceWaiver,q.waiver_id)
            if old and old.status=='SUBMITTED':raise HTTPException(409,'已有豁免申请待独立复核')
        scope=evidence_scope(db,c,body.resolution);q.version+=1;c.version+=1;t.version+=1
        w=EvidenceWaiver(id=uid(),tenant_id=user.tenant_id,task_id=t.id,case_id=c.id,requirement_id=q.id,requester_id=user.id,
            requirement_version=q.version,case_version_at_request=c.version,scope=scope,scope_hash=digest(scope),reason=body.reason)
        db.add(w);q.waiver_id=w.id;invalidate_reports(db,t.id);case_event(db,c,user,'EVIDENCE_WAIVER_REQUEST',c.status,body.reason)
        db.commit();return requirement_json(db,q)
    @app.post('/api/evidence-waivers/{oid}/decision')
    def decide_waiver(oid:str,body:WaiverDecision,user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer');lock(db);w,t=related_task(db,EvidenceWaiver,oid,user);require_open_period(db,t)
        payload=['waiver',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        c=scoped(db,Case,w.case_id,user);q=scoped(db,EvidenceRequirement,w.requirement_id,user)
        version_guard(w,body.version);version_guard(c,body.case_version)
        if w.requester_id==user.id:raise HTTPException(403,'禁止自审证据豁免')
        if not c.active or c.status=='CLOSED' or w.status!='SUBMITTED' or q.waiver_id!=w.id or q.version!=w.requirement_version:raise HTTPException(409,'豁免或材料版本已变化')
        if digest(evidence_scope(db,c,w.scope['resolution']))!=w.scope_hash:raise HTTPException(409,'豁免范围证据已变化，请重新申请')
        w.status='APPROVED' if body.action=='APPROVE' else 'REJECTED';w.reviewer_id=user.id;w.review_reason=body.reason;w.decided_at=now();w.version+=1
        q.version+=1;w.approved_requirement_version=q.version if w.status=='APPROVED' else 0
        c.version+=1;t.version+=1;invalidate_reports(db,t.id);case_event(db,c,user,'EVIDENCE_WAIVER_'+w.status,c.status,body.reason)
        result=requirement_json(db,q)
        db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result
