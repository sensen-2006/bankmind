"""Read-only access to the supplied original synthetic upload pack.
Private answers and third-party reference material are never exposed by this API.
"""
import json,hashlib,io
from pathlib import Path
from typing import Literal
from fastapi import Depends,HTTPException
from fastapi.responses import FileResponse,StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import *
from .schemas import Input,TaskInput
from .security import current_user,scoped,role,audit,mask
from .parsing import read_table

Dataset=Literal['demo_small','standard_2000','benchmark_10000']
PACK=ROOT/'bankmind_data_pack'

class LibraryPrepare(Input):
    dataset: Dataset
    scope_index: int
    account_id: str

def read_manifest(dataset):
    path=PACK/'07_app_ready'/'upload_pairs'/dataset/'task_manifest.json'
    if not path.is_file():raise HTTPException(404,'本地资源库尚未提供此档位')
    return json.loads(path.read_text(encoding='utf-8-sig'))

def get_scope(dataset,index):
    scopes=read_manifest(dataset)
    if index<0 or index>=len(scopes):raise HTTPException(404,'样本账户范围不存在')
    return scopes[index]

def input_path(scope,side,format='csv'):
    if side not in ['BANK','BOOK']:raise HTTPException(422,'无效侧别')
    relative=scope.get('bank_file' if side=='BANK' else 'book_file','')
    if not relative:raise HTTPException(404,'此范围没有该侧文件；单边事实用于调查，不能编造另一侧')
    path=(PACK/relative).with_suffix('.'+format).resolve()
    if not path.is_relative_to((PACK/'07_app_ready').resolve()) or not path.is_file():raise HTTPException(404,'公开上传样本缺失或路径不允许')
    return path

def install_data_library(app):
    from .main import lock,task_json
    USER=Depends(current_user);DB=Depends(get_db)

    @app.get('/api/data-library')
    def library(user:User=USER,db:Session=DB):
        accounts=list(db.scalars(select(Account).where(Account.tenant_id==user.tenant_id,Account.id.in_(user.account_ids))))
        datasets=[]
        for name in ['demo_small','standard_2000','benchmark_10000']:
            try:scopes=read_manifest(name)
            except HTTPException:continue
            datasets.append({'id':name,'synthetic':True,'bank_rows':sum(s['bank_rows'] for s in scopes),'book_rows':sum(s['book_rows'] for s in scopes),
                'scopes':[{'index':i,'account_masked':mask(s['account']),'currency':s['currency'],'start_date':s['start_date'],'end_date':s['end_date'],'bank_rows':s['bank_rows'],'book_rows':s['book_rows'],
                    'authorized_account_ids':[a.id for a in accounts if a.number==s['account'] and a.currency==s['currency']]} for i,s in enumerate(scopes)]})
        return {'datasets':datasets,'boundary':'仅开放07_app_ready原创合成输入；私有真值、期后答案、引擎输出及第三方标准不进入产品匹配输入。','formats':['CSV','XLSX'],'mapping_confirmation_required':True}

    @app.get('/api/data-library/{dataset}/{scope_index}/{side}')
    def download_sample(dataset:Dataset,scope_index:int,side:Literal['BANK','BOOK'],format:Literal['csv','xlsx']='csv',user:User=USER,db:Session=DB):
        path=input_path(get_scope(dataset,scope_index),side)
        lock(db);audit(db,user,'SYNTHETIC_SAMPLE_DOWNLOAD',user.id,{}, {'dataset':dataset,'scope':scope_index,'side':side,'format':format},'下载原创合成输入，不包含私有答案');db.commit()
        name=f'synthetic-{dataset}-{scope_index}-{side}.{format}'
        if format=='csv':return FileResponse(path,filename=name,media_type='application/octet-stream')
        # Per-account sources are CSV; generate an equivalent text-preserving
        # workbook in memory. Do not copy the mixed-account top-level workbook.
        import openpyxl
        table=read_table(path.read_bytes(),'.csv');wb=openpyxl.Workbook();ws=wb.active;ws.title='交易明细'
        for values in [table['columns']]+[[r['raw'][c] for c in table['columns']] for r in table['rows']]:
            ws.append(values)
            for cell in ws[ws.max_row]:cell.data_type='s';cell.number_format='@'
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        buffer=io.BytesIO();wb.save(buffer);buffer.seek(0)
        return StreamingResponse(buffer,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="{name}"'})

    @app.post('/api/data-library/prepare')
    def prepare(body:LibraryPrepare,user:User=USER,db:Session=DB):
        role(user,'Admin','Accountant');lock(db);account=scoped(db,Account,body.account_id,user)
        scope=get_scope(body.dataset,body.scope_index)
        if account.number!=scope['account'] or account.currency!=scope['currency']:raise HTTPException(422,'账户或币种与样本清单不一致，不能用默认值掩盖冲突')
        # Parse both sources before creating business state. Neither mapping is published.
        sources=[]
        for side in ['BANK','BOOK']:
            path=input_path(scope,side);raw=path.read_bytes();table=read_table(raw,'.csv')
            if len(table['rows'])!=scope['bank_rows' if side=='BANK' else 'book_rows']:raise HTTPException(409,'资源库清单与文件行数不一致')
            sources.append((side,path,raw,table))
        validated=TaskInput(name=f'数据实验室 · {body.dataset} · {scope["currency"]}（合成）',account_id=account.id,currency=scope['currency'],start_date=scope['start_date'],end_date=scope['end_date'])
        task=Task(id=uid(),tenant_id=user.tenant_id,creator_id=user.id,**{**validated.model_dump(),'start_date':str(validated.start_date),'end_date':str(validated.end_date)})
        db.add(task);db.flush()
        for side,path,raw,table in sources:
            key='uploads/'+uid()+'.csv';(STORAGE/'uploads').mkdir(exist_ok=True);(STORAGE/key).write_bytes(raw)
            batch=ImportBatch(id=uid(),tenant_id=user.tenant_id,task_id=task.id,side=side,filename=f'synthetic-{body.dataset}-{side}.csv',sha256=hashlib.sha256(raw).hexdigest(),object_key=key,size=len(raw),sheet='',header_row=1,columns=table['columns'],preview=table['rows'][:20],row_count=len(table['rows']),quality={'encoding':table['encoding'],'synthetic':True,'library_dataset':body.dataset,'library_scope':body.scope_index,'source_path':str(path.relative_to(PACK))})
            db.add(batch)
            audit(db,user,'SYNTHETIC_IMPORT_PREVIEW',batch.id,{}, {'dataset':body.dataset,'side':side,'sha256':batch.sha256,'rows':batch.row_count},'仅创建预览；映射、方向、日期仍必须人工确认')
        audit(db,user,'SYNTHETIC_TASK_CREATE',task.id,{}, {'dataset':body.dataset,'scope':body.scope_index},'来自本地原创合成资源库，不读取私有答案')
        db.commit();return task_json(db,task)
