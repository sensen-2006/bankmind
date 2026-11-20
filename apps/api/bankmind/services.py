from collections import Counter, defaultdict
import hashlib
import json
import os
from time import monotonic
from decimal import Decimal
from sqlalchemy import select, update
from fastapi import HTTPException
from .db import (Task, Account, ImportBatch, Transaction, Run, Match, MatchMember, Case,
                 CaseEvent, Report, Audit, STORAGE, uid, now)
from .security import digest, audit
from .parsing import read_table, normalize_row, amount_text, money, suggest, MAX_BYTES
from .matching import run_matching, ENGINE_VERSION, delta_days, same_scope
from .periods import require_open_period
from .statement import StatementMismatch, verify_statement

def invalidate_reports(db, task_id):
    task=db.get(Task,task_id)
    require_open_period(db,task)
    db.execute(update(Report).where(Report.task_id == task_id, Report.status != 'SUPERSEDED').values(status='SUPERSEDED'))
    if task and task.status=='COMPLETED':task.status='REVIEW'

def rows_for_task(db, task):
    return list(db.scalars(select(Transaction).where(Transaction.task_id == task.id, Transaction.tenant_id == task.tenant_id)))

def active_matches(db, task):
    return list(db.scalars(select(Match).where(Match.task_id == task.id, Match.tenant_id == task.tenant_id, Match.active == True)))

def import_sheet(batch):
    return batch.quality.get('selected_sheet', batch.sheet)

def parse_digest(table):
    return digest({k:v for k,v in table.items() if k!='printed_statement_controls'})

def find_import(db, task, side, sha, sheet, header_row, visual=False):
    matches=[]
    candidates=select(ImportBatch).where(ImportBatch.tenant_id==task.tenant_id,ImportBatch.task_id==task.id,
                ImportBatch.side==side,ImportBatch.sha256==sha,ImportBatch.header_row==header_row)
    for batch in db.scalars(candidates.order_by(ImportBatch.created_at,ImportBatch.id)):
        sheets=batch.quality.get('sheets') or []
        if sheet==batch.sheet or import_sheet(batch)==(sheet or (sheets[0] if sheets else '')):
            matches.append(batch)
    return next((b for b in matches if b.status!='PREVIEWED'),None) or next((b for b in matches if (b.quality.get('encoding')=='document-ai')==visual),None)

def write_source(path, content):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('wb') as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)

def save_import(db, task, user, filename, side, sha, sheet, header_row, content, table, object_id=None):
    oid=object_id or uid()
    key='uploads/'+oid+'.'+filename.rsplit('.',1)[-1].lower()
    (STORAGE/'uploads').mkdir(exist_ok=True)
    write_source(STORAGE/key,content)
    quality={'sheets':table['sheets'],'selected_sheet':table['sheet'],'encoding':table['encoding'],'pdf_pages':table.get('pdf_pages',0)}
    if table['encoding']=='document-ai':
        extracted=json.dumps(table,ensure_ascii=False).encode('utf-8')
        write_source(STORAGE/(key+'.parsed.json'),extracted)
        quality.update(document_provider=table.get('provider','Google Gemini'),document_model=table['model'],extraction_sha256=hashlib.sha256(extracted).hexdigest(),external_consent_by=user.id)
        if 'page_ranges' in table:
            quality['page_ranges']=table['page_ranges']
    else:
        quality['parse_sha256']=parse_digest(table)
    batch=ImportBatch(id=oid,tenant_id=user.tenant_id,task_id=task.id,side=side,filename=filename,sha256=sha,object_key=key,
                      size=len(content),sheet=sheet,header_row=header_row,columns=table['columns'],preview=table['rows'][:20],
                      row_count=len(table['rows']),quality=quality)
    db.add(batch)
    audit(db,user,'FILE_UPLOAD',batch.id,{}, {'sha256':sha,'size':len(content),'side':side,'rows':batch.row_count,'source':quality},'原文件保留，映射尚未确认')
    return batch

def verified_original(batch):
    try:
        with (STORAGE/batch.object_key).open('rb') as source:
            content=source.read(MAX_BYTES+1)
        if hashlib.sha256(content).hexdigest()==batch.sha256:
            return content
    except OSError:
        pass
    raise HTTPException(409,'导入原件缺失或与预览时的哈希不符，请联系管理员恢复原件')

def comparable_mapping(config):
    controls=config.get('statement_controls')
    if not controls:
        return config
    pages=[{key:value for key,value in page.items() if key!='references' or value is not None}
           for page in controls['pages']]
    return {**config,'statement_controls':{**{key:value for key,value in controls.items()
            if key!='source_has_no_reference' or value},'pages':pages}}

def validate_import(db, batch, config, task, user):
    if batch.status != 'PREVIEWED':
        raise HTTPException(409,'该导入已经发布；原映射不可覆盖，请新建导入版本')
    published=db.scalars(select(ImportBatch).where(ImportBatch.task_id==task.id,ImportBatch.tenant_id==task.tenant_id,
            ImportBatch.side==batch.side,ImportBatch.sha256==batch.sha256,ImportBatch.status!='PREVIEWED'))
    if any(import_sheet(prior)==import_sheet(batch) for prior in published):
        raise HTTPException(409,'同侧的相同文件与物理工作表已发布；更改表头不能重复入账')
    fields = config.get('fields', {})
    if not config.get('confirmed') or config.get('direction') not in ['signed_in','signed_out','debit_in','credit_in']:
        raise HTTPException(422,'请显式确认字段映射与企业现金方向约定')
    if not fields.get('date') or (config['direction'].startswith('signed') and not fields.get('amount')):
        raise HTTPException(422,'必须映射日期、金额')
    if config['direction'] in ['debit_in','credit_in'] and not all(fields.get(x) for x in ['debit','credit']):
        raise HTTPException(422,'借贷模式必须映射借方和贷方列')
    if any(k not in {'date','amount','debit','credit','currency','account','reference','counterparty','description','group_key','balance'} for k in fields):
        raise HTTPException(422,'映射含非白名单字段')
    if any(v and v not in batch.columns for v in fields.values()):
        raise HTTPException(422,'映射包含不存在的原列')
    account = db.get(Account,task.account_id)
    visual=batch.quality.get('encoding')=='document-ai'
    document=visual or batch.filename.lower().endswith('.pdf')
    controls=config.get('statement_controls')
    if document and not controls:
        raise HTTPException(422,'文档发布须提交从原件独立核对的笔数、收支总额和期初期末余额')
    if controls and (not config.get('source_verified') or len(controls['source_note'].strip())<4):
        raise HTTPException(422,'须显式确认逐页核对原件，并填写控制数来源位置')
    if visual and suggest(batch.columns)['reference'] and not fields.get('reference'):
        raise HTTPException(422,'多模态提取已识别流水号列，不得省略流水号映射')
    if visual and bool(fields.get('reference'))==bool(controls.get('source_has_no_reference')):
        raise HTTPException(422,'多模态发布须核对流水号；原件无流水号列时须显式确认')
    original=verified_original(batch)
    if visual:
        try:
            extracted=(STORAGE/(batch.object_key+'.parsed.json')).read_bytes()
            if hashlib.sha256(extracted).hexdigest()!=batch.quality.get('extraction_sha256'):
                raise ValueError('hash mismatch')
            table=json.loads(extracted)
        except (OSError,ValueError):
            raise HTTPException(409,'提取凭证缺失或与预览时的哈希不符，请联系管理员恢复凭证') from None
    else:
        try:
            table=read_table(original,'.'+batch.filename.rsplit('.',1)[-1].lower(),import_sheet(batch),batch.header_row)
        except Exception:
            raise HTTPException(409,'原件无法按预览配置重新解析，未发布任何记录') from None
        expected=batch.quality.get('parse_sha256')
        if expected and parse_digest(table)!=expected:
            raise HTTPException(409,'重新解析的结果与原预览不一致，未发布任何记录')
    normalized = [normalize_row(row,config,task,account) for row in table['rows']]
    statement_check=None
    if controls:
        try:
            statement_check=verify_statement(normalized,table['rows'],controls,task.currency,
                                             table.get('pdf_pages',0),fields.get('balance',''),table.get('printed_statement_controls'),
                                             require_pages=visual,require_references=visual and bool(fields.get('reference')))
        except StatementMismatch as error:
            raise HTTPException(422,{'code':'STATEMENT_CONTROLS_FAILED','message':'原件控制数或明细校验未通过，未发布',
                                     'mismatches':error.mismatches}) from None
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
    existing = list(db.scalars(select(Transaction).where(Transaction.tenant_id == task.tenant_id,Transaction.task_id == task.id,Transaction.account_id == task.account_id,Transaction.side == batch.side)))
    ref_counts = Counter(r['reference'] for r in normalized if r['reference'])
    fp_counts = Counter(r['fingerprint'] for r in normalized)
    ext_refs = {r.reference for r in existing if r.reference}
    ext_fps = {r.fingerprint for r in existing}
    for row in normalized:
        if (row['reference'] and (ref_counts[row['reference']]>1 or row['reference'] in ext_refs)) or fp_counts[row['fingerprint']]>1 or row['fingerprint'] in ext_fps:
            row['issues'].append({'code':'DUPLICATE_SUSPECTED','field':'reference','message':'重复编号或业务指纹，保留原行并隔离，不能直接删除真实同额交易'})
            row['quality']='QUARANTINED'
        db.add(Transaction(tenant_id=task.tenant_id,task_id=task.id,import_id=batch.id,account_id=task.account_id,side=batch.side,**row))
    batch.mapping = {**{k:v for k,v in comparable_mapping(config).items() if k!='source_verified' and v is not None},
                     'version':1,'confirmed_by':user.id,'confirmed_at':now()}
    if statement_check:
        batch.mapping={**batch.mapping,'source_verified':True}
        batch.quality={**batch.quality,'statement_check':{**statement_check,'source_sha256':batch.sha256}}
    if visual:
        batch.mapping={**batch.mapping,'source_verified':True,'extraction_sha256':batch.quality['extraction_sha256'],'document_model':batch.quality['document_model']}
    batch.row_count=len(normalized)
    batch.valid_count=sum(r['quality']=='VALID' for r in normalized)
    issue_counts=Counter(i['code'] for r in normalized for i in r['issues'])
    batch.status='READY' if batch.valid_count==batch.row_count else 'PARTIAL_REJECTED'
    batch.quality={**batch.quality,'issues':dict(issue_counts),'valid':batch.valid_count,'quarantined':batch.row_count-batch.valid_count,'excluded':0}
    task.status='READY'; task.version+=1
    invalidate_reports(db,task.id)
    audit(db,user,'IMPORT_PUBLISH',batch.id,{'status':'PREVIEWED'},{'status':batch.status,'mapping_version':1,'counts':batch.quality},'用户确认映射和方向后全量验证')
    return batch

def confirm_batches(db, task, body, user):
    require_open_period(db,task)
    batches=[]; sides=set()
    for selection in body.selections:
        batch=db.get(ImportBatch,selection.batch_id)
        if not batch or batch.task_id!=task.id or batch.tenant_id!=task.tenant_id:raise HTTPException(404,'导入批次不属于当前任务')
        if batch.side in sides:raise HTTPException(422,'每侧只能选一个预览批次')
        sides.add(batch.side)
        config=selection.mapping.model_dump()
        if config['default_account']=='__TASK_ACCOUNT_CONFIRMED__':config['default_account']=db.get(Account,task.account_id).number
        if batch.status!='PREVIEWED':
            expected={k:v for k,v in comparable_mapping(config).items() if k!='source_verified' or batch.mapping.get('source_verified')}
            saved=comparable_mapping(batch.mapping)
            if not all(saved.get(k)==v for k,v in expected.items()):raise HTTPException(409,'此批次已按不同口径导入，请刷新后核对')
        else:
            if task.current_run:raise HTTPException(409,'输入已冻结，请创建新任务版本')
            validate_import(db,batch,config,task,user)
        batches.append(batch)
    return batches

def reserve(db, match):
    from .carry import used_transactions
    used=used_transactions(db,match.task_id)
    if any(used[i] for i in match.bank_ids+match.book_ids):raise HTTPException(409,'交易已被跨期清算占用')
    for transaction_id in match.bank_ids+match.book_ids:
        if db.scalar(select(MatchMember).where(MatchMember.transaction_id==transaction_id,MatchMember.active==True)):
            raise HTTPException(409,'交易已被其他匹配占用，请刷新')
        db.add(MatchMember(tenant_id=match.tenant_id,match_id=match.id,transaction_id=transaction_id))
    db.flush()

def execute_run(db, task, user, single_side_declaration='UNSUBMITTED', single_side_reason=''):
    require_open_period(db,task)
    start=monotonic()
    txs=rows_for_task(db,task)
    sides={side:any(t.side==side and t.quality=='VALID' for t in txs) for side in ['BANK','BOOK']}
    if not any(sides.values()):
        raise HTTPException(422,'至少一侧必须有一条已确认映射的有效记录')
    # A genuinely zero-activity side is a legitimate single-sided investigation;
    # it must not be reported as a completed two-sided reconciliation.
    missing_sides=[side for side,ok in sides.items() if not ok]
    if missing_sides and single_side_declaration=='CONFIRMED_ZERO' and len(single_side_reason.strip())<8:
        raise HTTPException(422,'声明另一侧已证实零发生额时，必须填写至少8字的来源与核验依据')
    # Preserve approvals. All unapproved proposals/automatic results are versioned, never deleted.
    from .carry import used_transactions
    pinned=set(used_transactions(db,task.id))
    for m in active_matches(db,task):
        if m.status in ['APPROVED','REVERSAL_PENDING']:
            pinned.update(m.bank_ids+m.book_ids)
        else:
            m.active=False
            db.execute(update(MatchMember).where(MatchMember.match_id==m.id).values(active=False))
    for c in db.scalars(select(Case).where(Case.task_id==task.id,Case.active==True)):
        if c.status != 'CLOSED': c.active=False
        elif c.resolution != 'MATCH_APPROVED': pinned.update(c.transaction_ids)
    db.flush()
    result=run_matching([t for t in txs if t.id not in pinned],task.rule_config)
    result['diagnostics']['single_side']=missing_sides
    result['diagnostics']['single_side_declaration']=single_side_declaration if missing_sides else ''
    result['diagnostics']['single_side_reason']=single_side_reason if missing_sides else ''
    result['diagnostics']['mode']='SINGLE_SIDE_INVESTIGATION' if missing_sides else 'BILATERAL_MATCH'
    run=Run(id=uid(),tenant_id=task.tenant_id,task_id=task.id,creator_id=user.id,
            rule_version=ENGINE_VERSION+':'+digest(task.rule_config)[:12],input_hash=digest([(t.id,t.quality,t.fingerprint) for t in sorted(txs,key=lambda x:x.id)]),
            diagnostics=result['diagnostics'])
    db.add(run); db.flush()
    occupied=set(pinned)
    occupied.update(db.scalars(select(MatchMember.transaction_id).join(Match,Match.id==MatchMember.match_id)
                   .where(Match.task_id==task.id,MatchMember.active==True)))
    for row in result['matches']:
        m=Match(id=uid(),tenant_id=task.tenant_id,task_id=task.id,run_id=run.id,creator_id=user.id,**row)
        db.add(m)
        if m.status=='DETERMINISTIC':
            members=m.bank_ids+m.book_ids
            if len(members)!=len(set(members)) or occupied.intersection(members):
                raise HTTPException(409,'确定性匹配的交易已被其他匹配或跨期清算占用')
            occupied.update(members)
            db.add_all(MatchMember(tenant_id=task.tenant_id,match_id=m.id,transaction_id=member) for member in members)
    for row in result['cases']:
        db.add(Case(tenant_id=task.tenant_id,task_id=task.id,run_id=run.id,**row))
    run.elapsed_ms=round((monotonic()-start)*1000)
    task.current_run=run.id;task.status='REVIEW';task.version+=1
    invalidate_reports(db,task.id)
    audit(db,user,'MATCH_RUN',run.id,{}, {'input_hash':run.input_hash,'rule_version':run.rule_version,'match_groups':len(result['matches']),'elapsed_ms':run.elapsed_ms,
        'single_side':result['diagnostics']['single_side'],'single_side_declaration':result['diagnostics']['single_side_declaration']},
        single_side_reason if missing_sides else '执行确定性优先匹配；组合/模糊仅建议')
    return run

def serialize_tx(t, detail=False):
    data={k:getattr(t,k) for k in ['id','task_id','import_id','side','row_no','date','currency','reference','counterparty','description','group_key','quality','issues','created_at']}
    data['amount']=amount_text(t.amount,t.currency)
    if detail: data['raw']=t.raw
    return data

def serialize_match(db,m):
    return {**{k:getattr(m,k) for k in ['id','task_id','run_id','bank_ids','book_ids','method','score','evidence','status','creator_id','reviewer_id','version','active','created_at']},
            'bank':[serialize_tx(db.get(Transaction,i)) for i in m.bank_ids],
            'book':[serialize_tx(db.get(Transaction,i)) for i in m.book_ids]}

def case_event(db,c,user,action,before,reason):
    db.add(CaseEvent(tenant_id=c.tenant_id,case_id=c.id,actor_id=user.id,action=action,before=before,after=c.status,reason=reason))
    audit(db,user,action,c.id,{'status':before},{'status':c.status,'version':c.version},reason)

def match_guard(db,task,bank_ids,book_ids):
    if not bank_ids or not book_ids or len(bank_ids)>3 or len(book_ids)>3 or (len(bank_ids)>1 and len(book_ids)>1):
        raise HTTPException(422,'仅允许1:1、最多3笔的1:N或N:1')
    if len(set(bank_ids+book_ids))!=len(bank_ids+book_ids):
        raise HTTPException(422,'交易ID不能重复')
    all_rows={t.id:t for t in rows_for_task(db,task) if t.quality=='VALID'}
    if any(i not in all_rows for i in bank_ids+book_ids):
        raise HTTPException(422,'交易不在有效且授权的当前任务中')
    from .carry import used_transactions
    used=used_transactions(db,task.id)
    if any(used[i] for i in bank_ids+book_ids):raise HTTPException(409,'交易已被跨期清算占用')
    bs,ls=[all_rows[i] for i in bank_ids],[all_rows[i] for i in book_ids]
    if any(t.side!='BANK' for t in bs) or any(t.side!='BOOK' for t in ls):
        raise HTTPException(422,'交易侧不正确')
    if any(not same_scope(b,l) or delta_days(b,l)>task.rule_config['date_days'] for b in bs for l in ls):
        raise HTTPException(422,'账户/币种/方向/日期硬约束不满足')
    residual=sum(t.amount for t in bs)-sum(t.amount for t in ls)
    if residual:
        raise HTTPException(422,'人工匹配金额必须相等；差额应先建立调整证据，不得抹平')
    return bs,ls

def balance_control(db, task, txs):
    result={}
    for side in ['BANK','BOOK']:
        values=task.balances.get(side,{})
        opening,closing=values.get('opening'),values.get('closing')
        rows=[t for t in txs if t.side==side and t.quality!='EXCLUDED']
        if opening is None or closing is None:
            result[side]={'status':'UNVERIFIED','message':'未提供两端余额，不能验证完整性'};continue
        expected=money(opening,task.currency)+sum(t.amount for t in rows if t.currency==task.currency)
        gap=money(closing,task.currency)-expected
        unresolved=any(t.quality=='QUARANTINED' for t in rows)
        status='UNVERIFIED' if unresolved else ('PASS' if gap==0 else 'FAIL')
        result[side]={'status':status,'opening':opening,'closing':closing,'expected_closing':amount_text(expected,task.currency),'gap':amount_text(gap,task.currency),
                      'message':'存在隔离数据，余额尚未验证' if unresolved else '余额勾稽通过（不代表无漏行）' if not gap else '期初+收入−支出与期末不一致'}
    return result

def program_determined(m):
    """Whether the matching engine, not a human approval, first determined this group.

    New matches store the classification in evidence. Legacy rows are inferred
    conservatively: an L1/L2 group that carried an auto-blocker was a pending
    suggestion and must not be counted as automatic after manual approval.
    """
    evidence=m.evidence or {}
    if 'program_determined' in evidence:return bool(evidence['program_determined'])
    return m.method in ['L1_REFERENCE','L2_EXACT'] and not evidence.get('auto_blockers')

def task_summary(db,task,rows=None):
    txs=rows_for_task(db,task) if rows is None else rows; matches=active_matches(db,task)
    cases=list(db.scalars(select(Case).where(Case.task_id==task.id,Case.active==True)))
    certain={i for m in matches if m.status in ['DETERMINISTIC','APPROVED','REVERSAL_PENDING'] for i in m.bank_ids+m.book_ids}
    from .carry import cleared_ids
    certain.update(cleared_ids(db,task))
    automatic={i for m in matches if program_determined(m) and m.status in ['DETERMINISTIC','APPROVED','REVERSAL_PENDING'] for i in m.bank_ids+m.book_ids}
    suggestions={i for m in matches if m.status=='PENDING' for i in m.bank_ids+m.book_ids}
    summary={}
    for side in ['BANK','BOOK']:
        rows=[t for t in txs if t.side==side];valid=[t for t in rows if t.quality=='VALID']
        total=len(rows)
        summary[side]={'raw':total,'valid':len(valid),'quarantined':sum(t.quality=='QUARANTINED' for t in rows),'excluded':sum(t.quality=='EXCLUDED' for t in rows),
                       'matched':sum(t.id in certain for t in valid),'suggested':sum(t.id in suggestions for t in valid),
                       'unmatched':sum(t.id not in certain for t in valid),'automatic':sum(t.id in automatic for t in valid),
                       'coverage':round(100*sum(t.id in automatic for t in valid)/len(valid),1) if valid else 0,
                       'matched_amount':amount_text(sum(abs(t.amount) for t in valid if t.id in certain),task.currency),
                       'unmatched_amount':amount_text(sum(abs(t.amount) for t in valid if t.id not in certain),task.currency),
                       'valid_net_amount':amount_text(sum(t.amount for t in valid),task.currency)}
    return {'sides':summary,'match_groups':len(matches),'pending_matches':sum(m.status in ['PENDING','REVERSAL_PENDING'] for m in matches),
            'open_cases':sum(c.status!='CLOSED' for c in cases),'case_types':dict(Counter(c.kind for c in cases)),
            'balance_control':balance_control(db,task,txs),'data_as_of':now()}

def balance_adjustment(db,task,txs):
    control=balance_control(db,task,txs)
    if any(control[s]['status']!='PASS' for s in ['BANK','BOOK']):
        return {'status':'UNVERIFIED','message':'两侧余额完整性未通过，仅提供逐笔报告'}
    cases=list(db.scalars(select(Case).where(Case.task_id==task.id,Case.active==True)))
    total=defaultdict(int);by_id={t.id:t for t in txs}
    for c in cases:
        if c.status=='CLOSED' and c.resolution in ['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']:
            total[c.resolution]+=sum(abs(by_id[i].amount) for i in c.transaction_ids)
    from .carry import carry_totals
    for kind,n in carry_totals(db,task).items():total[kind]+=n
    adjusted_bank=money(task.balances['BANK']['closing'],task.currency)+total['BOOK_RECEIPT']-total['BOOK_PAYMENT']
    adjusted_book=money(task.balances['BOOK']['closing'],task.currency)+total['BANK_RECEIPT']-total['BANK_PAYMENT']
    opening_gap=money(task.balances['BANK']['opening'],task.currency)-money(task.balances['BOOK']['opening'],task.currency)
    return {'status':'CALCULATED','message':'仅按人工审核的四类未达项调节；差额为零不证明逐笔全部正确',
            'opening_gap':amount_text(opening_gap,task.currency),'adjusted_bank':amount_text(adjusted_bank,task.currency),
            'adjusted_book':amount_text(adjusted_book,task.currency),'gap':amount_text(adjusted_bank-adjusted_book,task.currency),
            'items':{k:amount_text(total[k],task.currency) for k in ['BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT']}}

def make_snapshot(db,task):
    from .controls import evidence_snapshot
    from .assurance import assurance_snapshot
    from .carry import carry_snapshot
    from .db import AIInvocation
    summary=task_summary(db,task);txs=rows_for_task(db,task)
    run=db.get(Run,task.current_run) if task.current_run else None
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.task_id==task.id)))
    cases=list(db.scalars(select(Case).where(Case.task_id==task.id,Case.active==True)))
    case_ids=[c.id for c in cases]
    match_history=list(db.scalars(select(Match).where(Match.task_id==task.id).order_by(Match.created_at,Match.id)))
    match_ids=[m.id for m in match_history]
    review_history=[{k:getattr(e,k) for k in ['id','actor_id','action','before','after','reason','created_at']}
                    for e in db.scalars(select(CaseEvent).where(CaseEvent.case_id.in_(case_ids)).order_by(CaseEvent.created_at,CaseEvent.id))]
    match_reviews=[{k:getattr(e,k) for k in ['id','actor_id','object_id','action','before','after','reason','created_at']}
                   for e in db.scalars(select(Audit).where(Audit.object_id.in_(match_ids)).order_by(Audit.created_at,Audit.id))]
    return {**evidence_snapshot(db,task),**assurance_snapshot(db,task),**carry_snapshot(db,task),'task':{k:getattr(task,k) for k in ['id','name','currency','start_date','end_date','account_id','status','version']},
            'summary':summary,'input_hash':run.input_hash if run else '', 'rule_version':run.rule_version if run else '',
            'files':[{'id':b.id,'sha256':b.sha256,'filename':b.filename,'mapping':b.mapping,'status':b.status} for b in batches],
            'transactions':[serialize_tx(t,True) for t in txs], 'matches':[serialize_match(db,m) for m in active_matches(db,task)],
            'cases':[{k:getattr(c,k) for k in ['id','kind','transaction_ids','candidates','status','resolution','note','owner_id','due_date','submitter_id','version','analysis']} for c in cases],
            'ai_invocations':[{k:getattr(a,k) for k in ['id','object_id','provider','model','status','elapsed_ms','evidence_ids','error_code','created_at']} for a in db.scalars(select(AIInvocation).where(AIInvocation.object_id.in_(case_ids),AIInvocation.tenant_id==task.tenant_id).order_by(AIInvocation.created_at,AIInvocation.id))],
            'review_history':review_history,'match_reviews':match_reviews,
            'match_history':[{k:getattr(m,k) for k in ['id','run_id','bank_ids','book_ids','method','status','active','version','creator_id','reviewer_id','evidence']} for m in match_history],
            'balance_adjustment':balance_adjustment(db,task,txs),'generated_at':now(),
            'disclaimer':('合成演示。' if os.getenv('BANKMIND_DEMO','true').lower()=='true' else '')+
              '系统建议、程序确定匹配、人工确认与最终签署分别列示；未完成复核不能作无保留对账结论。'}
