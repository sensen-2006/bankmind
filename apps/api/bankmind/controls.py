"""Additive B2B controls. No original records or accounting entries are edited."""
import io
import os
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from fastapi import Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import *
from .schemas import Input, Decision
from .security import current_user, scoped, role, audit, digest
from .periods import require_open_period
from .parsing import money, amount_text
from .services import rows_for_task, match_guard, invalidate_reports, case_event, reserve, active_matches
from .matching import ENGINE_VERSION
from .ai import ProviderName, probe, provider_options

class MatchVersion(Input):
    id: str
    version: int = Field(ge=1)

class BulkReview(Input):
    items: list[MatchVersion] = Field(min_length=1,max_length=100)
    reason: str = Field(min_length=8,max_length=2000)
    idempotency_key: str = Field(min_length=8,max_length=100)

class CombinationBulk(Input):
    items: list[MatchVersion] = Field(min_length=1,max_length=20)
    reason: str = Field(min_length=8,max_length=2000)
    idempotency_key: str = Field(min_length=8,max_length=100)

class SideControl(Input):
    rows: int = Field(ge=0,le=1000000)
    net_amount: str = Field(max_length=40)

class SourceDeclaration(Input):
    BANK: SideControl
    BOOK: SideControl
    source_reference: str = Field(min_length=4,max_length=200)
    statement: str = Field(min_length=12,max_length=2000)

def attachment_json(a,db=None):
    from .assurance import attachment_metadata
    return {**{k:getattr(a,k) for k in ['id','task_id','case_id','uploader_id','reviewer_id','filename','sha256','size','purpose','external_reference','note','status','version','created_at']},**(attachment_metadata(db,a) if db else {})}

def source_json(s,db=None,t=None):
    result={k:getattr(s,k) for k in ['id','task_id','creator_id','reviewer_id','declaration','result','status','version','created_at']}
    if db is not None:
        from .assurance import task_source_scope
        b=db.scalar(select(SourceControlBinding).where(SourceControlBinding.source_control_id==s.id))
        result['scope_current']=bool(b and b.guard_hash==digest(task_source_scope(db,t or db.get(Task,s.task_id))))
        result['bound_scope_hash']=b.guard_hash if b else ''
    return result

def evidence_snapshot(db,task):
    return {'attachments':[attachment_json(a,db) for a in db.scalars(select(EvidenceAttachment).where(EvidenceAttachment.task_id==task.id).order_by(EvidenceAttachment.id))],
            'source_controls':[source_json(s,db,task) for s in db.scalars(select(SourceControl).where(SourceControl.task_id==task.id).order_by(SourceControl.id))]}

def check_source(db,t,body):
    rows=rows_for_task(db,t);result={}
    for side in ['BANK','BOOK']:
        r=[x for x in rows if x.side==side]
        expected=getattr(body,side)
        try: net=money(expected.net_amount,t.currency)
        except ValueError: raise HTTPException(422,'源控制净额格式或精度错误')
        actual=sum(x.amount for x in r if x.currency==t.currency)
        ok=len(r)==expected.rows and actual==net and all(x.quality=='VALID' for x in r)
        result[side]={'status':'PASS' if ok else 'FAIL','actual_rows':len(r),'actual_net':amount_text(actual,t.currency),'declared_rows':expected.rows,'declared_net':expected.net_amount}
    return result

def install_controls(app):
    from .main import lock, related_task, version_guard, receipt_get, allowed_tasks
    DB=Depends(get_db);USER=Depends(current_user)

    @app.get('/api/ai/config')
    def ai_config(user:User=USER):
        return {'mode':os.getenv('LLM_MODE','mock'),'external_enabled':os.getenv('LLM_ALLOW_EXTERNAL','false').lower()=='true',
                'provider_host':urlsplit(os.getenv('LLM_BASE_URL','')).hostname or '',
                'model':os.getenv('LLM_MODEL',''),'configured':all(os.getenv(k) for k in ['LLM_API_KEY','LLM_BASE_URL','LLM_MODEL']),
                'providers':provider_options()}

    @app.post('/api/ai/probe/{provider}')
    def ai_probe(provider:ProviderName,confirm_external:bool=False,user:User=USER):
        role(user,'Admin','Accountant','Reviewer')
        if provider != 'mock' and not confirm_external:
            raise HTTPException(400,'请先确认本次模型连通测试')
        return probe(provider)

    @app.get('/api/ai/reliability')
    def ai_reliability(user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer','Auditor')
        ids={t.id for t in allowed_tasks(db,user)};case_ids=list(db.scalars(select(Case.id).where(Case.task_id.in_(ids))))
        calls=list(db.scalars(select(AIInvocation).where(AIInvocation.tenant_id==user.tenant_id,AIInvocation.object_id.in_(case_ids))))
        outcomes={o.invocation_id:o for o in db.scalars(select(AIOutcome).where(AIOutcome.tenant_id==user.tenant_id))}
        groups={}
        for c in calls:
            g=groups.setdefault((c.provider,c.model),{'attempted_provider':c.provider,'model':c.model,'total':0,'provider_success':0,'fallback':0,'timeouts':0,'discarded':0,'network_attempts':0,'legacy_unknown':0,'latencies_ms':[]})
            g['total']+=1;g['latencies_ms'].append(c.elapsed_ms);g['discarded']+=c.status in ['STALE','DISCARDED']
            o=outcomes.get(c.id)
            if o:g['provider_success']+=o.provider_status=='SUCCEEDED';g['fallback']+=o.provider_status=='FALLBACK';g['timeouts']+='timeout' in o.provider_error_code.lower();g['network_attempts']+=o.network_attempts
            else:g['legacy_unknown']+=1
        for g in groups.values():
            values=sorted(g.pop('latencies_ms'));g['average_ms']=round(sum(values)/len(values));g['p95_ms']=values[max(0,__import__('math').ceil(len(values)*.95)-1)]
        return {'scope':'授权范围内历史调用记录；Mock、真实服务与历史缺失状态分开统计，不代表供应商SLA或异常根因准确率','groups':list(groups.values()),'live_acceptance':'本轮无新增真实调用；真实正常成功率仍需另行授权验收'}

    @app.post('/api/reviews/bulk')
    def bulk_review(body:BulkReview,user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer');lock(db)
        payload=['bulk',body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        if len({v.id for v in body.items})!=len(body.items): raise HTTPException(422,'匹配组不能重复')
        chosen=[];tasks={};seen=set()
        # Validate the entire batch before any mutation. One failure rolls it all back.
        for v in body.items:
            m,t=related_task(db,Match,v.id,user);version_guard(m,v.version);require_open_period(db,t)
            run=db.get(Run,m.run_id)
            if not run or not run.rule_version.startswith(ENGINE_VERSION+':'):raise HTTPException(409,'匹配引擎已更新，请重新运行后再批量复核')
            if m.creator_id==user.id:raise HTTPException(403,'批量复核同样禁止自审')
            if not m.active or m.status!='DETERMINISTIC' or m.method not in ['L1_REFERENCE','L2_EXACT'] or m.evidence.get('auto_blockers'):
                raise HTTPException(422,'批量仅限无风险阻断的程序确定L1/L2；模糊、组合、歧义须逐组复核')
            match_guard(db,t,m.bank_ids,m.book_ids)
            ids=set(m.bank_ids+m.book_ids)
            if seen.intersection(ids):raise HTTPException(409,'批次交易重复')
            seen.update(ids)
            occupied=list(db.scalars(select(MatchMember).where(MatchMember.transaction_id.in_(ids),MatchMember.active==True)))
            if {x.transaction_id for x in occupied}!=ids or any(x.match_id!=m.id for x in occupied):raise HTTPException(409,'交易占用发生变化')
            for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True,Case.status!='CLOSED')):
                if ids.intersection(c.transaction_ids+c.candidates):raise HTTPException(422,'关联未解决异常，必须逐组调查')
            chosen.append(m);tasks[t.id]=t
        for m in chosen:
            m.status='APPROVED';m.reviewer_id=user.id;m.version+=1
            audit(db,user,'MATCH_BULK_APPROVE',m.id,{'status':'DETERMINISTIC'},{'status':m.status,'version':m.version},body.reason)
        for t in tasks.values():t.version+=1;invalidate_reports(db,t.id)
        result={'count':len(chosen),'items':[{'id':m.id,'version':m.version,'status':m.status} for m in chosen]}
        db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result

    @app.post('/api/tasks/{task_id}/reviews/bulk-combinations')
    def bulk_combinations(task_id:str,body:CombinationBulk,user:User=USER,db:Session=DB):
        """Bulk-preview confirmation of exact-total 1:N/N:1 suggestions.

        This is deliberately narrower than the L1/L2 bulk endpoint: each selected
        group must be a current L4 suggestion with exact totals, no auto-blocker,
        no overlapping active case and no transaction overlap. All groups are
        validated before any mutation; every group still gets its own audit event.
        """
        role(user,'Admin','Reviewer');lock(db);t=scoped(db,Task,task_id,user);require_open_period(db,t)
        # Item order is not semantic; normalize it so a retry with the same set
        # does not produce a false idempotency conflict.
        normalized={**body.model_dump(),'items':sorted(body.model_dump()['items'],key=lambda x:(x['id'],x['version']))}
        payload=['bulk-combinations',task_id,normalized];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        if len({v.id for v in body.items})!=len(body.items):raise HTTPException(422,'组合匹配组不能重复')
        chosen=[];seen=set()
        for v in body.items:
            m=scoped(db,Match,v.id,user);version_guard(m,v.version)
            if m.task_id!=t.id:raise HTTPException(404,'组合匹配不属于当前任务')
            run=db.get(Run,m.run_id)
            if not run or not run.rule_version.startswith(ENGINE_VERSION+':') or m.run_id!=t.current_run:
                raise HTTPException(409,'匹配引擎或当前运行已变化，请刷新后重新复核')
            if m.creator_id==user.id:raise HTTPException(403,'批量复核同样禁止自审')
            if not m.active or m.status!='PENDING' or m.method not in ['L4_ONE_TO_MANY','L4_MANY_TO_ONE']:
                raise HTTPException(422,'批量组合仅限当前待确认的L4一对多/多对一')
            if m.evidence.get('auto_blockers'):raise HTTPException(422,'含风险阻断的组合必须逐组复核')
            match_guard(db,t,m.bank_ids,m.book_ids)
            ids=set(m.bank_ids+m.book_ids)
            if seen.intersection(ids):raise HTTPException(409,'批次交易重复')
            seen.update(ids)
            occupied=list(db.scalars(select(MatchMember).where(MatchMember.transaction_id.in_(ids),MatchMember.active==True)))
            if occupied:raise HTTPException(409,'组合组已有交易占用，请刷新后重试')
            for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True,Case.status!='CLOSED')):
                if ids.intersection(c.transaction_ids+c.candidates):raise HTTPException(422,'关联未解决异常必须逐组调查')
            chosen.append(m)
        for m in chosen:
            reserve(db,m)
            m.status='APPROVED';m.reviewer_id=user.id;m.version+=1
            approved_ids=set(m.bank_ids+m.book_ids)
            # Match the single-approval supersede rule so a batch cannot leave
            # overlapping PENDING suggestions behind (which would block sign-off).
            for other in active_matches(db,t):
                if other.id!=m.id and other.status=='PENDING' and approved_ids.intersection(other.bank_ids+other.book_ids):
                    other.active=False;other.status='SUPERSEDED';other.version+=1
            audit(db,user,'MATCH_COMBINATION_BULK_APPROVE',m.id,{'status':'PENDING'},{'status':m.status,'version':m.version},body.reason)
        t.version+=1;invalidate_reports(db,t.id)
        result={'count':len(chosen),'items':[{'id':m.id,'version':m.version,'status':m.status} for m in chosen],
                'note':'仅精确合计、无风险阻断的L4组合；每组保留独立审计事件'}
        db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result

    @app.get('/api/cases/{oid}/attachments')
    def attachments(oid:str,user:User=USER,db:Session=DB):
        c,t=related_task(db,Case,oid,user)
        return [attachment_json(a,db) for a in db.scalars(select(EvidenceAttachment).where(EvidenceAttachment.case_id==c.id).order_by(EvidenceAttachment.created_at))]

    @app.post('/api/cases/{oid}/attachments')
    def add_attachment(oid:str,file:UploadFile=File(...),purpose:Literal['SUPPORT','CORRECTION_RECEIPT']=Form('SUPPORT'),external_reference:str=Form(''),note:str=Form(...),necessary:bool=Form(True),replaces_attachment_id:str=Form(''),original_filename:str=Form(''),user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant','Reviewer');lock(db);c,t=related_task(db,Case,oid,user);require_open_period(db,t)
        if not c.active or c.status=='CLOSED':raise HTTPException(409,'请在有效且未关闭的异常中补充材料')
        if not 4<=len(note)<=2000 or len(external_reference)>160:raise HTTPException(422,'材料说明至少四字，外部凭证号最多160字')
        if purpose=='CORRECTION_RECEIPT' and not external_reference.strip():raise HTTPException(422,'更正回执必须填写外部系统凭证号')
        filename=(original_filename or file.filename or '').replace('\\','/').split('/')[-1]
        if not filename or len(filename)>200:raise HTTPException(422,'文件名无效')
        ext=Path(filename).suffix.lower();raw=file.file.read(5*1024*1024+1)
        if not raw or len(raw)>5*1024*1024:raise HTTPException(413,'附件需非空且不超过5MB')
        if ext=='.txt':
            try:
                content=raw.decode('utf-8-sig')
                if '\x00' in content:raise ValueError()
            except (UnicodeError,ValueError):raise HTTPException(422,'TXT必须为UTF-8文本')
        elif ext=='.pdf':
            try:
                from pypdf import PdfReader
                if not raw.startswith(b'%PDF-'):raise ValueError()
                pdf=PdfReader(io.BytesIO(raw),strict=True)
                if pdf.is_encrypted or not 1<=len(pdf.pages)<=100:raise ValueError()
            except Exception:raise HTTPException(422,'PDF损坏、加密或超过100页；仅归档，不执行OCR')
        else:raise HTTPException(422,'证据附件支持PDF和UTF-8 TXT；不接受可执行或网页文件')
        sha=hashlib.sha256(raw).hexdigest()
        old=db.scalar(select(EvidenceAttachment).where(EvidenceAttachment.case_id==c.id,EvidenceAttachment.sha256==sha))
        if old and not replaces_attachment_id:return attachment_json(old,db)
        oid2=uid();folder=STORAGE/'uploads';folder.mkdir(exist_ok=True)
        object_key='uploads/evidence-'+oid2+ext
        (STORAGE/object_key).write_bytes(raw)
        a=EvidenceAttachment(id=oid2,tenant_id=user.tenant_id,task_id=t.id,case_id=c.id,uploader_id=user.id,filename=filename,object_key=object_key,sha256=sha,size=len(raw),purpose=purpose,external_reference=external_reference,note=note)
        from .assurance import attach_requirement
        db.add(a);db.flush();attach_requirement(db,c,a,necessary,replaces_attachment_id)
        c.version+=1;t.version+=1;invalidate_reports(db,t.id)
        case_event(db,c,user,'EVIDENCE_SUBMITTED',c.status,note);db.commit();return attachment_json(a,db)

    @app.get('/api/attachments/{oid}/original')
    def attachment_download(oid:str,user:User=USER,db:Session=DB):
        a,t=related_task(db,EvidenceAttachment,oid,user)
        p=(STORAGE/a.object_key).resolve()
        if not p.is_relative_to(STORAGE.resolve()) or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=a.sha256:raise HTTPException(409,'附件缺失或哈希异常')
        lock(db);audit(db,user,'EVIDENCE_DOWNLOAD',a.id,{}, {'sha256':a.sha256},'授权下载原始证据附件');db.commit()
        return FileResponse(p,filename=a.filename,media_type='application/octet-stream')

    @app.post('/api/attachments/{oid}/decision')
    def attachment_review(oid:str,body:Decision,user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer');lock(db);a,t=related_task(db,EvidenceAttachment,oid,user);require_open_period(db,t)
        payload=['evidence',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        version_guard(a,body.version)
        if a.uploader_id==user.id:raise HTTPException(403,'禁止自审附件或外部更正回执')
        if a.status!='SUBMITTED':raise HTTPException(409,'材料已复核')
        a.status='VERIFIED' if body.action=='APPROVE' else 'REJECTED';a.reviewer_id=user.id;a.version+=1
        for q in db.scalars(select(EvidenceRequirement).where(EvidenceRequirement.current_attachment_id==a.id)):
            q.waiver_id='';q.version+=1
        c=scoped(db,Case,a.case_id,user);c.version+=1;t.version+=1;invalidate_reports(db,t.id)
        case_event(db,c,user,'EVIDENCE_'+a.status,c.status,body.reason)
        result=attachment_json(a,db);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result

    @app.get('/api/tasks/{task_id}/source-controls')
    def source_controls(task_id:str,user:User=USER,db:Session=DB):
        t=scoped(db,Task,task_id,user)
        return [source_json(s,db,t) for s in db.scalars(select(SourceControl).where(SourceControl.task_id==t.id).order_by(SourceControl.created_at.desc(),SourceControl.id))]

    @app.post('/api/tasks/{task_id}/source-controls')
    def declare_source(task_id:str,body:SourceDeclaration,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant');lock(db);t=scoped(db,Task,task_id,user);require_open_period(db,t)
        if not t.current_run:raise HTTPException(422,'先完成输入冻结与匹配运行，再提交源控制声明')
        result=check_source(db,t,body)
        s=SourceControl(tenant_id=user.tenant_id,task_id=t.id,creator_id=user.id,declaration=body.model_dump(),result=result)
        db.add(s);t.version+=1;invalidate_reports(db,t.id);db.flush()
        from .assurance import bind_source
        bind_source(db,t,s)
        audit(db,user,'SOURCE_CONTROL_DECLARE',s.id,{},result,body.statement);db.commit();return source_json(s)

    @app.post('/api/source-controls/{oid}/decision')
    def review_source(oid:str,body:Decision,user:User=USER,db:Session=DB):
        role(user,'Admin','Reviewer');lock(db);s,t=related_task(db,SourceControl,oid,user);require_open_period(db,t)
        payload=['source',oid,body.model_dump()];old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        version_guard(s,body.version)
        if s.creator_id==user.id:raise HTTPException(403,'禁止自审源控制声明')
        if s.status!='SUBMITTED':raise HTTPException(409,'声明已复核')
        from .assurance import require_source_binding
        binding=require_source_binding(db,t,s) if body.action=='APPROVE' else None
        s.result=check_source(db,t,SourceDeclaration.model_validate(s.declaration))
        if body.action=='APPROVE' and any(r['status']!='PASS' for r in s.result.values()):raise HTTPException(422,'源控制行数、净额或质量不一致，不能批准')
        if binding:binding.reviewer_snapshot={'id':user.id,'name':user.name,'username':user.username,'role':user.role,'reviewed_at':now()}
        before=s.status;s.status='VERIFIED' if body.action=='APPROVE' else 'REJECTED';s.reviewer_id=user.id;s.version+=1;t.version+=1;invalidate_reports(db,t.id)
        audit(db,user,'SOURCE_CONTROL_'+s.status,s.id,{'status':before},{'status':s.status},body.reason)
        result=source_json(s);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result

    @app.get('/api/outstanding')
    def outstanding(as_of:str='',user:User=USER,db:Session=DB):
        # Account-wide carry view; no invented clearance or automatic write-off.
        from datetime import date
        try: at=date.fromisoformat(as_of) if as_of else date.today()
        except ValueError:raise HTTPException(422,'账龄日期无效')
        tasks={t.id:t for t in allowed_tasks(db,user)};items=[]
        for c in db.scalars(select(Case).where(Case.task_id.in_(tasks),Case.active==True,Case.status=='CLOSED',Case.resolution.in_(['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']))):
            t=tasks[c.task_id]
            if t.end_date>at.isoformat():continue
            tx=[db.get(Transaction,i) for i in c.transaction_ids]
            formed=min(x.date for x in tx);age=max(0,(at-date.fromisoformat(formed)).days)
            items.append({'case_id':c.id,'task_id':t.id,'task_name':t.name,'account_id':t.account_id,'currency':t.currency,'resolution':c.resolution,'formed_date':formed,'period_end':t.end_date,'age_days':age,'aging_bucket':'90+' if age>90 else '31–90' if age>30 else '0–30','overdue':bool(c.due_date and c.due_date<at.isoformat()),'owner_id':c.owner_id,'due_date':c.due_date,'amount':amount_text(sum(abs(x.amount) for x in tx),t.currency),'note':c.note,'clearance_status':'UNVERIFIED'})
        from .carry import remaining
        for row in items:
            item=db.scalar(select(OutstandingItem).where(OutstandingItem.case_id==row['case_id']))
            authority=db.get(AccountPeriod,item.authority_id) if item else None
            if item and authority.status in ['APPROVED','REVERSAL_PENDING']:
                n=remaining(db,item,at.isoformat());row.update({'outstanding_id':item.id,'remaining':amount_text(n,item.currency),'clearance_status':'CLEARED' if n==0 else 'PARTIAL' if n<item.amount else 'OPEN'})
            else:row['remaining']=None
        return {'as_of':at.isoformat(),'items':sorted(items,key=lambda x:-x['age_days']),'limitation':'未登记权威期间的历史事项仍属清算待核验；已登记项目按独立批准的逐项分配计算余额。期初桥接与清算请到对应任务报告页办理，不能仅按金额抵销差异。'}
