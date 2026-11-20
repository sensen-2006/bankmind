import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import time
from urllib.parse import urlsplit

from sqlalchemy import func, select, text

from .db import STORAGE, WorkJob, engine


def work_mode():
    return os.getenv('BANKMIND_WORK_MODE', 'off').lower()


def pilot_enabled():
    return os.getenv('BANKMIND_PILOT', 'false').lower() == 'true'


def public_trial_enabled():
    return os.getenv('BANKMIND_PUBLIC_TRIAL', 'false').lower() == 'true'


DEMO_ACCOUNTS = (
    ('accountant', 'Accountant', '财务人员', 'BANKMIND_TRIAL_ACCOUNTANT_PASSWORD'),
    ('reviewer', 'Reviewer', '独立审核', 'BANKMIND_TRIAL_PASSWORD'),
    ('auditor', 'Auditor', '审计人员', 'BANKMIND_TRIAL_AUDITOR_PASSWORD'),
    ('admin', 'Admin', '管理员', ''),
)


def trial_accounts():
    accounts = {}
    for username, role, _, variable in DEMO_ACCOUNTS:
        password = os.getenv(variable) if variable else None
        if password:
            accounts[username] = (role, password)
    return accounts


def validate_trial_deepseek():
    key = os.getenv('BANKMIND_DEEPSEEK_KEY', '')
    if os.getenv('BANKMIND_TRIAL_DEEPSEEK_ENABLED', 'false').lower() != 'true':
        if key:
            raise RuntimeError('Set BANKMIND_TRIAL_DEEPSEEK_ENABLED=true to enable DeepSeek for this trial, or remove BANKMIND_DEEPSEEK_KEY')
        return

    base = os.getenv('BANKMIND_DEEPSEEK_URL', '')
    model = os.getenv('BANKMIND_DEEPSEEK_MODEL', '')
    if not all(value and not any(char.isspace() for char in value) for value in (key, base, model)):
        raise RuntimeError('Set BANKMIND_DEEPSEEK_KEY, BANKMIND_DEEPSEEK_URL and BANKMIND_DEEPSEEK_MODEL to their values only')
    if key.startswith(('LLM_API_KEY=', 'DEEPSEEK_API_KEY=', 'BANKMIND_DEEPSEEK_KEY=')):
        raise RuntimeError('BANKMIND_DEEPSEEK_KEY must contain only the API key')
    if model.startswith(('LLM_MODEL=', 'DEEPSEEK_MODEL=', 'BANKMIND_DEEPSEEK_MODEL=')):
        raise RuntimeError('BANKMIND_DEEPSEEK_MODEL must contain only the model name')
    try:
        url = urlsplit(base)
        valid_url = (base.startswith('https://') and url.hostname and not
                     (url.username is not None or url.password is not None or url.query or url.fragment)
                     and not url.path.rstrip('/').endswith('/chat/completions'))
        url.port  # Validate an explicitly configured port.
    except ValueError:
        valid_url = False
    if not valid_url:
        raise RuntimeError('BANKMIND_DEEPSEEK_URL must be an HTTPS base URL without /chat/completions')


def validate_deployment():
    if work_mode() not in ('off', 'embedded', 'external'):
        raise RuntimeError('BANKMIND_WORK_MODE must be off, embedded or external')
    if work_mode() != 'off' and engine.dialect.name not in ('sqlite','postgresql'):
        raise RuntimeError('This queue deployment requires SQLite or PostgreSQL')
    if public_trial_enabled():
        accounts = trial_accounts()
        passwords = [password for _, password in accounts.values()]
        if ('reviewer' not in accounts or any(len(password.strip()) < 16 for password in passwords)
            or len(set(passwords)) != len(passwords)):
            raise RuntimeError('Set a unique password of at least 16 characters for each enabled trial account')
        if os.getenv('BANKMIND_DEMO', 'false').lower() != 'true' or pilot_enabled():
            raise RuntimeError('The public experience service requires its own synthetic demo database')
        if (os.getenv('LLM_MODE', 'mock').lower() != 'mock'
            or any(os.getenv(name, 'false').lower() == 'true' for name in (
                'LLM_ALLOW_EXTERNAL', 'DOCUMENT_AI_ENABLED', 'BANKMIND_CODEX_ENABLED'))
            or os.getenv('WECHAT_APP_SECRET')):
            raise RuntimeError('Public trial requires LLM_MODE=mock, enterprise-code login, and generic external AI, document AI and Codex disabled')
        validate_trial_deepseek()
    if not pilot_enabled():
        return
    if os.getenv('BANKMIND_DEMO', 'true').lower() != 'false' or work_mode() == 'off':
        raise RuntimeError('Pilot requires BANKMIND_DEMO=false and an enabled worker')
    if os.getenv('LLM_ALLOW_EXTERNAL', 'false').lower() != 'false' or os.getenv('LLM_MODE', 'mock').lower() != 'mock':
        raise RuntimeError('Pilot case investigation requires local mock mode')
    key = Path(os.getenv('BANKMIND_SIGNING_KEY_FILE',''))
    if not key.is_absolute() or not key.is_file() or key.stat().st_size != 32 or not os.access(key,os.R_OK):
        raise RuntimeError('Pilot requires a separately provisioned 32-byte Ed25519 signing key file')
    if os.name != 'nt' and key.stat().st_mode & 0o077:
        raise RuntimeError('Pilot signing key must be readable only by its operating-system owner')


def readiness(db):
    db.execute(text('SELECT 1'))
    limit = 60
    try:
        heartbeat_age = max(0, int(time() - (STORAGE/'worker.heartbeat').stat().st_mtime))
    except FileNotFoundError:
        heartbeat_age = None
    disk_ok = shutil.disk_usage(STORAGE).free >= 128 * 1024 * 1024
    worker_ok = work_mode() == 'off' or (heartbeat_age is not None and heartbeat_age <= limit)
    return {'ready':disk_ok and worker_ok,'database':'available','disk_ok':disk_ok,
            'worker': 'disabled' if work_mode() == 'off' else 'available' if worker_ok else 'stalled'}


def queue_metrics(db, tenant_id):
    scope = WorkJob.tenant_id == tenant_id
    counts = dict(db.execute(select(WorkJob.status,func.count()).where(scope).group_by(WorkJob.status)).all())
    oldest = db.scalar(select(func.min(WorkJob.created_at)).where(scope,WorkJob.status=='QUEUED'))
    age = max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(oldest)).total_seconds())) if oldest else None
    return {**readiness(db),'job_counts':counts,'oldest_queued_job_created_seconds_ago':age}
