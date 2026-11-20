import hashlib
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Path as PathParam, UploadFile
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import Task, User, WorkChunk, WorkJob, get_db, lock
from .periods import require_open_period
from .schemas import MatchJobInput, PreviewJobInput, PublishJobInput
from .security import current_user, digest, role, scoped
from .work import CHUNK_BYTES, LOCAL_EXTENSIONS, ensure_supported, release_chunks

router = APIRouter(prefix='/api/tasks/{task_id}/jobs',tags=['恢复作业'])
USER = Depends(current_user)
DB = Depends(get_db)
RequestJob = Annotated[PreviewJobInput | PublishJobInput | MatchJobInput, Field(discriminator='kind')]


def job_json(job):
    return {'id':job.id,'kind':job.kind,'status':job.status,'created_at':job.created_at,'attempts':job.attempts,
            'received_bytes':job.uploaded,'total_bytes':job.payload.get('size'),'chunk_bytes':CHUNK_BYTES,
            'result':job.result,'error':job.error}


def access(db, user, task_id, job_id=None, write=False):
    ensure_supported(db)
    if write:
        lock(db)
    role(user,'Admin','Accountant')
    task = scoped(db,Task,task_id,user)
    if job_id is None:
        return task
    job = db.get(WorkJob,job_id)
    if not job or job.task_id!=task.id or job.tenant_id!=task.tenant_id or job.actor_id!=user.id:
        raise HTTPException(404,'作业不在授权范围内')
    return job


@router.post('',status_code=202)
def create_job(task_id:str,body:RequestJob,user:User=USER,db:Session=DB):
    task = access(db,user,task_id,write=True)
    payload = body.model_dump(exclude={'idempotency_key','kind'})
    if isinstance(body,MatchJobInput) and not body.require_both:
        payload.pop('require_both')  # Preserve signatures of existing requests.
    signature = digest([body.kind,payload])
    old = db.scalar(select(WorkJob).where(WorkJob.task_id==task.id,WorkJob.tenant_id==task.tenant_id,
                    WorkJob.actor_id==user.id,WorkJob.request_key==body.idempotency_key))
    if old:
        if old.request_hash!=signature:
            raise HTTPException(409,'相同幂等键不能用于不同请求')
        return job_json(old)
    require_open_period(db,task)
    if isinstance(body,PreviewJobInput):
        filename = Path(body.filename).name
        ext = Path(filename).suffix.lower()
        if filename!=body.filename or '/' in filename or '\\' in filename or ext not in LOCAL_EXTENSIONS:
            raise HTTPException(422,'续传仅支持本地 CSV、XLS、XLSX、文本 PDF，请提供无路径文件名')
        if (body.sheet and ext not in ('.xls','.xlsx')) or (ext=='.pdf' and body.header_row!=1):
            raise HTTPException(422,'文件类型不接受指定工作表或表头')
        if task.current_run:
            raise HTTPException(409,'输入已冻结，不能新增上传')
    elif body.version!=task.version:
        raise HTTPException(409,'任务版本已变化，请刷新后核对')
    active = db.scalar(select(func.count()).select_from(WorkJob).where(WorkJob.tenant_id==task.tenant_id).where(
                or_(WorkJob.status.in_(['UPLOADING','QUEUED']), (WorkJob.status=='FAILED') & (WorkJob.kind=='PREVIEW')
                    & WorkJob.error['retryable'].as_boolean())))
    if active>=30:
        raise HTTPException(429,'有过多未处理作业；请先处理或取消旧作业')
    job = WorkJob(tenant_id=task.tenant_id,task_id=task.id,actor_id=user.id,request_key=body.idempotency_key,
                  request_hash=signature,kind=body.kind,status='UPLOADING' if isinstance(body,PreviewJobInput) else 'QUEUED',payload=payload)
    db.add(job)
    db.commit()
    return job_json(job)


@router.get('')
def job_list(task_id:str,user:User=USER,db:Session=DB):
    task = access(db,user,task_id)
    jobs = db.scalars(select(WorkJob).where(WorkJob.task_id==task.id,WorkJob.tenant_id==task.tenant_id,
                     WorkJob.actor_id==user.id).order_by(WorkJob.created_at.desc(),WorkJob.id.desc()).limit(100))
    return [job_json(job) for job in jobs]


@router.get('/{job_id}')
def job_status(task_id:str,job_id:str,user:User=USER,db:Session=DB):
    return job_json(access(db,user,task_id,job_id))


@router.post('/{job_id}/chunks/{offset}')
def put_chunk(task_id:str,job_id:str,offset:Annotated[int,PathParam(ge=0)],file:UploadFile=File(...),user:User=USER,db:Session=DB):
    content = file.file.read(CHUNK_BYTES+1)
    if not 1<=len(content)<=CHUNK_BYTES:
        raise HTTPException(422,'分块长度必须在 1 字节至 1 MiB 之间')
    job = access(db,user,task_id,job_id,write=True)
    if job.kind!='PREVIEW':
        raise HTTPException(409,'此作业不接收文件分块')
    sha = hashlib.sha256(content).hexdigest()
    if offset<job.uploaded:
        original = db.get(WorkChunk,(job.id,offset))
        if original and original.size==len(content) and original.sha256==sha:
            return job_json(job)
        raise HTTPException(409,'重传内容与已保存分块不符')
    if job.status!='UPLOADING' or offset!=job.uploaded:
        raise HTTPException(409,'请按作业状态返回的已接收字节数继续上传')
    require_open_period(db,db.get(Task,task_id))
    if len(content)!=min(CHUNK_BYTES,job.payload['size']-offset):
        raise HTTPException(422,'请使用 1 MiB 固定分块，只有最后一块可以更短')
    db.add(WorkChunk(job_id=job.id,offset=offset,size=len(content),sha256=sha,content=content))
    job.uploaded += len(content)
    db.commit()
    return job_json(job)


@router.post('/{job_id}/{action}')
def transition(task_id:str,job_id:str,action:Literal['complete','retry','cancel'],user:User=USER,db:Session=DB):
    job = access(db,user,task_id,job_id,write=True)
    if action=='complete':
        if job.kind!='PREVIEW' or job.status=='CANCELLED':
            raise HTTPException(409,'此作业不接受上传完成通知')
        if job.status=='UPLOADING':
            if job.uploaded!=job.payload['size']:
                raise HTTPException(409,'文件尚未上传完整')
            job.status = 'QUEUED'
    elif action=='retry':
        if job.status=='FAILED' and job.error.get('retryable'):
            job.status = 'QUEUED'
            job.error = None
        elif job.status not in ('QUEUED','SUCCEEDED'):
            raise HTTPException(409,'当前作业不可重试，请查看失败原因')
    else:
        if job.status=='SUCCEEDED':
            raise HTTPException(409,'作业已提交，不能取消或回滚财务结果')
        job.status = 'CANCELLED'
        job.error = None
        if job.kind=='PREVIEW':
            release_chunks(db,job)
    db.commit()
    return job_json(job)
