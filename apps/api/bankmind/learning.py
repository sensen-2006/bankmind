"""Audited local feedback learning. Models provide advisory scores only.

No provider calls, pickle, background retraining, or writes to matches/transactions.
Approved labels, immutable dataset snapshots and source-file-group holdout are required.
"""
import math
from collections import Counter
from datetime import date
from typing import Literal
from fastapi import Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from rapidfuzz.fuzz import ratio
from .db import (get_db, Task, Account, Transaction, ImportBatch, Match, Receipt, Audit,
                 LearningFeedback, LearningModel, LearningDeployment)
from .schemas import Input
from .security import current_user, role, scoped, digest, audit
from .parsing import normalize_name

FEATURES=['amount_equal','reference_equal','name_similarity','date_affinity','batch_equal','same_cardinality']
ALGORITHM='local-logistic-advisory-v1'

class FeedbackInput(Input):
    task_id:str
    bank_ids:list[str]=Field(min_length=1,max_length=3)
    book_ids:list[str]=Field(min_length=1,max_length=3)
    correct:bool
    reason:str=Field(min_length=12,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

class ReviewInput(Input):
    version:int=Field(ge=1)
    action:Literal['APPROVE','REJECT','WITHDRAW']
    reason:str=Field(min_length=8,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

class TrainInput(Input):
    account_id:str
    dataset_revision:str=Field(default='',max_length=64)
    reason:str=Field(min_length=8,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

class PublishInput(Input):
    version:int=Field(ge=1)
    deployment_version:int=Field(ge=0)
    reason:str=Field(min_length=8,max_length=2000)
    idempotency_key:str=Field(min_length=8,max_length=100)

def evidence(db,task,bank_ids,book_ids):
    ids=bank_ids+book_ids
    if len(ids)!=len(set(ids)):raise HTTPException(422,'反馈不能重复引用交易')
    rows=[db.get(Transaction,i) for i in ids]
    if any(not x or x.task_id!=task.id or x.tenant_id!=task.tenant_id for x in rows):
        raise HTTPException(404,'反馈交易不属于本任务')
    bank,book=rows[:len(bank_ids)],rows[len(bank_ids):]
    if any(x.side!='BANK' for x in bank) or any(x.side!='BOOK' for x in book):raise HTTPException(422,'银行和账面侧别错误')
    valid=all(x.quality=='VALID' and x.account_id==task.account_id and x.currency==task.currency for x in rows)
    btotal=sum(x.amount for x in bank);ltotal=sum(x.amount for x in book)
    amount_equal=btotal==ltotal and btotal!=0
    direction=bool(btotal) and all(x.amount*btotal>0 for x in rows)
    days=max(abs((date.fromisoformat(b.date)-date.fromisoformat(l.date)).days) for b in bank for l in book) if valid else 999
    brefs={x.reference for x in bank};lrefs={x.reference for x in book}
    bg={x.group_key for x in bank};lg={x.group_key for x in book}
    batch_conflict=bool(all(x.group_key for x in rows) and bg!=lg)
    hard_ok=bool(valid and amount_equal and direction and days<=task.rule_config.get('date_days',3) and not batch_conflict)
    features={'amount_equal':float(amount_equal),'reference_equal':float(bool(brefs==lrefs and '' not in brefs)),
        'name_similarity':sum(ratio(normalize_name(b.counterparty),normalize_name(l.counterparty))/100 if b.counterparty and l.counterparty else 0 for b in bank for l in book)/(len(bank)*len(book)),
        'date_affinity':1/(1+days),'batch_equal':float(bool(bg==lg and '' not in bg)),
        'same_cardinality':float(len(bank)==len(book)),'hard_ok':hard_ok}
    # Immutable input identity and rule binding. Approval-status changes do not alter the input.
    records=[{k:getattr(x,k) for k in ['id','import_id','row_no','side','amount','currency','date','reference','counterparty','group_key','quality']} for x in rows]
    batches=[db.get(ImportBatch,i) for i in sorted({x.import_id for x in rows})]
    source_group=digest(sorted(b.sha256 for b in batches))
    binding=digest({'task_id':task.id,'run':task.current_run,'account':task.account_id,'currency':task.currency,
        'period':[task.start_date,task.end_date],'rules':task.rule_config,'records':records,
        'imports':[(b.id,b.sha256,b.mapping) for b in batches]})
    # Deduplicate identical evidence copied into another task; do not learn repeated labels as new examples.
    sample_key=digest(sorted((x.side,x.date,x.amount,x.currency,x.reference,x.counterparty,x.group_key) for x in rows))
    return features,binding,source_group,sample_key

def is_current(db,f):
    task=db.get(Task,f.task_id)
    if not task:return False
    try:
        features,binding,*_=evidence(db,task,f.bank_ids,f.book_ids)
        return binding==f.binding_hash and features==f.features
    except (HTTPException,ValueError,TypeError,AttributeError):return False

def prefetch_sources(db,feedback):
    """Keep ORM identities alive for the whole validation, avoiding per-label SQL reads."""
    if not feedback:return ([],[],[])
    tasks=list(db.scalars(select(Task).where(Task.id.in_({f.task_id for f in feedback}))))
    txs=list(db.scalars(select(Transaction).where(Transaction.id.in_({i for f in feedback for i in f.bank_ids+f.book_ids}))))
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.id.in_({t.import_id for t in txs}))))
    return tasks,txs,batches

def feedback_json(db,f):
    return {**{k:getattr(f,k) for k in ['id','task_id','account_id','creator_id','reviewer_id','bank_ids','book_ids','correct','reason','review_reason','status','version','created_at']},'source_current':is_current(db,f)}

def sigmoid(z):return 1/(1+math.exp(-max(-35,min(35,z))))
def score(weights,features):
    return sigmoid(weights[0]+sum(w*features[k] for w,k in zip(weights[1:],FEATURES))) if features['hard_ok'] else 0.0

def fit(train):
    weights=[0.0]*(len(FEATURES)+1)
    for _ in range(450):
        grad=[0.0]*len(weights)
        for row in train:
            x=[1.0]+[row['features'][k] for k in FEATURES]
            error=sigmoid(sum(a*b for a,b in zip(weights,x)))-int(row['correct'])
            for i,v in enumerate(x):grad[i]+=error*v
        weights=[w-.4*(g/len(train)+(.005*w if i else 0)) for i,(w,g) in enumerate(zip(weights,grad))]
    return weights

def metrics(rows,weights):
    tp=fp=tn=fn=0;brier=0.0
    for row in rows:
        p=score(weights,row['features']);yes=p>=.5;truth=row['correct'];brier+=(p-int(truth))**2
        if yes and truth:tp+=1
        elif yes:fp+=1
        elif truth:fn+=1
        else:tn+=1
    return {'n':len(rows),'positive':tp+fn,'negative':tn+fp,'tp':tp,'fp':fp,'tn':tn,'fn':fn,
        'accuracy':round((tp+tn)/len(rows),4),'precision':round(tp/(tp+fp),4) if tp+fp else None,
        'recall':round(tp/(tp+fn),4) if tp+fn else None,'brier':round(brier/len(rows),5)}

def current_dataset(db,m):
    if digest(m.artifact)!=m.artifact_hash or m.artifact.get('algorithm')!=ALGORITHM:return False
    weights=m.artifact.get('weights',[])
    if len(weights)!=len(FEATURES)+1 or any(not isinstance(w,(float,int)) or not math.isfinite(w) or abs(w)>100 for w in weights):return False
    feedback=list(db.scalars(select(LearningFeedback).where(LearningFeedback.id.in_([x['id'] for x in m.artifact['dataset']]))))
    _source_cache=prefetch_sources(db,feedback)
    for item in m.artifact['dataset']:
        f=db.get(LearningFeedback,item['id'])
        if not f or f.tenant_id!=m.tenant_id or f.account_id!=m.account_id or f.status!='APPROVED' or f.version!=item['version'] or f.binding_hash!=item['binding_hash'] or f.correct!=item['correct'] or not is_current(db,f):return False
    return True

def model_json(db,m):
    deployment=db.get(LearningDeployment,m.account_id)
    return {**{k:getattr(m,k) for k in ['id','account_id','trainer_id','reviewer_id','status','version','artifact_hash','reason','review_reason','created_at']},
        'active':bool(deployment and deployment.model_id==m.id),'deployment_version':deployment.version if deployment else 0,
        'dataset_current':current_dataset(db,m),'metrics':m.artifact['metrics'],'publish_gate':m.artifact['publish_gate'],
        'train_count':len(m.artifact['train_ids']),'holdout_count':len(m.artifact['holdout_ids']),
        'split':'源文件分组留出，未完成真实企业/时间留出验证','algorithm':ALGORITHM}

def install_learning(app):
    from .main import lock,receipt_get
    DB=Depends(get_db);USER=Depends(current_user)
    def receipt(db,user,key,payload,result):
        db.add(Receipt(tenant_id=user.tenant_id,key=key,request_hash=digest(payload),result=result));db.commit();return result

    @app.get('/api/learning/feedback')
    def list_feedback(account_id:str='',page:int=Query(1,ge=1),page_size:int=Query(30,ge=1,le=100),db=DB,user=USER):
        if account_id:scoped(db,Account,account_id,user)
        q=select(LearningFeedback).where(LearningFeedback.tenant_id==user.tenant_id,LearningFeedback.account_id.in_(user.account_ids))
        if account_id:q=q.where(LearningFeedback.account_id==account_id)
        rows=list(db.scalars(q.order_by(LearningFeedback.created_at.desc(),LearningFeedback.id.desc())))
        _source_cache=prefetch_sources(db,rows)
        approved=[f for f in rows if f.status=='APPROVED' and is_current(db,f)]
        return {'total':len(rows),'approved':len(approved),'dataset_revision':digest(sorted((f.id,f.version,f.binding_hash) for f in approved)),
            'items':[feedback_json(db,f) for f in rows[(page-1)*page_size:page*page_size]]}

    @app.post('/api/learning/feedback')
    def submit_feedback(body:FeedbackInput,db=DB,user=USER):
        role(user,'Admin','Accountant','Reviewer');lock(db);payload=['learning-feedback',body.model_dump()]
        old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        task=scoped(db,Task,body.task_id,user)
        features,binding,group,sample=evidence(db,task,body.bank_ids,body.book_ids)
        if body.correct and not features['hard_ok']:raise HTTPException(422,'正向反馈违反账户、币种、质量、方向、精确合计或日期范围控制')
        duplicate=db.scalar(select(LearningFeedback).where(LearningFeedback.tenant_id==user.tenant_id,LearningFeedback.account_id==task.account_id,
            LearningFeedback.sample_key==sample,LearningFeedback.status.in_(['SUBMITTED','APPROVED'])))
        if duplicate:raise HTTPException(409,'相同证据已有待审或已审反馈；纠错请先独立撤回原反馈')
        f=LearningFeedback(tenant_id=user.tenant_id,task_id=task.id,account_id=task.account_id,creator_id=user.id,
            bank_ids=body.bank_ids,book_ids=body.book_ids,correct=body.correct,reason=body.reason,
            binding_hash=binding,sample_key=sample,source_group=group,features=features)
        db.add(f);db.flush();audit(db,user,'LEARNING_FEEDBACK_SUBMIT',f.id,after={'correct':f.correct,'binding_hash':binding},reason=body.reason)
        return receipt(db,user,key,payload,feedback_json(db,f))

    @app.post('/api/learning/feedback/{oid}/decision')
    def review_feedback(oid:str,body:ReviewInput,db=DB,user=USER):
        role(user,'Admin','Reviewer');lock(db);payload=['learning-review',oid,body.model_dump()]
        old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        f=scoped(db,LearningFeedback,oid,user)
        if f.creator_id==user.id:raise HTTPException(403,'反馈提交人不能审核或撤回本人反馈')
        if f.version!=body.version:raise HTTPException(409,'反馈版本已变化')
        if body.action=='WITHDRAW':
            if f.status!='APPROVED':raise HTTPException(409,'仅已批准反馈可撤回')
            status='WITHDRAWN'
        else:
            if f.status!='SUBMITTED':raise HTTPException(409,'反馈已处理')
            if body.action=='APPROVE' and not is_current(db,f):raise HTTPException(409,'来源范围已变化，需重新提交反馈')
            status='APPROVED' if body.action=='APPROVE' else 'REJECTED'
        before=f.status;f.status=status;f.reviewer_id=user.id;f.review_reason=body.reason;f.version+=1
        audit(db,user,'LEARNING_FEEDBACK_'+body.action,f.id,{'status':before},{'status':status,'version':f.version},body.reason)
        return receipt(db,user,key,payload,feedback_json(db,f))

    @app.post('/api/learning/train')
    def train(body:TrainInput,db=DB,user=USER):
        role(user,'Admin','Accountant','Reviewer');lock(db);payload=['learning-train',body.model_dump()]
        old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        scoped(db,Account,body.account_id,user)
        all_rows=list(db.scalars(select(LearningFeedback).where(LearningFeedback.tenant_id==user.tenant_id,
            LearningFeedback.account_id==body.account_id,LearningFeedback.status=='APPROVED').order_by(LearningFeedback.id)))
        if len(all_rows)>2000:raise HTTPException(422,'本地训练每次最多2000项已审反馈，请使用离线训练流程')
        _source_cache=prefetch_sources(db,all_rows)
        rows=[f for f in all_rows if is_current(db,f)]
        revision=digest(sorted((f.id,f.version,f.binding_hash) for f in rows))
        if body.dataset_revision and revision!=body.dataset_revision:raise HTTPException(409,'反馈集已变化，请刷新后重新训练')
        # A stable sample key is unique in approved input, including copies from different tasks.
        unique={f.sample_key:f for f in rows};rows=list(unique.values());counts=Counter(f.correct for f in rows)
        groups=sorted({f.source_group for f in rows})
        if len(rows)<40 or min(counts.get(True,0),counts.get(False,0))<10 or len(groups)<3:
            raise HTTPException(422,'至少40项当前有效、独立批准的去重反馈，正负各10项且来自至少3组源文件；未审反馈和AI建议不作为标签')
        if sum(not f.correct and f.features['hard_ok'] for f in rows)<10:
            raise HTTPException(422,'至少10项负例须通过基础匹配约束，避免只用金额不等等显然负例造成虚高评估')
        dataset=[{'id':f.id,'version':f.version,'binding_hash':f.binding_hash,'source_group':f.source_group,'sample_key':f.sample_key,
            'correct':f.correct,'features':f.features,'bank_ids':f.bank_ids,'book_ids':f.book_ids,
            'creator_id':f.creator_id,'reviewer_id':f.reviewer_id,'reason':f.reason,'review_reason':f.review_reason} for f in rows]
        # Stable group-stratified split selected by counts/classes, never by fitted performance.
        test_groups=set();train_rows=[];test_rows=[]
        for group in groups:
            test_groups.add(group)
            train_rows=[x for x in dataset if x['source_group'] not in test_groups];test_rows=[x for x in dataset if x['source_group'] in test_groups]
            if len(train_rows)>=20 and len(test_rows)>=max(8,len(rows)//5) and all(min(Counter(x['correct'] for x in subset).values(),default=0)>=2 and len({x['correct'] for x in subset})==2 for subset in [train_rows,test_rows]):break
        else:
            raise HTTPException(422,'按源文件分组后训练集/留出集不足或缺少正负类别，请增加独立来源样本')
        weights=fit(train_rows);evaluation=metrics(test_rows,weights)
        gate=bool(evaluation['precision']==1 and evaluation['fp']==0 and evaluation['recall'] is not None and evaluation['recall']>=.8)
        artifact={'algorithm':ALGORITHM,'features':FEATURES,'weights':weights,'dataset':dataset,
            'train_ids':[x['id'] for x in train_rows],'holdout_ids':[x['id'] for x in test_rows],
            'holdout_source_groups':sorted(test_groups),'metrics':evaluation,'publish_gate':gate,
            'limitations':'源文件分组的本地试验模型，非校准置信度；不能证明真实企业误配率或自动批准。'}
        model=LearningModel(tenant_id=user.tenant_id,task_id=rows[0].task_id,account_id=body.account_id,trainer_id=user.id,
            artifact=artifact,artifact_hash=digest(artifact),reason=body.reason)
        db.add(model);db.flush();audit(db,user,'LEARNING_MODEL_TRAIN',model.id,after={'artifact_hash':model.artifact_hash,'metrics':evaluation,'publish_gate':gate},reason=body.reason)
        return receipt(db,user,key,payload,model_json(db,model))

    @app.get('/api/learning/models')
    def models(account_id:str='',db=DB,user=USER):
        if account_id:scoped(db,Account,account_id,user)
        q=select(LearningModel).where(LearningModel.tenant_id==user.tenant_id,LearningModel.account_id.in_(user.account_ids))
        if account_id:q=q.where(LearningModel.account_id==account_id)
        return [model_json(db,m) for m in db.scalars(q.order_by(LearningModel.created_at.desc()))]

    @app.post('/api/learning/models/{oid}/activate')
    def activate(oid:str,body:PublishInput,db=DB,user=USER):
        role(user,'Admin','Reviewer');lock(db);payload=['learning-activate',oid,body.model_dump()]
        old,key=receipt_get(db,user,body.idempotency_key,payload)
        if old:return old
        m=scoped(db,LearningModel,oid,user)
        if m.trainer_id==user.id:raise HTTPException(403,'训练发起人不能发布本人模型')
        if m.version!=body.version:raise HTTPException(409,'模型版本已变化')
        if not current_dataset(db,m):raise HTTPException(409,'模型或反馈来源已变化，请重新训练')
        if not m.artifact['publish_gate']:raise HTTPException(422,'留出评估未达到发布门槛')
        d=db.get(LearningDeployment,m.account_id)
        if body.deployment_version!=(d.version if d else 0):raise HTTPException(409,'当前发布版本已变化')
        previous=d.model_id if d else '';rollback=m.status=='PUBLISHED'
        if d:d.model_id=m.id;d.version+=1;d.publisher_id=user.id
        else:db.add(LearningDeployment(id=m.account_id,tenant_id=user.tenant_id,account_id=m.account_id,model_id=m.id,publisher_id=user.id))
        m.status='PUBLISHED';m.version+=1;m.reviewer_id=user.id;m.review_reason=body.reason;db.flush()
        audit(db,user,'LEARNING_MODEL_ROLLBACK' if rollback else 'LEARNING_MODEL_PUBLISH',m.id,{'active_model':previous},{'active_model':m.id,'artifact_hash':m.artifact_hash},body.reason)
        return receipt(db,user,key,payload,model_json(db,m))

    @app.get('/api/learning/models/{oid}/evidence')
    def model_evidence(oid:str,db=DB,user=USER):
        lock(db);m=scoped(db,LearningModel,oid,user)
        feedback_ids=[x['id'] for x in m.artifact['dataset']]
        events=list(db.scalars(select(Audit).where(Audit.tenant_id==user.tenant_id,
            Audit.object_id.in_(feedback_ids+[m.id])).order_by(Audit.created_at,Audit.id)))
        result={'schema':'bankmind-learning-evidence-v1','model':model_json(db,m),'artifact':m.artifact,
            'audit_events':[{k:getattr(e,k) for k in ['id','actor_id','action','object_id','before','after','reason','created_at','prev_hash','event_hash']} for e in events],
            'limitations':'本地训练审计材料；哈希校验用于一致性，不是企业数字签名或真实客户泛化证明。'}
        audit(db,user,'LEARNING_EVIDENCE_EXPORT',m.id,after={'artifact_hash':m.artifact_hash},reason='授权导出训练集标签、留出范围、模型参数和审计依据')
        db.commit();return result

    @app.get('/api/learning/matches/{oid}/score')
    def match_score(oid:str,db=DB,user=USER):
        match=scoped(db,Match,oid,user);task=scoped(db,Task,match.task_id,user)
        d=db.get(LearningDeployment,task.account_id)
        if not d or d.tenant_id!=user.tenant_id:return {'state':'NO_MODEL','message':'尚无已独立发布的本账户学习模型'}
        m=scoped(db,LearningModel,d.model_id,user)
        if not current_dataset(db,m):return {'state':'STALE_MODEL','model_id':m.id,'message':'模型来源已变化，评分暂停；请重新训练并复核发布'}
        features,*_=evidence(db,task,match.bank_ids,match.book_ids)
        return {'state':'ADVISORY' if features['hard_ok'] else 'BLOCKED','model_id':m.id,'model_hash':m.artifact_hash,
            'deployment_version':d.version,'score':round(score(m.artifact['weights'],features),4),
            'message':'本地学习辅助评分，未经概率校准；仍需按原始证据独立复核，不改变匹配与审批。'}
