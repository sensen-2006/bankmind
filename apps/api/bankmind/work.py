import hashlib
import logging
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select, update

from .db import STORAGE, SessionLocal, Task, User, WorkChunk, WorkJob, init_db, lock
from .parsing import read_table
from .periods import require_open_period
from .schemas import ImportConfirm
from .security import role, scoped
from .services import confirm_batches, execute_run, find_import, rows_for_task, save_import

CHUNK_BYTES = 1024 * 1024
LOCAL_EXTENSIONS = {
    '.csv', '.xls', '.xlsx', '.pdf',
    '.html', '.htm', '.md', '.markdown',
    '.txt', '.tsv', '.dat',
    '.sta', '.mt940', '.940', '.xml', '.ofx', '.qfx',
}
logger = logging.getLogger(__name__)


def ensure_supported(db):
    if db.bind.dialect.name not in ('sqlite','postgresql'):
        raise HTTPException(503,'恢复队列支持 SQLite 或 PostgreSQL 工作库')


def release_chunks(db, job):
    db.execute(update(WorkChunk).where(WorkChunk.job_id==job.id).values(content=None))


def assemble(db, job):
    content = bytearray()
    for chunk in db.scalars(select(WorkChunk).where(WorkChunk.job_id==job.id).order_by(WorkChunk.offset)):
        if chunk.content is None or chunk.offset!=len(content) or len(chunk.content)!=chunk.size or hashlib.sha256(chunk.content).hexdigest()!=chunk.sha256:
            raise HTTPException(409,'上传分块缺失或损坏，请创建新上传')
        content.extend(chunk.content)
    if len(content)!=job.payload['size'] or hashlib.sha256(content).hexdigest()!=job.payload['sha256']:
        raise HTTPException(422,'完整文件的哈希或长度不一致，请创建新上传')
    return bytes(content)


def perform(db, job):
    user = db.get(User,job.actor_id)
    if not user or not user.enabled or user.tenant_id!=job.tenant_id:
        raise HTTPException(403,'提交人已停用或不再可用')
    role(user,'Admin','Accountant')
    task = scoped(db,Task,job.task_id,user)
    require_open_period(db,task)
    payload = job.payload
    if job.kind=='PREVIEW':
        content = assemble(db,job)
        old = find_import(db,task,payload['side'],payload['sha256'],payload['sheet'],payload['header_row'])
        if old:
            return {'batch_id':old.id,'reused':True}
        if task.current_run:
            raise HTTPException(409,'输入已冻结，未建立导入批次')
        ext = Path(payload['filename']).suffix.lower()
        try:
            table = read_table(content,ext,payload['sheet'],payload['header_row'])
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        batch = save_import(db,task,user,payload['filename'],payload['side'],payload['sha256'],payload['sheet'],
                            payload['header_row'],content,table,object_id=job.id)
        return {'batch_id':batch.id,'reused':False}
    if task.version!=payload['version']:
        raise HTTPException(409,'任务版本已变化，请刷新并核对后以新请求提交')
    if job.kind=='PUBLISH':
        batches = confirm_batches(db,task,ImportConfirm(selections=payload['selections']),user)
        return {'batch_ids':[batch.id for batch in batches]}
    if task.current_run and not payload['rerun']:
        raise HTTPException(409,'任务已匹配；重跑须使用当前版本并显式授权')
    if payload.get('require_both') and {row.side for row in rows_for_task(db,task) if row.quality=='VALID'}!={'BANK','BOOK'}:
        raise HTTPException(422,'快捷匹配需要两侧都有有效数据')
    run = execute_run(db,task,user,payload['single_side_declaration'],payload['reason'])
    return {'run_id':run.id,'elapsed_ms':run.elapsed_ms}


def process_one():
    with SessionLocal() as db:
        ensure_supported(db)
        if not db.scalar(select(WorkJob.id).where(WorkJob.status=='QUEUED').limit(1)):
            return False
        lock(db)
        job = db.scalar(select(WorkJob).where(WorkJob.status=='QUEUED').order_by(WorkJob.created_at,WorkJob.id).limit(1))
        if not job:
            return False
        job.attempts += 1
        try:
            with db.begin_nested():
                job.result = perform(db,job)
                job.status = 'SUCCEEDED'
                job.error = None
        except HTTPException as error:
            job.status = 'FAILED'
            job.error = {'status':error.status_code,'detail':error.detail,'retryable':False}
        except Exception as error:
            logger.error('Work job %s failed: %s',job.id,type(error).__name__)
            job.status = 'FAILED'
            job.error = {'status':500,'detail':'作业未提交。可排除故障后以原作业编号重试。','retryable':True}
        if job.kind=='PREVIEW' and not (job.error or {}).get('retryable'):
            release_chunks(db,job)
        db.commit()
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description='BankMind durable work queue')
    parser.add_argument('--once',action='store_true')
    options = parser.parse_args()
    init_db()
    if options.once:
        process_one()
        return
    from threading import Event
    consume(Event())


def consume(stop):
    heartbeat = STORAGE/'worker.heartbeat'
    while not stop.is_set():
        try:
            worked = process_one()
            heartbeat.touch()
            if not worked:
                stop.wait(1)
        except Exception as error:
            logger.error('Work loop interrupted: %s',type(error).__name__)
            stop.wait(3)


if __name__=='__main__':
    main()
