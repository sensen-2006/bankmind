import os
import platform
import ssl  # preload OpenSSL DLLs before openpyxl
if os.name == 'nt' and hasattr(platform, '_wmi_query'):
    platform._wmi_query = lambda *a, **k: (_ for _ in ()).throw(OSError('WMI disabled'))
import io
import csv
import json
import hashlib
import zipfile
import html
from pathlib import Path
from urllib.parse import quote
from contextlib import asynccontextmanager
from collections import Counter, defaultdict, deque
from time import monotonic
from datetime import date,datetime
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import openpyxl
from zoneinfo import ZoneInfo
from .db import *
from .security import current_user, password_ok, password_hash, issue_token, digest, role, scoped, audit, mask
from .schemas import Login, WechatCode, TaskInput, Mapping, Rules, Decision, Proposal, CaseUpdate, QualityAction, QualityBulk, Balances, PeriodRequest, UserInput, UserUpdate, AccountInput, RunInput
from .schemas import ImportConfirm
from . import document_ai
from .parsing import read_table, MAX_BYTES, money, amount_text, PRECISION
from .services import *
from .ai import investigate, MappingAdvice, safe_evidence
from .seed import seed
from .periods import period_json,period_record,require_open_period,seal_period
from .work_api import router as work_router
from .operations import pilot_enabled,queue_metrics,readiness,validate_deployment,work_mode

def audit_denial(value,status,route):
    with SessionLocal() as db:
        lock(db)
        token=db.scalar(select(Token).where(Token.token_hash==digest(value)))
        user=db.get(User,token.user_id) if token else None
        if user:
            audit(db,user,'ACCESS_OR_STATE_DENIED',user.id,{}, {'status_code':status,'route':route},'服务端权限或版本拒绝；未记录请求正文/令牌')
            db.commit()

@asynccontextmanager
async def lifespan(app):
    validate_deployment()
    init_db()
    with SessionLocal() as db:
        lock(db)
        if pilot_enabled() and db.scalar(select(Tenant.id).where(Tenant.name.in_(['澄明制造 · 合成演示企业','隔离验证 · 合成企业']))):
            raise RuntimeError('Pilot requires a separate database without seeded demo tenants')
        seed(db)
    if work_mode()!='embedded':
        yield
        return
    from threading import Event, Thread
    from .work import consume
    stop=Event()
    worker=Thread(target=consume,args=(stop,),name='bankmind-worker',daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        await run_in_threadpool(worker.join,35)

app=FastAPI(title='银账智核 BankMind API',version='1.0.0',lifespan=lifespan,
            description='企业银行流水与银行存款日记账逐笔对账。本地原型，AI默认Mock，模拟数据明确标识。')
app.add_middleware(CORSMiddleware,allow_origins=os.getenv('CORS_ORIGINS','http://localhost:5173,http://127.0.0.1:5173').split(','),
                   allow_credentials=False,allow_methods=['GET','POST','PATCH'],allow_headers=['Authorization','Content-Type'])
DB=Depends(get_db);USER=Depends(current_user)
app.include_router(work_router)

def related_task(db,model,oid,user):
    obj=scoped(db,model,oid,user)
    return obj,scoped(db,Task,obj.task_id,user)

def version_guard(obj,version):
    if obj.version!=version: raise HTTPException(409,'对象已被更新，请刷新后重新提交')

def model_dict(obj,keys):
    return {k:getattr(obj,k) for k in keys}

@app.middleware('http')
async def headers_and_denials(request,call_next):
    if request.method in ['POST','PATCH']:
        length=request.headers.get('content-length','0')
        if length.isdigit() and int(length)>12*1024*1024:
            return JSONResponse(status_code=413,content={'detail':'请求体超过12 MB，单文件限制10 MB'})
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='same-origin'
    if request.url.path.startswith('/api'): response.headers['Cache-Control']='no-store'
    if response.status_code in [401,403,409] and request.url.path.startswith('/api/'):
        value=request.headers.get('authorization','')
        if value.startswith('Bearer '):
            await run_in_threadpool(audit_denial,value[7:],response.status_code,request.method+' '+request.url.path[:200])
    return response

@app.exception_handler(IntegrityError)
async def conflict(request,exc):
    return JSONResponse(status_code=409,content={'detail':'并发写入或唯一约束冲突，请刷新；事务未部分提交'})

@app.get('/api/health')
def health(): return {'status':'ok','product':'BankMind','ai_mode':os.getenv('LLM_MODE','mock'),'demo':os.getenv('BANKMIND_DEMO','true').lower()=='true'}

@app.get('/api/ready')
def ready(db:Session=DB):
    try:
        result=readiness(db)
    except Exception:
        result={'ready':False}
    return JSONResponse(result,status_code=200 if result['ready'] else 503)

@app.get('/api/operations/queue')
def operational_queue(user:User=USER,db:Session=DB):
    role(user,'Admin')
    return queue_metrics(db,user.tenant_id)

login_attempts=defaultdict(deque)
@app.post('/api/auth/login')
def login(body:Login,request:Request,db:Session=DB):
    key=digest((request.client.host if request.client else '')+body.username)
    queue=login_attempts[key];now_m=monotonic()
    while queue and queue[0]<now_m-60: queue.popleft()
    if len(queue)>=10: raise HTTPException(429,'登录尝试过于频繁，请稍后再试')
    queue.append(now_m)
    lock(db)
    u=db.scalar(select(User).where(User.username==body.username))
    if not u or not u.enabled or not password_ok(body.password,u.password_hash): raise HTTPException(401,'用户名或密码不正确')
    token=issue_token(db,u);audit(db,u,'LOGIN',u.id,{}, {'client':'shared-web-mini'},'企业身份认证')
    db.commit();return {'token':token,'expires_in':28800}

@app.post('/api/auth/logout')
def logout(request:Request,user:User=USER,db:Session=DB):
    lock(db)
    token=db.scalar(select(Token).where(Token.token_hash==digest(request.headers['authorization'][7:])))
    if token: db.delete(token)
    audit(db,user,'LOGOUT',user.id,reason='撤销当前会话');db.commit();return {'ok':True}

@app.get('/api/me')
def me(user:User=USER,db:Session=DB):
    return {**model_dict(user,['id','name','username','role','tenant_id','account_ids']), 'tenant_name':db.get(Tenant,user.tenant_id).name,
            'demo':os.getenv('BANKMIND_DEMO','true').lower()=='true'}

@app.post('/api/auth/wechat')
def wechat_login(body:WechatCode,db:Session=DB):
    from .wechat import exchange_code
    app_id,identity_hash=exchange_code(body.code)
    identity=db.scalar(select(WechatIdentity).where(WechatIdentity.app_id==app_id,WechatIdentity.identity_hash==identity_hash))
    u=db.get(User,identity.user_id) if identity else None
    if not u or not u.enabled:raise HTTPException(403,'微信身份尚未绑定已授权企业成员，请先使用企业账号登录并绑定')
    token=issue_token(db,u);audit(db,u,'WECHAT_LOGIN',u.id,reason='微信code由服务端验证，映射至已授权企业成员');db.commit();return {'token':token,'expires_in':28800}

@app.post('/api/auth/wechat/bind')
def wechat_bind(body:WechatCode,user:User=USER,db:Session=DB):
    from .wechat import exchange_code
    app_id,identity_hash=exchange_code(body.code)
    lock(db)
    identity=db.scalar(select(WechatIdentity).where(WechatIdentity.identity_hash==identity_hash))
    if identity and identity.user_id!=user.id:raise HTTPException(409,'该微信身份已绑定其他企业成员')
    if not identity:db.add(WechatIdentity(tenant_id=user.tenant_id,user_id=user.id,app_id=app_id,identity_hash=identity_hash))
    audit(db,user,'WECHAT_BIND',user.id,reason='企业身份验证后绑定当前微信；不根据客户端openid授权');db.commit();return {'ok':True}

@app.get('/api/accounts')
def accounts(user:User=USER,db:Session=DB):
    return [{**model_dict(a,['id','name','bank','currency']), 'number_masked':mask(a.number)}
            for a in db.scalars(select(Account).where(Account.tenant_id==user.tenant_id,Account.id.in_(user.account_ids)))]

@app.post('/api/accounts')
def add_account(body:AccountInput,user:User=USER,db:Session=DB):
    role(user,'Admin');lock(db)
    if db.scalar(select(Account).where(Account.tenant_id==user.tenant_id,Account.number==body.number)): raise HTTPException(409,'账户已存在')
    a=Account(id=uid(),tenant_id=user.tenant_id,**body.model_dump());db.add(a)
    user.account_ids=[*user.account_ids,a.id]
    audit(db,user,'ACCOUNT_CREATE',a.id,{}, {'currency':a.currency,'last4':a.number[-4:]},'创建账户主数据');db.commit();return {'id':a.id}

@app.get('/api/users')
def users(user:User=USER,db:Session=DB):
    return [model_dict(u,['id','name','username','role','enabled','account_ids']) for u in db.scalars(select(User).where(User.tenant_id==user.tenant_id))]

@app.post('/api/users')
def add_user(body:UserInput,user:User=USER,db:Session=DB):
    role(user,'Admin');lock(db)
    for aid in body.account_ids: scoped(db,Account,aid,user)
    if db.scalar(select(User).where(User.username==body.username)): raise HTTPException(409,'用户名已存在')
    u=User(id=uid(),tenant_id=user.tenant_id,username=body.username,name=body.name,role=body.role,password_hash=password_hash(body.password),account_ids=body.account_ids)
    db.add(u);audit(db,user,'USER_CREATE',u.id,{}, {'role':u.role,'accounts':u.account_ids},'新增企业成员');db.commit();return {'id':u.id}

@app.patch('/api/users/{oid}')
def edit_user(oid:str,body:UserUpdate,user:User=USER,db:Session=DB):
    role(user,'Admin');lock(db);u=scoped(db,User,oid,user)
    if u.id==user.id: raise HTTPException(422,'不能在当前会话撤销自己的权限')
    for aid in body.account_ids: scoped(db,Account,aid,user)
    before={'enabled':u.enabled,'accounts':u.account_ids};u.enabled=body.enabled;u.account_ids=body.account_ids
    audit(db,user,'USER_ACCESS_UPDATE',u.id,before,{'enabled':u.enabled,'accounts':u.account_ids},body.reason);db.commit();return {'ok':True}

def allowed_tasks(db,user):
    return list(db.scalars(select(Task).where(Task.tenant_id==user.tenant_id,Task.account_id.in_(user.account_ids)).order_by(Task.created_at.desc())))

def task_json(db,t,rows=None,summary=None):
    a=db.get(Account,t.account_id)
    return {**model_dict(t,['id','name','account_id','currency','start_date','end_date','status','current_run','version','rule_config','balances','created_at','creator_id']),
            'account_name':a.bank+' · '+mask(a.number),'summary':summary if summary is not None else task_summary(db,t,rows)}

@app.get('/api/tasks')
def tasks(user:User=USER,db:Session=DB): return [task_json(db,t) for t in allowed_tasks(db,user)]

@app.post('/api/tasks')
def create_task(body:TaskInput,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);a=scoped(db,Account,body.account_id,user)
    if a.currency!=body.currency: raise HTTPException(422,'任务币种必须等于银行账户币种')
    t=Task(id=uid(),tenant_id=user.tenant_id,creator_id=user.id,**{**body.model_dump(),'start_date':body.start_date.isoformat(),'end_date':body.end_date.isoformat()})
    db.add(t);audit(db,user,'TASK_CREATE',t.id,{}, {'account_id':t.account_id,'period':[t.start_date,t.end_date]},'新建对账任务');db.commit();return task_json(db,t)

@app.get('/api/tasks/{oid}')
def get_task(oid:str,user:User=USER,db:Session=DB): return task_json(db,scoped(db,Task,oid,user))

@app.get('/api/dashboard')
def dashboard(user:User=USER,db:Session=DB):
    ts=allowed_tasks(db,user);summaries=[task_summary(db,t) for t in ts]
    # Only the explicitly selected task month is shown; future/current months never use invented counts.
    month=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()[:7]
    total_valid=sum(s['sides']['BANK']['valid'] for s in summaries)
    auto=sum(s['sides']['BANK']['automatic'] for s in summaries)
    trend=Counter()
    task_ids=[t.id for t in ts]
    for created_at in db.scalars(select(Case.created_at).where(Case.task_id.in_(task_ids),Case.active==True)):
        trend[datetime.fromisoformat(created_at).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()]+=1
    return {'task_count':len(ts),'month':month,'month_tasks':sum(datetime.fromisoformat(t.created_at).astimezone(ZoneInfo('Asia/Shanghai')).isoformat().startswith(month) for t in ts),
            'completed':sum(t.status=='COMPLETED' for t in ts),'review_tasks':sum(t.status=='REVIEW' for t in ts),
            'unmatched':sum(s['sides']['BANK']['unmatched']+s['sides']['BOOK']['unmatched'] for s in summaries),
            'open_cases':sum(s['open_cases'] for s in summaries),'pending_matches':sum(s['pending_matches'] for s in summaries),
            'auto_coverage':round(100*auto/total_valid,1) if total_valid else 0,
            'quality_issues':sum(s['sides'][side]['quarantined'] for s in summaries for side in ['BANK','BOOK']),
            'trend':[{'date':k,'count':v} for k,v in sorted(trend.items())],
            'case_types':dict(sum((Counter(s['case_types']) for s in summaries),Counter())),
            'recent_tasks':[task_json(db,t,summary=summaries[i]) for i,t in enumerate(ts[:6])],'data_as_of':now()}

@app.post('/api/tasks/{task_id}/imports')
def upload(task_id:str,side:str=Form(...),file:UploadFile=File(...),sheet:str=Form(''),header_row:int=Form(1),external_consent:bool=Form(False),user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant')
    content=file.file.read(MAX_BYTES+1)
    task=scoped(db,Task,task_id,user)
    require_open_period(db,task)
    if task.current_run: raise HTTPException(409,'匹配后输入已冻结，请创建新的任务版本导入，原运行保留')
    if side not in ['BANK','BOOK']: raise HTTPException(422,'side必须为BANK或BOOK')
    filename=Path(file.filename or '').name
    ext=Path(filename).suffix.lower()
    if len(filename)>200: raise HTTPException(422,'文件名过长')
    if ext not in ('.xls','.xlsx','.html','.htm','.md','.markdown') and sheet:raise HTTPException(422,'此文件类型不接受工作表参数')
    if ext in ('.pdf','.sta','.mt940','.940','.xml','.ofx','.qfx') and header_row!=1:raise HTTPException(422,'此报文格式自动识别结构，不接受自定义表头行')
    sha=hashlib.sha256(content).hexdigest()
    old=find_import(db,task,side,sha,sheet,header_row,external_consent)
    if old: return {**import_json(old),'idempotent':True}
    db.rollback()
    try:
        if external_consent:
            settings=document_ai.settings()
            if not settings:raise ValueError('外部文档识别未启用；未发送文件')
            if not document_ai.allow_document(task.tenant_id,sha,ext):
                raise HTTPException(403,'此文件未获批准发送给当前供应商；未发送文件')
            if sheet or header_row!=1:raise ValueError('多模态不接受工作表或表头参数')
            total_pages=document_ai.page_count(content,ext)
            lock(db);task=scoped(db,Task,task_id,user);require_open_period(db,task)
            if task.current_run:raise HTTPException(409,'输入已冻结，未发送文件')
            old=find_import(db,task,side,sha,sheet,header_row,True)
            if old:return {**import_json(old),'idempotent':True}
            local_demo=document_ai.provider()=='codex-demo'
            reason='读取事先由本机 Codex 生成的合成样本回执；本次未调用供应商' if local_demo else '操作人授权发送原件各页进行视觉提取；本记录不表示实际调用次数或提取成功'
            audit(db,user,'DOCUMENT_AI_DEMO_REPLAY' if local_demo else 'DOCUMENT_AI_REQUEST',task.id,after={'sha256':sha,'size':len(content),'side':side,'provider':document_ai.provider_name(),
                  'model':settings[0],'page_ranges':[list(span) for span in document_ai.request_ranges(total_pages)]},
                  reason=reason)
            db.commit()
            table=document_ai.extract(content,ext,*settings,total_pages)
        else:
            table=read_table(content,ext,sheet,header_row)
    except HTTPException:raise
    except document_ai.DocumentAIError as error:
        lock(db)
        audit(db,user,'DOCUMENT_AI_REJECTED',task_id,after={'sha256':sha,'side':side,'error_code':error.code,
             'provider_status':error.provider_status},reason='提取未生成可发布预览；未记录供应商原始响应')
        db.commit()
        raise HTTPException(422,error.detail()) from None
    except Exception as e: raise HTTPException(422,str(e) if isinstance(e,ValueError) else '文件损坏或无法解析')
    lock(db);task=scoped(db,Task,task_id,user)
    require_open_period(db,task)
    if task.current_run:raise HTTPException(409,'并发更新：输入已冻结，本次预览未入库')
    old=find_import(db,task,side,sha,sheet,header_row,external_consent)
    if old:return {**import_json(old),'idempotent':True}
    b=save_import(db,task,user,filename,side,sha,sheet,header_row,content,table)
    db.commit();return import_json(b)

def import_json(b):
    from .parsing import suggest
    return {**model_dict(b,['id','task_id','side','filename','sha256','size','sheet','header_row','columns','preview','mapping','status','row_count','valid_count','quality','created_at']),
            'suggested_mapping':suggest(b.columns)}

@app.get('/api/tasks/{task_id}/imports')
def imports(task_id:str,user:User=USER,db:Session=DB):
    task=scoped(db,Task,task_id,user)
    return [import_json(b) for b in db.scalars(select(ImportBatch).where(ImportBatch.task_id==task.id).order_by(ImportBatch.created_at.desc(),ImportBatch.id.desc()))]

@app.get('/api/tasks/{task_id}/workbench')
def workbench(task_id:str,user:User=USER,db:Session=DB):
    task=scoped(db,Task,task_id,user)
    rows=rows_for_task(db,task)
    reasons=Counter(issue['code'] for row in rows if row.quality=='QUARANTINED' for issue in row.issues)
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.task_id==task.id).order_by(ImportBatch.created_at.desc(),ImportBatch.id.desc()))) if user.role in ('Admin','Accountant') else []
    templates={}
    pending=[b for b in batches if b.status=='PREVIEWED' and b.quality.get('encoding')!='document-ai']
    if pending:
        history=db.execute(select(ImportBatch,Task).join(Task,Task.id==ImportBatch.task_id).where(
            Task.tenant_id==task.tenant_id,Task.account_id==task.account_id,Task.currency==task.currency,
            ImportBatch.status.in_(['READY','PARTIAL_REJECTED']),ImportBatch.valid_count>0
        ).order_by(ImportBatch.created_at.desc()).limit(100)).all()
        for batch in pending:
            for previous,source in history:
                if (previous.side,previous.columns,import_sheet(previous),previous.header_row,previous.quality.get('encoding'))!=(batch.side,batch.columns,import_sheet(batch),batch.header_row,batch.quality.get('encoding')):continue
                mapping=previous.mapping
                templates[batch.id]={'fields':mapping['fields'],'direction':mapping['direction'],'date_format':mapping['date_format'],'from':source.name}
                break
    signed=db.scalar(select(Report).where(Report.task_id==task.id,Report.run_id==task.current_run,Report.status=='SIGNED').order_by(Report.created_at.desc()).limit(1)) if task.current_run else None
    return {'task':task_json(db,task,rows),'adjustment':balance_adjustment(db,task,rows),'quality_reasons':reasons.most_common(5),
            'batches':[import_json(b) for b in batches],'templates':templates,'document_ai':bool(document_ai.settings()),
            'document_ai_provider':document_ai.provider_name(),'background_jobs':work_mode()!='off',
            'signed':report_json(signed) if signed else None}

@app.get('/api/imports/{oid}/extraction')
def extraction(oid:str,page:int=Query(1,ge=1,le=20),user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');batch,task=related_task(db,ImportBatch,oid,user)
    if batch.quality.get('encoding')!='document-ai':raise HTTPException(404,'此文件没有多模态提取预览')
    try:
        content=(STORAGE/(batch.object_key+'.parsed.json')).read_bytes()
        if hashlib.sha256(content).hexdigest()!=batch.quality.get('extraction_sha256'):raise ValueError('hash mismatch')
        rows=json.loads(content)['rows']
    except (OSError,ValueError):raise HTTPException(409,'提取凭证缺失或已变更') from None
    return {'total':len(rows),'rows':rows[(page-1)*50:page*50]}

@app.get('/api/imports/{oid}/original')
def original(oid:str,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant','Auditor');lock(db);b,task=related_task(db,ImportBatch,oid,user)
    content=verified_original(b);filename=quote(b.filename,safe='')
    audit(db,user,'SOURCE_DOWNLOAD',b.id,after={'sha256':b.sha256},reason='下载授权原文件');db.commit()
    return StreamingResponse(io.BytesIO(content),media_type='application/octet-stream',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{filename}"})

@app.post('/api/imports/{oid}/mapping')
def publish(oid:str,body:Mapping,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);b,task=related_task(db,ImportBatch,oid,user)
    require_open_period(db,task)
    if task.current_run: raise HTTPException(409,'输入已冻结，请创建新任务版本')
    # Defaults must be explicitly confirmed but full account is supplied from trusted master data.
    config=body.model_dump()
    if config['default_account']=='__TASK_ACCOUNT_CONFIRMED__': config['default_account']=db.get(Account,task.account_id).number
    validate_import(db,b,config,task,user);db.commit();return import_json(b)

@app.post('/api/tasks/{task_id}/imports/confirm')
def confirm_imports(task_id:str,body:ImportConfirm,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);task=scoped(db,Task,task_id,user)
    batches=confirm_batches(db,task,body,user)
    db.commit();return [import_json(batch) for batch in batches]

@app.post('/api/imports/{oid}/mapping-advice')
def mapping_advice(oid:str,user:User=USER,db:Session=DB):
    b,task=related_task(db,ImportBatch,oid,user)
    from .parsing import suggest
    result=MappingAdvice(fields=suggest(b.columns),notes=['Mock 数据识别Agent：根据列名别名生成建议，未向外部发送文件。','必须人工确认日期格式、账户、币种与借贷含义。']).model_dump()
    db.add(AIInvocation(tenant_id=user.tenant_id,actor_id=user.id,object_id=b.id,provider='mock',model='mapping-alias-v1',status='SUCCEEDED',elapsed_ms=0,evidence_ids=[b.id]))
    audit(db,user,'MAPPING_ADVICE',b.id,after={'provider':'mock'},reason='字段识别建议，不能直接发布');db.commit();return result

@app.get('/api/tasks/{task_id}/transactions')
def transactions(task_id:str,side:str='',quality:str='',search:str='',issue_code:str='',sort_by:str='',sort_dir:str='asc',page:int=Query(1,ge=1),page_size:int=Query(30,ge=1,le=100),user:User=USER,db:Session=DB):
    task=scoped(db,Task,task_id,user);rows=rows_for_task(db,task)
    terms=[tok.strip().lower() for tok in search.split() if tok.strip()]
    def _matches_tx(t):
        if side and t.side!=side: return False
        if quality and t.quality!=quality: return False
        if issue_code:
            codes={str(i.get('code','')) for i in (t.issues or [])}
            if issue_code not in codes: return False
        if terms:
            issue_text=' '.join(f"{i.get('code','')} {i.get('message','')}" for i in (t.issues or []))
            hay=f"{t.counterparty} {t.description} {t.reference} {t.date} {t.row_no} {t.side} {t.quality} {issue_text} {amount_text(t.amount,t.currency)}".lower()
            if not all(tok in hay for tok in terms): return False
        return True
    rows=[t for t in rows if _matches_tx(t)]
    rev=(sort_dir.lower()=='desc')
    if sort_by=='issue':
        rows.sort(key=lambda t:(' / '.join(sorted({str(i.get('code','')) for i in (t.issues or [])})) or 'ZZZZ',t.side,t.row_no),reverse=rev)
    elif sort_by=='date':
        rows.sort(key=lambda t:(t.date or '',t.side,t.row_no),reverse=rev)
    elif sort_by=='amount':
        rows.sort(key=lambda t:(abs(t.amount),t.side,t.row_no),reverse=rev)
    else:
        rows.sort(key=lambda t:(t.side,t.import_id,t.row_no),reverse=rev)
    return {'total':len(rows),'items':[serialize_tx(t) for t in rows[(page-1)*page_size:page*page_size]]}

@app.get('/api/transactions/{oid}')
def transaction_detail(oid:str,user:User=USER,db:Session=DB):
    t,task=related_task(db,Transaction,oid,user);b=db.get(ImportBatch,t.import_id)
    raw=dict(t.raw)
    if user.role=='Reviewer':
        for k,v in raw.items():
            if '账号' in k or 'account' in k.lower(): raw[k]=mask(v)
    return {**serialize_tx(t,False),'raw':raw,'source':{'filename':b.filename,'sha256':b.sha256,'sheet':b.sheet or b.quality.get('selected_sheet',''),'row_no':t.row_no,'mapping':b.mapping}}

@app.post('/api/transactions/{oid}/quality')
def quality_action(oid:str,body:QualityAction,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);t,task=related_task(db,Transaction,oid,user)
    if task.current_run: raise HTTPException(409,'运行输入冻结，请在匹配前处理质量问题或新建任务')
    if t.quality!='QUARANTINED': raise HTTPException(409,'仅处理当前隔离行')
    codes={i['code'] for i in t.issues}
    if body.action=='VALID' and not codes <= {'DUPLICATE_SUSPECTED','REVERSAL_REVIEW'}:
        raise HTTPException(422,'结构性错误不能直接放行；请修正源文件并建立新任务')
    before=t.quality;t.quality=body.action
    audit(db,user,'QUALITY_RESOLVE',t.id,{'quality':before},{'quality':t.quality,'original_issues':t.issues},body.reason)
    task.version+=1;db.commit();return serialize_tx(t)

@app.post('/api/tasks/{task_id}/quality/bulk')
def quality_bulk(task_id:str,body:QualityBulk,user:User=USER,db:Session=DB):
    """Group-level release/exclude for one reference and one side.

    Keeps the original rows and issues; every row receives its own audit event.
    Structural errors still cannot be released. All rows are validated before
    any mutation, so a batch never partially applies.
    """
    role(user,'Admin','Accountant');lock(db);task=scoped(db,Task,task_id,user)
    if task.current_run:raise HTTPException(409,'运行输入冻结，请在匹配前处理质量问题或新建任务')
    normalized={**body.model_dump(),'ids':sorted(body.ids)}
    old,key=receipt_get(db,user,body.idempotency_key,['quality-bulk',task_id,normalized])
    if old:return old
    if len(set(body.ids))!=len(body.ids):raise HTTPException(422,'同一批次不能重复包含同一行')
    rows=[db.get(Transaction,i) for i in body.ids]
    if any(not x or x.task_id!=task.id or x.tenant_id!=task.tenant_id for x in rows):raise HTTPException(404,'质量批次包含不属于本任务的行')
    if any(x.side!=body.side or x.reference!=body.reference for x in rows):raise HTTPException(422,'批量处理仅限同一侧、同一参考号的隔离组')
    if any(x.quality!='QUARANTINED' for x in rows):raise HTTPException(409,'批次中存在非隔离行，请刷新后重试')
    if body.action=='VALID':
        allowed={'DUPLICATE_SUSPECTED','REVERSAL_REVIEW'}
        if any({i['code'] for i in x.issues} - allowed for x in rows):raise HTTPException(422,'结构性错误不能批量放行；请修正源文件并建立修订')
    for x in rows:
        before=x.quality;x.quality=body.action
        audit(db,user,'QUALITY_BULK_RESOLVE',x.id,{'quality':before},{'quality':x.quality,'original_issues':x.issues},body.reason)
    task.version+=1;invalidate_reports(db,task.id)
    result={'count':len(rows),'ids':[x.id for x in rows],'action':body.action}
    db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(['quality-bulk',task_id,normalized]),result=result));db.commit();return result

@app.post('/api/tasks/{task_id}/run')
def run(task_id:str,body:RunInput|None=None,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);task=scoped(db,Task,task_id,user)
    if body and body.quick_version is not None:
        version_guard(task,body.quick_version)
        if task.current_run:raise HTTPException(409,'已经运行；重跑请使用专业工作区')
        if {row.side for row in rows_for_task(db,task) if row.quality=='VALID'}!={'BANK','BOOK'}:
            raise HTTPException(422,'快捷匹配需要两侧都有有效数据；单侧调查请使用专业工作区')
    declaration=body.single_side_declaration if body else 'UNSUBMITTED'
    reason=body.reason if body else ''
    result=execute_run(db,task,user,declaration,reason);db.commit();return model_dict(result,['id','status','elapsed_ms','input_hash','rule_version','diagnostics'])

@app.get('/api/tasks/{task_id}/runs')
def runs(task_id:str,user:User=USER,db:Session=DB):
    task=scoped(db,Task,task_id,user)
    return [model_dict(r,['id','created_at','status','elapsed_ms','input_hash','rule_version','diagnostics']) for r in db.scalars(select(Run).where(Run.task_id==task.id).order_by(Run.created_at.desc()))]

@app.patch('/api/tasks/{task_id}/rules')
def rules(task_id:str,body:Rules,user:User=USER,db:Session=DB):
    role(user,'Admin');lock(db);t=scoped(db,Task,task_id,user);version_guard(t,body.version)
    if t.current_run: raise HTTPException(409,'运行规则已冻结，请在新任务配置新规则版本')
    before=t.rule_config;t.rule_config=body.model_dump(exclude={'version','reason'});t.version+=1
    audit(db,user,'RULE_VERSION',t.id,before,t.rule_config,body.reason);db.commit();return task_json(db,t)

@app.patch('/api/tasks/{task_id}/balances')
def balances(task_id:str,body:Balances,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);t=scoped(db,Task,task_id,user);version_guard(t,body.version)
    values={}
    for s in ['BANK','BOOK']:
        values[s]={}
        for k,v in getattr(body,s).items():
            if k not in ['opening','closing']: raise HTTPException(422,'只接受期初opening与期末closing')
            if v is not None:
                try: v=amount_text(money(v,t.currency),t.currency)
                except ValueError as exc: raise HTTPException(422,str(exc))
            values[s][k]=v
    before=t.balances;t.balances=values;t.version+=1
    invalidate_reports(db,t.id);audit(db,user,'BALANCE_INPUT',t.id,before,values,body.reason);db.commit();return task_json(db,t)

@app.post('/api/tasks/{task_id}/xbrl-check')
def xbrl_balance_check(task_id:str,file:UploadFile=File(...),scope_confirmed:bool=Form(False),user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant','Reviewer','Auditor')
    task=scoped(db,Task,task_id,user)
    content=file.file.read(MAX_BYTES+1)
    if not content or len(content)>MAX_BYTES:
        raise HTTPException(422,'XBRL 文件为空或超过 10 MB')
    from .xbrl_reconcile import extract_xbrl_cash_facts, reconcile_xbrl_balance
    try:
        extracted=extract_xbrl_cash_facts(content)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    book_closing=(task.balances or {}).get('BOOK',{}).get('closing')
    result=reconcile_xbrl_balance(extracted['facts'],task.currency,task.end_date,book_closing,scope_confirmed=scope_confirmed)
    audit(db,user,'XBRL_BALANCE_CHECK',task.id,{},{'status':result['status'],'fact':result['fact'],'scope_confirmed':scope_confirmed},'XBRL/iXBRL 财报货币资金期末余额勾稽检查')
    db.commit()
    return {**extracted,'reconciliation':result}

@app.get('/api/tasks/{task_id}/matches')
def matches(task_id:str,status:str='',method:str='',search:str='',page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),history:bool=False,user:User=USER,db:Session=DB):
    t=scoped(db,Task,task_id,user)
    query=select(Match).where(Match.task_id==t.id)
    if not history: query=query.where(Match.active==True)
    base_rows=list(db.scalars(query.order_by(Match.created_at.desc(),Match.id)))
    counts={'all':len(base_rows),'deterministic':sum(m.status=='DETERMINISTIC' for m in base_rows),
            'pending':sum(m.status=='PENDING' for m in base_rows),'approved':sum(m.status=='APPROVED' for m in base_rows),
            'reversal_pending':sum(m.status=='REVERSAL_PENDING' for m in base_rows)}
    rows=[m for m in base_rows if (not status or m.status==status) and (not method or m.method==method)]
    if search:
        terms=[tok for tok in search.strip().lower().split() if tok]
        if terms:
            ids=set()
            for x in rows_for_task(db,t):
                hay=' '.join([
                    str(x.counterparty or ''),
                    str(x.description or ''),
                    str(x.reference or ''),
                    str(x.date or ''),
                    amount_text(x.amount_minor,x.currency) if hasattr(x,'amount_minor') else str(getattr(x,'amount','')),
                ]).lower()
                if all(tok in hay for tok in terms):
                    ids.add(x.id)
            rows=[m for m in rows if ids.intersection(m.bank_ids+m.book_ids)]
    return {'total':len(rows),'counts':counts,'items':[serialize_match(db,m) for m in rows[(page-1)*page_size:page*page_size]]}

@app.get('/api/tasks/{task_id}/matching-guidance')
def matching_guidance(task_id:str,user:User=USER,db:Session=DB):
    t=scoped(db,Task,task_id,user)
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.task_id==t.id)))
    group_mapped=any((b.mapping or {}).get('fields',{}).get('group_key') for b in batches)
    rows=[x for x in rows_for_task(db,t) if x.quality!='EXCLUDED']
    group_values=sum(bool(x.group_key) for x in rows if x.quality=='VALID')
    bank_refs=Counter(x.reference for x in rows if x.reference and x.side=='BANK')
    book_refs=Counter(x.reference for x in rows if x.reference and x.side=='BOOK')
    # A reference shared by one bank row and one book row is normal. Ambiguity
    # starts when the same reference repeats within one side.
    duplicates={r:bank_refs[r]+book_refs[r] for r in set(bank_refs)|set(book_refs) if bank_refs[r]>1 or book_refs[r]>1}
    examples=[]
    for ref,n in sorted(duplicates.items(),key=lambda kv:(-kv[1],kv[0]))[:5]:
        matching=[x for x in rows if x.reference==ref]
        examples.append({'reference':ref,'rows':n,'bank':sum(x.side=='BANK' for x in matching),'book':sum(x.side=='BOOK' for x in matching)})
    valid_rows=[x for x in rows if x.quality=='VALID']
    return {'group_key_mapped':group_mapped,'group_key_rows':group_values,'duplicate_references':len(duplicates),
            'duplicate_examples':examples,
            'single_side_task':not all(any(x.side==side for x in valid_rows) for side in ['BANK','BOOK']),
            'suggestion':'一对多/多对一必须显式映射“结算批次/业务组键”，组合合计需精确相等并仍需独立复核；仅重复编号不会自动组合，也不会为凑覆盖率放宽歧义保护。'}

@app.get('/api/tasks/{task_id}/quality-groups')
def quality_groups(task_id:str,user:User=USER,db:Session=DB):
    t=scoped(db,Task,task_id,user)
    rows=[x for x in rows_for_task(db,t) if x.quality=='QUARANTINED' and x.reference]
    grouped=defaultdict(list)
    for x in rows:grouped[x.reference].append(x)
    groups=[]
    for ref,items in grouped.items():
        if len(items)<2:continue
        currencies={x.currency for x in items}
        amounts={c:amount_text(sum(abs(x.amount) for x in items if x.currency==c),c) for c in currencies}
        def source_row(x):
            batch=db.get(ImportBatch,x.import_id)
            return {'side':x.side,'row_no':x.row_no,'id':x.id,'amount':amount_text(x.amount,x.currency),'counterparty':x.counterparty,
                    'issues':[i['code'] for i in x.issues],'filename':batch.filename if batch else '',
                    'sheet':batch.sheet if batch else '','page':(x.raw or {}).get('PDF页码','')}
        groups.append({'reference':ref,'rows':len(items),'bank':sum(x.side=='BANK' for x in items),'book':sum(x.side=='BOOK' for x in items),
            'amounts':amounts,'row_numbers':[source_row(x) for x in items[:50]]})
    groups.sort(key=lambda g:(-g['rows'],g['reference']))
    return {'total':len(groups),'groups':groups[:100],'note':'重复编号可代表真实业务复用，不能仅凭同号删除；此处仅提供同组对照。'}

@app.post('/api/tasks/{task_id}/proposals')
def propose(task_id:str,body:Proposal,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);t=scoped(db,Task,task_id,user)
    if not t.current_run: raise HTTPException(422,'请先执行匹配')
    bs,ls=match_guard(db,t,body.bank_ids,body.book_ids)
    if db.scalar(select(MatchMember).where(MatchMember.transaction_id.in_(body.bank_ids+body.book_ids),MatchMember.active==True)): raise HTTPException(409,'交易已占用')
    m=Match(id=uid(),tenant_id=user.tenant_id,task_id=t.id,run_id=t.current_run,bank_ids=body.bank_ids,book_ids=body.book_ids,
            method='MANUAL',score=0,evidence={'reason':body.reason,'source_ids':body.bank_ids+body.book_ids,'amount_delta':'0','predicates':['人工建议','硬约束通过']},status='PENDING',creator_id=user.id)
    db.add(m);audit(db,user,'MANUAL_PROPOSAL',m.id,{}, {'bank_ids':m.bank_ids,'book_ids':m.book_ids},body.reason)
    invalidate_reports(db,t.id);db.commit();return serialize_match(db,m)

@app.get('/api/matches/{oid}')
def match_detail(oid:str,user:User=USER,db:Session=DB):
    m,t=related_task(db,Match,oid,user)
    return serialize_match(db,m)

def receipt_get(db,user,key,payload):
    scoped_key=digest(user.tenant_id+user.id+key)
    old=db.scalar(select(Receipt).where(Receipt.key==scoped_key))
    if old:
        if old.request_hash!=digest(payload): raise HTTPException(409,'幂等键已用于不同操作')
        return old.result,scoped_key
    return None,scoped_key

@app.post('/api/matches/{oid}/decision')
def match_decision(oid:str,body:Decision,user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer');lock(db);m,t=related_task(db,Match,oid,user)
    old,key=receipt_get(db,user,body.idempotency_key,[oid,body.model_dump()])
    if old:return old
    version_guard(m,body.version)
    if m.creator_id==user.id: raise HTTPException(403,'禁止审核本人提交或执行生成的匹配')
    if not m.active or m.status not in ['PENDING','DETERMINISTIC','REVERSAL_PENDING']: raise HTTPException(409,'当前状态不能审批')
    before=m.status
    if m.status=='REVERSAL_PENDING':
        if body.action=='APPROVE':
            if m.evidence.get('reversal_requester')==user.id: raise HTTPException(403,'禁止自审撤销申请')
            m.status='REVERSED';m.active=False
            db.execute(update(MatchMember).where(MatchMember.match_id==m.id).values(active=False))
            for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True,Case.resolution=='MATCH_APPROVED',Case.status=='CLOSED')):
                if set(c.transaction_ids).intersection(m.bank_ids+m.book_ids):
                    cb=c.status;c.status='INVESTIGATING';c.resolution='';c.version+=1
                    case_event(db,c,user,'CASE_REOPENED_BY_REVERSAL',cb,body.reason)
        else: m.status=m.evidence.get('previous_status','APPROVED')
    elif body.action=='APPROVE':
        # Revalidate current normalized source constraints, including exact grouping amounts.
        if m.method!='L3_TOLERANCE': match_guard(db,t,m.bank_ids,m.book_ids)
        else:
            raise HTTPException(422,'存在金额残差，必须另行建立调整证据，当前版本不允许直接批准抹平')
        if m.status=='PENDING': reserve(db,m)
        m.status='APPROVED'
        approved_ids=set(m.bank_ids+m.book_ids)
        for other in active_matches(db,t):
            if other.id!=m.id and other.status=='PENDING' and approved_ids.intersection(other.bank_ids+other.book_ids):
                other.active=False;other.status='SUPERSEDED';other.version+=1
        for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True,Case.status!='CLOSED')):
            if set(c.transaction_ids)<=approved_ids:
                from .assurance import assert_case_evidence
                assert_case_evidence(db,c,resolution='MATCH_APPROVED',require_material=False)
                cb=c.status;c.status='CLOSED';c.resolution='MATCH_APPROVED';c.version+=1
                case_event(db,c,user,'CASE_CLOSED_BY_MATCH',cb,body.reason)
    else:
        m.status='REJECTED';m.active=False
        db.execute(update(MatchMember).where(MatchMember.match_id==m.id).values(active=False))
        c=Case(tenant_id=t.tenant_id,task_id=t.id,run_id=t.current_run,kind='MATCH_REJECTED',title='匹配被拒绝或退回，需重新调查',transaction_ids=m.bank_ids,candidates=m.book_ids,note=body.reason)
        db.add(c)
    m.reviewer_id=user.id;m.version+=1;t.version+=1
    invalidate_reports(db,t.id);audit(db,user,'MATCH_'+body.action,m.id,{'status':before},{'status':m.status,'version':m.version},body.reason)
    result={'id':m.id,'status':m.status,'version':m.version}
    db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest([oid,body.model_dump()]),result=result));db.commit();return result

@app.post('/api/matches/{oid}/reversal')
def reversal(oid:str,body:CaseUpdate,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant');lock(db);m,t=related_task(db,Match,oid,user);version_guard(m,body.version)
    if not m.active or m.status not in ['APPROVED','DETERMINISTIC']: raise HTTPException(409,'仅有效已确认/确定匹配可申请撤销')
    before=m.status;m.evidence={**m.evidence,'previous_status':m.status,'reversal_requester':user.id,'reversal_reason':body.reason}
    m.status='REVERSAL_PENDING';m.version+=1;t.version+=1;invalidate_reports(db,t.id)
    audit(db,user,'REVERSAL_REQUEST',m.id,{'status':before},{'status':m.status},body.reason);db.commit();return {'id':m.id,'version':m.version}

def case_list_json(db,c):
    source=[db.get(Transaction,i) for i in c.transaction_ids]
    related=[db.get(Transaction,i) for i in c.candidates]
    source=[t for t in source if t];related=[t for t in related if t]
    first=source[0] if source else (related[0] if related else None)
    currency=first.currency if first else ''
    source_total=sum(t.amount for t in source);related_total=sum(t.amount for t in related)
    delta=abs(source_total-related_total) if related else 0
    timezone=ZoneInfo('Asia/Shanghai')
    transaction_date=min((transaction.date for transaction in (source or related) if transaction.date),default='')
    age_start=date.fromisoformat(transaction_date) if transaction_date else datetime.fromisoformat(c.created_at).astimezone(timezone).date()
    return {**model_dict(c,['id','task_id','kind','title','status','transaction_ids','candidates','owner_id','due_date','resolution','note','version','created_at']),
            'side':first.side if first else '', 'counterparty':first.counterparty if first else '',
            'date':first.date if first else '', 'currency':currency,
            'source_amount':amount_text(source_total,currency) if source and currency else '',
            'related_amount':amount_text(related_total,currency) if related and currency else '',
            'amount_delta':amount_text(delta,currency) if related and currency else '',
            'age_days':max(0,(datetime.now(timezone).date()-age_start).days)}

@app.get('/api/cases')
def cases(task_id:str='',status:str='',kind:str='',search:str='',sort:str='',page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),user:User=USER,db:Session=DB):
    ts=allowed_tasks(db,user)
    if task_id: ts=[scoped(db,Task,task_id,user)]
    task_names={t.id:t.name for t in ts}
    rows=list(db.scalars(select(Case).where(Case.task_id.in_([t.id for t in ts]),Case.active==True).order_by(Case.created_at.desc(),Case.id)))
    rows=[c for c in rows if (not status or c.status==status) and (not kind or c.kind==kind)]
    items=[case_list_json(db,c) for c in rows]
    terms=[tok.strip().lower() for tok in search.split() if tok.strip()]
    if terms:
        items=[it for it in items if all(tok in f"{it['title']} {it['kind']} {it['note']} {it['counterparty']} {it['date']} {it['source_amount']} {task_names.get(it['task_id'],'')}".lower() for tok in terms)]
    if sort=='oldest':items.sort(key=lambda item:(item['created_at'],item['id']))
    elif sort=='task':items.sort(key=lambda item:(task_names.get(item['task_id'],''),item['created_at']))
    elif sort=='difference':
        def magnitude(item):
            amount=item['amount_delta'] or item['source_amount']
            return (item['currency'], -abs(money(amount,item['currency'])) if amount else 0)
        items.sort(key=magnitude)
    elif sort=='age':items.sort(key=lambda item:(-item['age_days'],item['created_at']))
    elif sort=='due':items.sort(key=lambda x:(x['due_date'] or '9999-12-31',x['created_at']))
    elif sort=='owner':items.sort(key=lambda x:(x['owner_id'] or 'zzzz',x['created_at']))
    return {'total':len(items),'items':items[(page-1)*page_size:page*page_size]}

@app.get('/api/tasks/{task_id}/case-groups')
def case_groups(task_id:str,status:str='',kind:str='',user:User=USER,db:Session=DB):
    """Read-only aggregation by exception type + counterparty + currency.

    It never resolves or closes a case; it only groups the queue so a reviewer
    can see "70 book-only rows for the same counterparty" as one business problem.
    """
    t=scoped(db,Task,task_id,user)
    rows=list(db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True)))
    rows=[c for c in rows if (not status or c.status==status) and (not kind or c.kind==kind)]
    grouped=defaultdict(list)
    for c in rows:
        txs=[db.get(Transaction,i) for i in c.transaction_ids]
        txs=[x for x in txs if x]
        tx=txs[0] if txs else None
        key=(c.kind,tx.counterparty if tx else '未提供',tx.currency if tx else '')
        grouped[key].append((c,txs))
    groups=[]
    for (kind_name,counterparty,currency),items in grouped.items():
        # A case can contain two sides (e.g. MATCH_REJECTED); sum every member,
        # not just the first transaction.
        total=sum(abs(x.amount) for _,txs in items for x in txs)
        dates=sorted(x.date for _,txs in items for x in txs if x.date)
        groups.append({'kind':kind_name,'counterparty':counterparty,'currency':currency,'count':len(items),
                       'total_abs':amount_text(total,currency) if currency else '', 'date_from':dates[0] if dates else '',
                       'date_to':dates[-1] if dates else '', 'sample_case_id':items[0][0].id,
                       'open':sum(c.status!='CLOSED' for c,_ in items),
                       'statuses':dict(Counter(c.status for c,_ in items)),
                       'case_ids':[c.id for c,_ in items[:100]],
                       'truncated':len(items)>100})
    groups.sort(key=lambda g:(-g['count'],g['counterparty']))
    return {'total_groups':len(groups),'total_cases':len(rows),'truncated':len(groups)>200,
            'scope':'按异常类型+对方+币种聚合，只读；不改变任何异常状态','groups':groups[:200]}

@app.get('/api/cases/{oid}')
def case_detail(oid:str,user:User=USER,db:Session=DB):
    c,t=related_task(db,Case,oid,user)
    return {**model_dict(c,['id','task_id','kind','title','status','owner_id','due_date','resolution','note','analysis','version','submitter_id','created_at']),
            'period_state':period_json(db,t)['state'],
            'transactions':[serialize_tx(db.get(Transaction,i)) for i in c.transaction_ids],
            'candidates':[serialize_tx(db.get(Transaction,i)) for i in c.candidates],
            'events':[model_dict(e,['id','actor_id','action','before','after','reason','created_at']) for e in db.scalars(select(CaseEvent).where(CaseEvent.case_id==c.id).order_by(CaseEvent.created_at))]}

@app.patch('/api/cases/{oid}')
def edit_case(oid:str,body:CaseUpdate,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant','Reviewer');lock(db);c,t=related_task(db,Case,oid,user);version_guard(c,body.version)
    if not c.active: raise HTTPException(409,'旧运行异常仅供历史查阅')
    before=c.status
    if c.status=='CLOSED' and body.action!='REOPEN':raise HTTPException(409,'异常已关闭，请先申请重新调查')
    if body.action=='ASSIGN':
        owner=scoped(db,User,body.owner_id,user)
        if t.account_id not in owner.account_ids or not owner.enabled:raise HTTPException(422,'处理人无此账户权限')
        if body.due_date:
            try: date.fromisoformat(body.due_date)
            except ValueError: raise HTTPException(422,'截止日期格式错误')
        c.owner_id=owner.id;c.due_date=body.due_date;c.status='ASSIGNED'
    elif body.action in ['INVESTIGATE','REOPEN']:c.status='INVESTIGATING'
    elif body.action=='SUBMIT':
        if not body.resolution:raise HTTPException(422,'请指定人工处理分类')
        check_resolution(db,c,body.resolution)
        from .assurance import ensure_requirements
        ensure_requirements(db,c)
        c.resolution=body.resolution;c.submitter_id=user.id;c.status='READY_REVIEW'
    c.note=body.reason;c.version+=1;t.version+=1;invalidate_reports(db,t.id)
    case_event(db,c,user,'CASE_'+body.action,before,body.reason);db.commit();return case_detail(oid,user,db)

def check_resolution(db,c,resolution):
    rows=[db.get(Transaction,i) for i in c.transaction_ids]
    if resolution in ['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']:
        expected_side='BOOK' if resolution.startswith('BOOK') else 'BANK'
        expected_positive=resolution.endswith('RECEIPT')
        if any(t.side!=expected_side or (t.amount>0)!=expected_positive or t.quality!='VALID' for t in rows):
            raise HTTPException(422,'未达分类与记录侧别/现金方向/有效状态不一致')
        ids=set(c.transaction_ids)
        if db.scalar(select(MatchMember).where(MatchMember.transaction_id.in_(ids),MatchMember.active==True)):
            raise HTTPException(409,'已匹配交易不能重复登记未达项')
        for other in db.scalars(select(Case).where(Case.task_id==c.task_id,Case.id!=c.id,Case.active==True,Case.status=='CLOSED')):
            if ids.intersection(other.transaction_ids):raise HTTPException(409,'交易已在其他已关闭异常中分类')

@app.post('/api/cases/{oid}/decision')
def case_decision(oid:str,body:Decision,user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer');lock(db);c,t=related_task(db,Case,oid,user)
    old,key=receipt_get(db,user,body.idempotency_key,[oid,body.model_dump()])
    if old:return old
    version_guard(c,body.version)
    if c.status!='READY_REVIEW' or not c.active:raise HTTPException(409,'异常尚未提交复核或已过期')
    if c.submitter_id==user.id:raise HTTPException(403,'禁止审核本人提交的异常处理')
    before=c.status
    if body.action=='APPROVE':
        from .assurance import assert_case_evidence
        assert_case_evidence(db,c)
        check_resolution(db,c,c.resolution);c.status='CLOSED'
    else:c.status='WAITING_EVIDENCE' if body.action=='REQUEST_EVIDENCE' else 'INVESTIGATING'
    c.version+=1;t.version+=1;invalidate_reports(db,t.id);case_event(db,c,user,'CASE_'+body.action,before,body.reason)
    result={'id':c.id,'status':c.status,'version':c.version}
    db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest([oid,body.model_dump()]),result=result));db.commit();return result

@app.post('/api/cases/{oid}/investigate')
def ai_investigate(oid:str,request:Request,user:User=USER,db:Session=DB):
    from types import SimpleNamespace
    role(user,'Admin','Accountant','Reviewer');lock(db);c,t=related_task(db,Case,oid,user)
    require_open_period(db,t)
    if not c.active:raise HTTPException(409,'旧证据版本不允许发起调查')
    if c.status=='CLOSED':raise HTTPException(409,'异常已关闭')
    previous=c.status;c.version+=1;c.status='INVESTIGATING';t.version+=1
    ids=sorted(set(c.transaction_ids+c.candidates))
    rows=[db.get(Transaction,i) for i in ids]
    snapshot=SimpleNamespace(id=c.id,kind=c.kind,version=c.version)
    evidence_hash=digest(safe_evidence(snapshot,rows))
    invalidate_reports(db,t.id)
    case_event(db,c,user,'AI_INVESTIGATION_REQUEST',previous,'开始只读脱敏调查，模型等待期间不持有业务写锁')
    db.commit()
    # Network I/O is outside every database transaction. Metadata is committed
    # separately so even stale/discarded results retain a provider call trace.
    with SessionLocal() as invocation_db:
        result=investigate(invocation_db,user,snapshot,rows)
        invocation_db.commit()
    lock(db);db.expire_all()
    invocation=db.get(AIInvocation,result['invocation_id'])
    try:
        fresh_user=current_user(request,db,SimpleNamespace(credentials=request.headers.get('authorization','').split(' ',1)[-1]))
        role(fresh_user,'Admin','Accountant','Reviewer')
        c,t=related_task(db,Case,oid,fresh_user)
    except HTTPException:
        invocation.status='DISCARDED';invocation.error_code='AUTHORIZATION_CHANGED';db.commit()
        raise
    current_rows=[db.get(Transaction,i) for i in sorted(set(c.transaction_ids+c.candidates))]
    stale=(not c.active or c.status!='INVESTIGATING' or c.version!=snapshot.version
           or period_json(db,t)['state']!='OPEN' or digest(safe_evidence(c,current_rows))!=evidence_hash)
    if stale:
        invocation.status='STALE';invocation.error_code='EVIDENCE_OR_STATE_CHANGED'
        audit(db,fresh_user,'AI_RESULT_DISCARDED',c.id,{'input_version':snapshot.version},{'current_version':c.version},'调查期间证据或状态变化，保留调用元数据但不覆盖人工处理')
        db.commit();raise HTTPException(409,'调查期间证据或处理状态已变化，建议未写入；请刷新后重新调查')
    previous=c.status;c.analysis=result;c.version+=1;t.version+=1;invalidate_reports(db,t.id)
    case_event(db,c,fresh_user,'AI_INVESTIGATION',previous,'只读脱敏建议已通过版本校验，不确认匹配或改账')
    db.commit();return c.analysis

@app.get('/api/approvals')
def approvals(page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),task_id:str='',user:User=USER,db:Session=DB):
    ts=allowed_tasks(db,user)
    if task_id:
        ts=[scoped(db,Task,task_id,user)]
    ids=[t.id for t in ts]
    ms=list(db.scalars(select(Match).where(Match.task_id.in_(ids),Match.active==True,Match.status.in_(['PENDING','REVERSAL_PENDING']))))
    cs=list(db.scalars(select(Case).where(Case.task_id.in_(ids),Case.active==True,Case.status=='READY_REVIEW')))
    eligible_tasks=[t.id for t in ts if period_json(db,t)['state']=='OPEN']
    # Serializing every deterministic L1/L2 group on every page is the main
    # approval-page cost; the controlled bulk UI only accepts at most 100 at a
    # time, so return the first 100 plus an explicit total.
    deterministic=list(db.scalars(select(Match).join(Run,Match.run_id==Run.id).where(Match.task_id.in_(eligible_tasks),Match.active==True,Match.status=='DETERMINISTIC',Run.rule_version.startswith(ENGINE_VERSION+':')).order_by(Match.created_at.desc(),Match.id)))
    deterministic_page=deterministic[:100]
    page_ms=ms[(page-1)*page_size:page*page_size]
    return {'matches':[serialize_match(db,m) for m in page_ms], 'match_total':len(ms),
            'deterministic':[serialize_match(db,m) for m in deterministic_page], 'deterministic_total':len(deterministic),
            'cases':[model_dict(c,['id','task_id','kind','title','status','version','submitter_id']) for c in cs]}

@app.post('/api/tasks/{task_id}/reports')
def report_create(task_id:str,reuse_current:bool=False,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,task_id,user)
    require_open_period(db,t)
    if not t.current_run:raise HTTPException(422,'执行匹配后才能生成报告')
    snap=make_snapshot(db,t)
    if reuse_current:
        prev=db.scalar(select(Report).where(Report.task_id==t.id,Report.run_id==t.current_run,Report.creator_id==user.id,Report.status=='DRAFT').order_by(Report.created_at.desc()).limit(1))
        if prev:
            comparable={**snap,'generated_at':prev.snapshot['generated_at'],'summary':{**snap['summary'],'data_as_of':prev.snapshot['summary']['data_as_of']}}
            if digest(comparable)==prev.sha256:return report_json(prev)
    r=Report(id=uid(),tenant_id=user.tenant_id,task_id=t.id,run_id=t.current_run,creator_id=user.id,snapshot=snap,sha256=digest(snap))
    db.add(r);audit(db,user,'REPORT_CREATE',r.id,{}, {'snapshot_hash':r.sha256},'生成不可变逐笔报告草稿');db.commit();return report_json(r)

def report_json(r,include=False):
    result=model_dict(r,['id','task_id','run_id','creator_id','signer_id','status','sha256','version','created_at'])
    if include:result['snapshot']=r.snapshot
    return result

@app.get('/api/tasks/{task_id}/reports')
def reports(task_id:str,user:User=USER,db:Session=DB):
    t=scoped(db,Task,task_id,user)
    return [report_json(r) for r in db.scalars(select(Report).where(Report.task_id==t.id).order_by(Report.created_at.desc()))]

@app.get('/api/reports/{oid}')
def report_detail(oid:str,user:User=USER,db:Session=DB):
    r,t=related_task(db,Report,oid,user);return report_json(r,True)

@app.post('/api/reports/{oid}/sign')
def sign_report(oid:str,body:Decision,user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer');lock(db);r,t=related_task(db,Report,oid,user)
    old,key=receipt_get(db,user,body.idempotency_key,[oid,body.model_dump()])
    if old:return old
    version_guard(r,body.version)
    # Previously generated automatic groups must also satisfy the current safety
    # rules. This prevents legacy drafts silently retaining a now-known risk.
    refreshed=run_matching(rows_for_task(db,t),t.rule_config)
    eligible={(tuple(sorted(m['bank_ids'])),tuple(sorted(m['book_ids']))) for m in refreshed['matches'] if m['status']=='DETERMINISTIC'}
    if any(m.status=='DETERMINISTIC' and (tuple(sorted(m.bank_ids)),tuple(sorted(m.book_ids))) not in eligible for m in active_matches(db,t)):
        raise HTTPException(422,'旧自动匹配不满足当前安全规则，请重新匹配并逐组复核风险项')
    if body.action!='APPROVE' or r.status!='DRAFT':raise HTTPException(409,'只有报告草稿可签署')
    if r.creator_id==user.id or t.creator_id==user.id:raise HTTPException(403,'最终报告须由独立审核者签署')
    from .assurance import assert_report_evidence
    assert_report_evidence(db,t)
    from .carry import assert_carry_sign,cleared_ids
    opening_bridge=assert_carry_sign(db,t)
    s=task_summary(db,t)
    require_open_period(db,t)
    control=s['balance_control']
    if any(control[side]['status']!='PASS' for side in ['BANK','BOOK']):
        raise HTTPException(422,'最终签署须两侧余额完整性均通过；缺失或不平时仅允许导出草稿')
    adjustment=balance_adjustment(db,t,rows_for_task(db,t))
    if adjustment.get('status')!='CALCULATED' or money(adjustment['gap'],t.currency)!=0 or (money(adjustment['opening_gap'],t.currency)!=0 and not opening_bridge):
        raise HTTPException(422,'调节后差额或期初差异未清零；必须补充逐项证据，不得仅为凑平签署')
    if s['pending_matches'] or s['open_cases'] or any(s['sides'][side]['quarantined'] or s['sides'][side]['unmatched'] for side in ['BANK','BOOK']):
        # Classified timing items may remain unmatched, but all require closed, reviewed cases.
        closed_ids={i for c in db.scalars(select(Case).where(Case.task_id==t.id,Case.active==True,Case.status=='CLOSED',Case.resolution.in_(['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']))) for i in c.transaction_ids}
        matched_ids={i for m in active_matches(db,t) if m.status in ['APPROVED','DETERMINISTIC'] for i in m.bank_ids+m.book_ids}
        matched_ids.update(cleared_ids(db,t))
        unresolved=[x for x in rows_for_task(db,t) if x.quality=='QUARANTINED' or (x.quality=='VALID' and x.id not in closed_ids|matched_ids)]
        if s['pending_matches'] or s['open_cases'] or unresolved:raise HTTPException(422,'存在隔离、未复核建议或未处理异常，不能签署完成结论')
    # Regenerate check ignoring volatile generated_at, data_as_of.
    fresh=make_snapshot(db,t);fresh['generated_at']=r.snapshot['generated_at'];fresh['summary']['data_as_of']=r.snapshot['summary']['data_as_of']
    if digest(fresh)!=r.sha256:raise HTTPException(409,'报告快照已过期，请生成新报告')
    r.status='SIGNED';r.signer_id=user.id;r.version+=1;t.status='COMPLETED'
    seal_period(db,t,r.id)
    audit(db,user,'REPORT_SIGN',r.id,{'status':'DRAFT'},{'status':'SIGNED','sha256':r.sha256},body.reason)
    from .report_evidence import record_signature
    record_signature(db,r,t,user,body.reason)
    result=report_json(r);db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest([oid,body.model_dump()]),result=result));db.commit();return result

def safe_text(value):
    value=str(value if value is not None else '')
    if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r')):return "'"+value
    return value

def report_rows(r):
    lookup={i:(m['id'],m['method'],m['status']) for m in r.snapshot['matches'] for i in m['bank_ids']+m['book_ids']}
    allocated=defaultdict(int);groups=defaultdict(list)
    for c in r.snapshot.get('outstanding_control',{}).get('clearings',[]):
        if c['status'] in ['APPROVED','REVERSAL_PENDING']:
            for a in c['allocations']:allocated[a['transaction_id']]+=int(a['amount_minor']);groups[a['transaction_id']].append(c['id'])
    for tx in r.snapshot['transactions']:
        if tx['id'] in allocated and allocated[tx['id']]==abs(money(tx['amount'],tx['currency'])):lookup[tx['id']]=('|'.join(groups[tx['id']]),'PRIOR_CLEARING','APPROVED')
    rows=[['交易ID','侧别','原始行','日期','金额','币种','对方名称','摘要','数据质量','匹配组','匹配方式','匹配状态','报告状态']]
    for tx in r.snapshot['transactions']:
        m=lookup.get(tx['id'],('','','UNMATCHED'))
        rows.append([tx['id'],tx['side'],tx['row_no'],tx['date'],tx['amount'],tx['currency'],tx['counterparty'],tx['description'],tx['quality'],*m,r.status])
    return rows

def csv_bytes(rows):
    out=io.StringIO(newline='');w=csv.writer(out)
    for index,row in enumerate(rows):w.writerow([v if index and j==4 else safe_text(v) for j,v in enumerate(row)])
    return ('\ufeff'+out.getvalue()).encode('utf-8')

@app.get('/api/reports/{oid}/export')
def export_report(oid:str,format:str='xlsx',own_draft:bool=False,user:User=USER,db:Session=DB):
    if not own_draft or user.role!='Accountant':role(user,'Admin','Reviewer','Auditor')
    lock(db);r,t=related_task(db,Report,oid,user)
    if user.role=='Accountant' and (r.creator_id!=user.id or r.status!='DRAFT' or r.run_id!=t.current_run or format!='xlsx'):
        raise HTTPException(403,'财务经办仅可输出本人未失效的工作底稿 Excel')
    rows=report_rows(r)
    if format=='csv':content=csv_bytes(rows);mime='text/csv; charset=utf-8'
    elif format=='xlsx':
        wb=openpyxl.Workbook();ws=wb.active;ws.title='逐笔对账'
        from decimal import Decimal
        for index,row in enumerate(rows):
            ws.append([str(v) if index and j==4 else v if isinstance(v,int) else safe_text(v) for j,v in enumerate(row)])
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=openpyxl.styles.Font(color='FFFFFF',bold=True);cell.fill=openpyxl.styles.PatternFill('solid',fgColor='1938A8')
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(45,max(14,max(len(str(c.value or '')) for c in col)+2))
        summary=wb.create_sheet('控制总数');summary.append(['侧别','原始','有效','隔离','排除','已确定/确认','未匹配','自动覆盖率'])
        for side in ['BANK','BOOK']:
            s=r.snapshot['summary']['sides'][side];summary.append([side,*[s[k] for k in ['raw','valid','quarantined','excluded','matched','unmatched','coverage']]])
        summary.append(['报告状态',r.status]);summary.append(['快照SHA256',r.sha256]);summary.append(['结论',r.snapshot['disclaimer']])
        from .report_evidence import append_sheets
        append_sheets(wb,db,r)
        buffer=io.BytesIO();wb.save(buffer);content=buffer.getvalue();mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif format=='zip':
        from .report_evidence import exit_package
        content=exit_package(db,r,t,rows,csv_bytes(rows));mime='application/zip'
    else:raise HTTPException(422,'支持xlsx/csv/zip')
    audit(db,user,'REPORT_EXPORT',r.id,{}, {'format':format,'sha256':r.sha256,'report_status':r.status},'下载有权限的报告快照');db.commit()
    return StreamingResponse(io.BytesIO(content),media_type=mime,headers={'Content-Disposition':f'attachment; filename="BankMind-{r.id[:8]}.{format}"'})

@app.get('/api/audit')
def audit_events(task_id:str='',action:str='',search:str='',sort:str='',page:int=Query(1,ge=1),page_size:int=Query(30,ge=1,le=100),user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer','Auditor')
    events=list(db.scalars(select(Audit).where(Audit.tenant_id==user.tenant_id).order_by(Audit.created_at.desc(),Audit.id.desc())))
    # Audit scope is restricted to objects in authorized tasks plus own identity/authorized settings.
    ts=allowed_tasks(db,user);ids={t.id for t in ts}
    if task_id:ids={scoped(db,Task,task_id,user).id}
    object_ids=set(ids)
    for model in [ImportBatch,Transaction,Run,Match,Case,Report,EvidenceAttachment,SourceControl,LearningFeedback,LearningModel]:object_ids.update(db.scalars(select(model.id).where(model.task_id.in_(ids))))
    if not task_id and user.role=='Admin':object_ids.update(db.scalars(select(User.id).where(User.tenant_id==user.tenant_id)));object_ids.update(user.account_ids)
    terms=[tok.strip().lower() for tok in search.split() if tok.strip()]
    events=[e for e in events if e.object_id in object_ids and (not action or e.action==action) and (not terms or all(tok in f"{e.action} {e.reason} {e.actor_id} {e.object_id}".lower() for tok in terms))]
    if sort=='oldest': events.reverse()
    elif sort=='action': events.sort(key=lambda e:(e.action,e.created_at))
    return {'total':len(events),'items':[model_dict(e,['id','actor_id','action','object_id','before','after','reason','prev_hash','event_hash','created_at']) for e in events[(page-1)*page_size:page*page_size]]}

@app.get('/api/ai/calls')
def ai_calls(user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer','Auditor')
    ids={t.id for t in allowed_tasks(db,user)}
    object_ids=set(db.scalars(select(Case.id).where(Case.task_id.in_(ids))))|set(db.scalars(select(ImportBatch.id).where(ImportBatch.task_id.in_(ids))))
    outcomes={o.invocation_id:o for o in db.scalars(select(AIOutcome).where(AIOutcome.tenant_id==user.tenant_id))}
    return [{**model_dict(a,['id','object_id','provider','model','status','elapsed_ms','evidence_ids','error_code','created_at']),
             'provider_outcome':model_dict(outcomes[a.id],['attempted_provider','result_provider','provider_status','transmission_state','provider_error_code','network_attempts']) if a.id in outcomes else None}
            for a in db.scalars(select(AIInvocation).where(AIInvocation.tenant_id==user.tenant_id,AIInvocation.object_id.in_(object_ids)).order_by(AIInvocation.created_at.desc()).limit(100))]

@app.get('/api/tasks/{task_id}/period')
def get_period(task_id:str,user:User=USER,db:Session=DB):
    t=scoped(db,Task,task_id,user)
    return period_json(db,t)

@app.post('/api/tasks/{task_id}/period/reopen-request')
def request_period_reopening(task_id:str,body:PeriodRequest,user:User=USER,db:Session=DB):
    role(user,'Admin','Accountant','Reviewer');lock(db);t=scoped(db,Task,task_id,user)
    state=period_json(db,t)
    if state['version']!=body.version or state['state']!='LOCKED':
        raise HTTPException(409,'期间状态或版本已变化，请刷新')
    p=period_record(db,t)
    if p is None:
        p=PeriodControl(tenant_id=user.tenant_id,task_id=t.id,version=1,state='LOCKED');db.add(p)
    p.state='REOPEN_REQUESTED';p.requester_id=user.id;p.reason=body.reason;p.version+=1
    audit(db,user,'PERIOD_REOPEN_REQUEST',t.id,{'state':'LOCKED'},{'state':p.state,'version':p.version},body.reason)
    db.commit();return period_json(db,t)

@app.post('/api/tasks/{task_id}/period/decision')
def decide_period_reopening(task_id:str,body:Decision,user:User=USER,db:Session=DB):
    role(user,'Admin','Reviewer');lock(db);t=scoped(db,Task,task_id,user)
    old,key=receipt_get(db,user,body.idempotency_key,['period',task_id,body.model_dump()])
    if old:return old
    p=period_record(db,t)
    if not p or p.version!=body.version or p.state!='REOPEN_REQUESTED':raise HTTPException(409,'期间申请已变化，请刷新')
    if p.requester_id==user.id:raise HTTPException(403,'禁止审批本人提交的期间重开申请')
    p.state='OPEN' if body.action=='APPROVE' else 'LOCKED';p.version+=1
    if body.action=='APPROVE':
        from .carry import forbid_reopen_with_dependencies
        forbid_reopen_with_dependencies(db,t)
        t.status='REVIEW';t.version+=1
        invalidate_reports(db,t.id)
    audit(db,user,'PERIOD_REOPEN_'+body.action,t.id,{'state':'REOPEN_REQUESTED'},{'state':p.state,'version':p.version},body.reason)
    result=period_json(db,t)
    db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(['period',task_id,body.model_dump()]),result=result))
    db.commit();return result

from .carry import install_carry
install_carry(app)
from .controls import install_controls
install_controls(app)
from .assurance import install_assurance
install_assurance(app)
from .data_library import install_data_library
install_data_library(app)
from .effort import install_effort
install_effort(app)
from .learning import install_learning
install_learning(app)

web_dist=ROOT/'apps/web/dist'
if web_dist.exists():app.mount('/',StaticFiles(directory=web_dist,html=True),name='web')
