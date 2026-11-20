"""Immutable report signing envelope and portable exit artifacts; never alters snapshots."""
import base64,hashlib,html,io,json,os,zipfile
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import select
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from .db import STORAGE,ReportSignature,Audit,ImportBatch,EvidenceAttachment,Report,now,uid
from .security import digest

def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
def keypair():
    configured=os.getenv('BANKMIND_SIGNING_KEY_FILE')
    path=Path(configured) if configured else STORAGE/'keys'/'report-ed25519.key'
    if not configured and not path.exists():
        path.parent.mkdir(exist_ok=True)
        key=Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        try:
            fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(key)
        except FileExistsError:pass
    key=Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    return key,base64.b64encode(public).decode(),hashlib.sha256(public).hexdigest()
def signed(body):
    key,public,fingerprint=keypair()
    return {'algorithm':'Ed25519','key_id':fingerprint,'public_key':public,'body':body,'signature':base64.b64encode(key.sign(canonical(body))).decode()}
def record_signature(db,r,t,user,reason):
    db.flush()
    event=db.scalar(select(Audit).where(Audit.object_id==r.id,Audit.action=='REPORT_SIGN').order_by(Audit.created_at.desc(),Audit.id.desc()))
    if not event:raise HTTPException(409,'签署审计事件缺失，不能提交签署')
    fields=['id','tenant_id','actor_id','action','object_id','before','after','reason','prev_hash','event_hash','created_at']
    body={'schema':'bankmind-signature-v1','report_id':r.id,'snapshot_sha256':r.sha256,'status_at_sign':'SIGNED','report_version':r.version,
      'signer':{k:getattr(user,k) for k in ['id','tenant_id','name','username','role']},'signed_at':event.created_at,'opinion':reason,
      'scope':{k:getattr(t,k) for k in ['id','account_id','currency','start_date','end_date']},'audit_event':{k:getattr(event,k) for k in fields},
      'assurance':'本地原型密钥签名；银行真实性、组织授权及生产密钥托管须另行验证'}
    db.add(ReportSignature(id=uid(),tenant_id=t.tenant_id,task_id=t.id,report_id=r.id,envelope=signed(body)))
def signature_for(db,r):
    sig=db.scalar(select(ReportSignature).where(ReportSignature.report_id==r.id,ReportSignature.tenant_id==r.tenant_id))
    if sig:return sig.envelope
    # Historical snapshots remain byte-for-byte unchanged. Do not infer a historical identity from today's directory.
    events=list(db.scalars(select(Audit).where(Audit.object_id==r.id,Audit.action=='REPORT_SIGN').order_by(Audit.created_at,Audit.id)))
    return {'schema':'bankmind-legacy-evidence-v1','state':'LEGACY_NO_CAPTURED_SIGNATURE' if events or r.signer_id else 'UNSIGNED_DRAFT',
      'report_id':r.id,'snapshot_sha256':r.sha256,'historical_actor_id':r.signer_id or '',
      'audit_events':[{k:getattr(e,k) for k in ['id','actor_id','action','object_id','before','after','reason','event_hash','prev_hash','created_at']} for e in events],
      'limitation':'历史记录未捕获签署时身份快照与密码签名；不补造姓名、原签名或签署时间'}
def append_sheets(wb,db,r):
    ws=wb.create_sheet('余额调节表');ws.append(['项目','精确十进制文本（避免Excel十五位限制）'])
    a=r.snapshot.get('balance_adjustment',{})
    for k in ['status','opening_gap','adjusted_bank','adjusted_book','gap','message']:ws.append([k,str(a.get(k,''))])
    for k,v in a.get('items',{}).items():ws.append([k,str(v)])
    ws=wb.create_sheet('签署证据');env=signature_for(db,r)
    ws.append(['签署封套（完整内容见退出ZIP）','值'])
    ws.append(['当前报告状态',r.status]);ws.append(['快照SHA256',r.sha256]);ws.append(['金额存储说明','金额按精确十进制文本输出，请勿自行转换为二进制浮点数'])
    body=env.get('body',{})
    for k in ['schema','signed_at','opinion','report_version','status_at_sign']:ws.append([k,str(body.get(k,env.get(k,'')))])
    for k,v in body.get('signer',{}).items():ws.append(['signer.'+k,str(v)])
    for k in ['algorithm','key_id','public_key','signature','state','limitation']:ws.append([k,str(env.get(k,''))])
    audit=body.get('audit_event',{})
    for k,v in audit.items():ws.append(['audit.'+k,json.dumps(v,ensure_ascii=False) if isinstance(v,dict) else str(v)])
    carry=wb.create_sheet('跨期逐项证据');carry.append(['类型','对象ID','状态','版本','原行ID / 来源报告','金额最小单位 / 范围'])
    for b in r.snapshot.get('outstanding_control',{}).get('bridges',[]):
        for item in b['scope'].get('items',[]):carry.append(['期初桥接',b['id'],b['status'],b['version'],item['origin']['report_id'],item['remaining_minor']])
    for c in r.snapshot.get('outstanding_control',{}).get('clearings',[]):
        for a in c['allocations']:carry.append(['清算分配',c['id'],c['status'],c['version'],a['transaction_id'],a['amount_minor']])
    # Prevent formula execution for all user-controlled fields in auxiliary sheets.
    for sheet in [wb['余额调节表'],ws,carry]:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
        sheet.freeze_panes='A2';sheet.column_dimensions['A'].width=30;sheet.column_dimensions['B'].width=90

def exit_package(db,r,t,rows,csv_data):
    artifacts={'transactions.csv':csv_data,'snapshot.json':json.dumps(r.snapshot,ensure_ascii=False,indent=2).encode('utf-8'),
      'signature-envelope.json':json.dumps(signature_for(db,r),ensure_ascii=False,indent=2).encode('utf-8')}
    def add_report_files(report,prefix=''):
        for source in report.snapshot['files']+report.snapshot.get('attachments',[]):
            is_file='mapping' in source;model=ImportBatch if is_file else EvidenceAttachment
            obj=db.get(model,source['id'])
            if not obj or obj.tenant_id!=r.tenant_id or obj.task_id!=report.task_id:raise HTTPException(409,'退出证据归属与快照不一致')
            p=(STORAGE/obj.object_key).resolve()
            if not p.is_relative_to(STORAGE.resolve()) or not p.is_file():raise HTTPException(409,'退出证据文件缺失')
            data=p.read_bytes()
            if hashlib.sha256(data).hexdigest()!=source['sha256']:raise HTTPException(409,'退出证据哈希与快照不一致')
            name=prefix+('sources/' if is_file else 'evidence/')+obj.id+Path(obj.filename).suffix.lower();artifacts[name]=data
    add_report_files(r)
    prior_reports={};pending=[r];visited={r.id}
    while pending:
        current=pending.pop()
        refs={}
        for bridge in current.snapshot.get('outstanding_control',{}).get('bridges',[]):
            scope=bridge['scope'];refs[scope['prior_report_id']]=scope['prior_snapshot_sha256']
            for item in scope.get('items',[]):refs[item['origin']['report_id']]=item['origin']['snapshot_sha256']
        for rid,expected_sha in refs.items():
            prior=db.get(Report,rid)
            if not prior or prior.tenant_id!=r.tenant_id or prior.snapshot['task']['account_id']!=t.account_id or prior.snapshot['task']['currency']!=t.currency or prior.sha256!=expected_sha:raise HTTPException(409,'跨期来源报告归属或指纹不一致')
            if prior.snapshot['task']['end_date']>=current.snapshot['task']['start_date']:raise HTTPException(409,'跨期证据包含非以前期间或循环依赖')
            if rid in visited:continue
            visited.add(rid)
            if len(visited)>120:raise HTTPException(422,'退出包跨期依赖超过120期，请分期归档')
            prefix='carry/'+rid+'/'
            artifacts[prefix+'snapshot.json']=json.dumps(prior.snapshot,ensure_ascii=False,indent=2).encode('utf-8')
            artifacts[prefix+'signature-envelope.json']=json.dumps(signature_for(db,prior),ensure_ascii=False,indent=2).encode('utf-8')
            add_report_files(prior,prefix)
            prior_reports[rid]={'prefix':prefix,'snapshot_hash':prior.sha256,'report_status':prior.status};pending.append(prior)
            if sum(len(data) for data in artifacts.values())>256*1024*1024:raise HTTPException(422,'退出证据超过256MB，请分期归档')
    table=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)
    artifacts['evidence.html']=('<!doctype html><meta charset="utf-8"><title>BankMind证据索引</title><h1>逐笔对账证据</h1><p>'+html.escape(r.snapshot['disclaimer'])+'</p><p>签署者、意见及时间见 signature-envelope.json；余额调节见 snapshot.json。校验需独立保存公钥指纹，SHA不证明银行真实性。</p><table border="1">'+table+'</table>').encode('utf-8')
    artifacts['VERIFY.txt']='使用项目 scripts/verify_exit.py 包路径 --trusted-key-id 独立保存的公钥指纹。未提供可信指纹时只能验证包内一致性，不能确认组织来源。历史报告缺少签署时身份快照会明确报告证据缺口。原始快照不回写。'.encode('utf-8')
    manifest={'schema':'bankmind-exit-v2','exported_at':now(),'report_id':r.id,'report_status':r.status,'snapshot_hash':r.sha256,
      'prior_reports':prior_reports,'input_files':r.snapshot['files'],'rule_version':r.snapshot['rule_version'],
      'artifacts':{k:hashlib.sha256(v).hexdigest() for k,v in artifacts.items()}}
    artifacts['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    artifacts['manifest-signature.json']=json.dumps(signed(manifest),ensure_ascii=False,indent=2).encode('utf-8')
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in artifacts.items():z.writestr(name,data)
    return buf.getvalue()
