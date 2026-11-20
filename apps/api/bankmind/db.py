import os
import platform
import ssl  # preload OpenSSL DLLs before other native modules
if os.name == 'nt' and hasattr(platform, '_wmi_query'):
    platform._wmi_query = lambda *a, **k: (_ for _ in ()).throw(OSError('WMI disabled'))
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from dotenv import load_dotenv
from sqlalchemy import create_engine, event, String, Integer, BigInteger, Boolean, JSON, Text, Index, LargeBinary, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / '.env')
STORAGE = Path(os.getenv('BANKMIND_STORAGE', str(ROOT / 'storage'))).resolve()
STORAGE.mkdir(parents=True, exist_ok=True)
URL = os.getenv('DATABASE_URL', f'sqlite:///{STORAGE / "bankmind.db"}')
engine = create_engine(URL, hide_parameters=True, connect_args={'check_same_thread': False, 'timeout': 30} if URL.startswith('sqlite') else {})
ADVISORY_WRITE_LOCK = text('SELECT pg_advisory_xact_lock(4774185964116126751)')
if URL.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def pragmas(conn, _):
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA temp_store=MEMORY')
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def uid():
    return str(uuid4())

def now():
    return datetime.now(timezone.utc).isoformat()

def lock(db):
    db.rollback()
    if db.bind.dialect.name=='sqlite': db.execute(text('BEGIN IMMEDIATE'))
    elif db.bind.dialect.name=='postgresql':
        # Preserve task, audit-chain and financial-write ordering across API
        # processes. The server releases this lock on commit, rollback or death.
        db.execute(text("SET LOCAL lock_timeout = '30s'"))
        db.execute(ADVISORY_WRITE_LOCK)

class Base(DeclarativeBase):
    pass

class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class Tenant(Base):
    __tablename__ = 'tenants'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))

class User(Record, Base):
    __tablename__ = 'users'
    username: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    account_ids: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class Token(Record, Base):
    __tablename__ = 'tokens'
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[str] = mapped_column(String(40))

class WechatIdentity(Record, Base):
    __tablename__ = 'wechat_identities'
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    app_id: Mapped[str] = mapped_column(String(80))
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True)

class Account(Record, Base):
    __tablename__ = 'accounts'
    name: Mapped[str] = mapped_column(String(120))
    number: Mapped[str] = mapped_column(String(80))
    bank: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default='CNY')

class Task(Record, Base):
    __tablename__ = 'tasks'
    name: Mapped[str] = mapped_column(String(120))
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    creator_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default='DRAFT')
    current_run: Mapped[str | None] = mapped_column(String(36), nullable=True)
    balances: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_config: Mapped[dict] = mapped_column(JSON, default=lambda: {'date_days': 3, 'amount_absolute': '0', 'amount_relative': '0'})
    version: Mapped[int] = mapped_column(Integer, default=1)

class ImportBatch(Record, Base):
    __tablename__ = 'imports'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    side: Mapped[str] = mapped_column(String(4))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(160))
    size: Mapped[int] = mapped_column(Integer)
    sheet: Mapped[str] = mapped_column(String(120), default='')
    header_row: Mapped[int] = mapped_column(Integer, default=1)
    columns: Mapped[list] = mapped_column(JSON, default=list)
    preview: Mapped[list] = mapped_column(JSON, default=list)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='PREVIEWED')
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)

class WorkJob(Record, Base):
    __tablename__ = 'work_jobs'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    request_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    uploaded: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint('tenant_id','task_id','actor_id','request_key'),
                      Index('ix_work_queue','status','created_at'),)

class WorkChunk(Base):
    __tablename__ = 'work_chunks'
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    offset: Mapped[int] = mapped_column(Integer, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

class Transaction(Record, Base):
    __tablename__ = 'transactions'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    import_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36))
    side: Mapped[str] = mapped_column(String(4))
    row_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON)
    date: Mapped[str] = mapped_column(String(10), default='')
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default='')
    reference: Mapped[str] = mapped_column(String(160), default='')
    counterparty: Mapped[str] = mapped_column(String(255), default='')
    description: Mapped[str] = mapped_column(Text, default='')
    group_key: Mapped[str] = mapped_column(String(160), default='')
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    quality: Mapped[str] = mapped_column(String(20), default='VALID')
    issues: Mapped[list] = mapped_column(JSON, default=list)

class Run(Record, Base):
    __tablename__ = 'runs'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    creator_id: Mapped[str] = mapped_column(String(36))
    rule_version: Mapped[str] = mapped_column(String(120))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='SUCCEEDED')
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)

class Match(Record, Base):
    __tablename__ = 'matches'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    bank_ids: Mapped[list] = mapped_column(JSON)
    book_ids: Mapped[list] = mapped_column(JSON)
    method: Mapped[str] = mapped_column(String(40))
    score: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='PENDING')
    creator_id: Mapped[str] = mapped_column(String(36))
    reviewer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class MatchMember(Record, Base):
    __tablename__ = 'match_members'
    match_id: Mapped[str] = mapped_column(String(36), index=True)
    transaction_id: Mapped[str] = mapped_column(String(36))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (Index('uq_active_transaction', 'transaction_id', unique=True,
                            sqlite_where=text('active = 1'), postgresql_where=text('active = true')),)

class Case(Record, Base):
    __tablename__ = 'cases'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    transaction_ids: Mapped[list] = mapped_column(JSON)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default='NEW')
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_date: Mapped[str] = mapped_column(String(10), default='')
    resolution: Mapped[str] = mapped_column(String(40), default='')
    note: Mapped[str] = mapped_column(Text, default='')
    submitter_id: Mapped[str] = mapped_column(String(36), default='')
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class CaseEvent(Record, Base):
    __tablename__ = 'case_events'
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))
    before: Mapped[str] = mapped_column(String(40))
    after: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)

class Receipt(Record, Base):
    __tablename__ = 'receipts'
    key: Mapped[str] = mapped_column(String(120), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)

class Report(Record, Base):
    __tablename__ = 'reports'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    creator_id: Mapped[str] = mapped_column(String(36))
    signer_id: Mapped[str] = mapped_column(String(36), default='')
    status: Mapped[str] = mapped_column(String(20), default='DRAFT')
    snapshot: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)

class Audit(Record, Base):
    __tablename__ = 'audit_events'
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(60))
    object_id: Mapped[str] = mapped_column(String(36))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64), default='')
    event_hash: Mapped[str] = mapped_column(String(64), default='')

class PeriodControl(Record, Base):
    __tablename__ = 'period_controls'
    task_id: Mapped[str] = mapped_column(String(36), unique=True)
    state: Mapped[str] = mapped_column(String(30), default='OPEN')
    version: Mapped[int] = mapped_column(Integer, default=1)
    requester_id: Mapped[str] = mapped_column(String(36), default='')
    reason: Mapped[str] = mapped_column(Text, default='')
    report_id: Mapped[str] = mapped_column(String(36), default='')

class AIInvocation(Record, Base):
    __tablename__ = 'ai_invocations'
    actor_id: Mapped[str] = mapped_column(String(36))
    object_id: Mapped[str] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30))
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    evidence_ids: Mapped[list] = mapped_column(JSON)
    error_code: Mapped[str] = mapped_column(String(80), default='')

class EvidenceAttachment(Record, Base):
    __tablename__ = 'evidence_attachments'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    uploader_id: Mapped[str] = mapped_column(String(36))
    reviewer_id: Mapped[str] = mapped_column(String(36), default='')
    filename: Mapped[str] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(30))
    external_reference: Mapped[str] = mapped_column(String(160), default='')
    note: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default='SUBMITTED')
    version: Mapped[int] = mapped_column(Integer, default=1)

class SourceControl(Record, Base):
    __tablename__ = 'source_controls'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    creator_id: Mapped[str] = mapped_column(String(36))
    reviewer_id: Mapped[str] = mapped_column(String(36), default='')
    declaration: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='SUBMITTED')
    version: Mapped[int] = mapped_column(Integer, default=1)

class EffortSession(Record, Base):
    __tablename__ = 'effort_sessions'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    method: Mapped[str] = mapped_column(String(30))
    phase: Mapped[str] = mapped_column(String(30))
    cycle: Mapped[str] = mapped_column(String(40))
    ended_at: Mapped[str] = mapped_column(String(40), default='')
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default='RUNNING')
    note: Mapped[str] = mapped_column(Text, default='')
    __table_args__ = (Index('uq_running_effort_actor','actor_id',unique=True,
                            sqlite_where=text("status = 'RUNNING'"),postgresql_where=text("status = 'RUNNING'")),)


class SourceControlBinding(Record, Base):
    __tablename__ = 'source_control_bindings'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    source_control_id: Mapped[str] = mapped_column(String(36), unique=True)
    guard_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSON)
    reviewer_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

class EvidenceRequirement(Record, Base):
    __tablename__ = 'evidence_requirements'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    purpose: Mapped[str] = mapped_column(String(30), default='SUPPORT')
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    current_attachment_id: Mapped[str] = mapped_column(String(36), default='')
    attachment_history: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    waiver_id: Mapped[str] = mapped_column(String(36), default='')

class EvidenceWaiver(Record, Base):
    __tablename__ = 'evidence_waivers'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    requirement_id: Mapped[str] = mapped_column(String(36), index=True)
    requester_id: Mapped[str] = mapped_column(String(36))
    reviewer_id: Mapped[str] = mapped_column(String(36), default='')
    status: Mapped[str] = mapped_column(String(20), default='SUBMITTED')
    version: Mapped[int] = mapped_column(Integer, default=1)
    requirement_version: Mapped[int] = mapped_column(Integer)
    approved_requirement_version: Mapped[int] = mapped_column(Integer, default=0)
    case_version_at_request: Mapped[int] = mapped_column(Integer)
    scope: Mapped[dict] = mapped_column(JSON)
    scope_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    review_reason: Mapped[str] = mapped_column(Text, default='')
    decided_at: Mapped[str] = mapped_column(String(40), default='')

class ReportSignature(Record, Base):
    __tablename__ = 'report_signatures'
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    report_id: Mapped[str] = mapped_column(String(36), unique=True)
    envelope: Mapped[dict] = mapped_column(JSON)

class AccountPeriod(Record, Base):
    __tablename__='account_period_authorities'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    report_id: Mapped[str]=mapped_column(String(36),index=True)
    account_id: Mapped[str]=mapped_column(String(36),index=True)
    start_date: Mapped[str]=mapped_column(String(10))
    end_date: Mapped[str]=mapped_column(String(10))
    currency: Mapped[str]=mapped_column(String(3))
    requester_id: Mapped[str]=mapped_column(String(36))
    reviewer_id: Mapped[str]=mapped_column(String(36),default='')
    status: Mapped[str]=mapped_column(String(30),default='SUBMITTED')
    version: Mapped[int]=mapped_column(Integer,default=1)
    reason: Mapped[str]=mapped_column(Text)
    review_reason: Mapped[str]=mapped_column(Text,default='')

class OutstandingItem(Record, Base):
    __tablename__='outstanding_items'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    authority_id: Mapped[str]=mapped_column(String(36),index=True)
    case_id: Mapped[str]=mapped_column(String(36),unique=True)
    account_id: Mapped[str]=mapped_column(String(36),index=True)
    currency: Mapped[str]=mapped_column(String(3))
    resolution: Mapped[str]=mapped_column(String(30))
    amount: Mapped[int]=mapped_column(BigInteger)
    sign: Mapped[int]=mapped_column(Integer)
    origin: Mapped[dict]=mapped_column(JSON)
    version: Mapped[int]=mapped_column(Integer,default=1)

class OpeningBridge(Record, Base):
    __tablename__='opening_bridges'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    prior_authority_id: Mapped[str]=mapped_column(String(36),index=True)
    requester_id: Mapped[str]=mapped_column(String(36))
    reviewer_id: Mapped[str]=mapped_column(String(36),default='')
    scope: Mapped[dict]=mapped_column(JSON)
    scope_hash: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(30),default='SUBMITTED')
    version: Mapped[int]=mapped_column(Integer,default=1)
    reason: Mapped[str]=mapped_column(Text)
    review_reason: Mapped[str]=mapped_column(Text,default='')

class Clearing(Record, Base):
    __tablename__='outstanding_clearings'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    item_id: Mapped[str]=mapped_column(String(36),index=True)
    requester_id: Mapped[str]=mapped_column(String(36))
    reviewer_id: Mapped[str]=mapped_column(String(36),default='')
    allocations: Mapped[list]=mapped_column(JSON)
    evidence_hash: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(30),default='SUBMITTED')
    version: Mapped[int]=mapped_column(Integer,default=1)
    reason: Mapped[str]=mapped_column(Text)
    review_reason: Mapped[str]=mapped_column(Text,default='')
    reversal_requester_id: Mapped[str]=mapped_column(String(36),default='')
    reversal_reason: Mapped[str]=mapped_column(Text,default='')

class TaskRevision(Record, Base):
    __tablename__='task_revisions'
    task_id: Mapped[str]=mapped_column(String(36),unique=True)
    parent_task_id: Mapped[str]=mapped_column(String(36),index=True)
    actor_id: Mapped[str]=mapped_column(String(36))
    reason: Mapped[str]=mapped_column(Text)

class AIOutcome(Record, Base):
    __tablename__='ai_provider_outcomes'
    invocation_id: Mapped[str]=mapped_column(String(36),unique=True)
    attempted_provider: Mapped[str]=mapped_column(String(40))
    result_provider: Mapped[str]=mapped_column(String(40))
    provider_status: Mapped[str]=mapped_column(String(30))
    transmission_state: Mapped[str]=mapped_column(String(30))
    provider_error_code: Mapped[str]=mapped_column(String(80),default='')
    network_attempts: Mapped[int]=mapped_column(Integer,default=0)

class LearningFeedback(Record, Base):
    __tablename__='learning_feedback'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    account_id: Mapped[str]=mapped_column(String(36),index=True)
    creator_id: Mapped[str]=mapped_column(String(36))
    reviewer_id: Mapped[str]=mapped_column(String(36),default='')
    bank_ids: Mapped[list]=mapped_column(JSON)
    book_ids: Mapped[list]=mapped_column(JSON)
    correct: Mapped[bool]=mapped_column(Boolean)
    reason: Mapped[str]=mapped_column(Text)
    review_reason: Mapped[str]=mapped_column(Text,default='')
    status: Mapped[str]=mapped_column(String(24),default='SUBMITTED')
    version: Mapped[int]=mapped_column(Integer,default=1)
    binding_hash: Mapped[str]=mapped_column(String(64))
    sample_key: Mapped[str]=mapped_column(String(64),index=True)
    source_group: Mapped[str]=mapped_column(String(64))
    features: Mapped[dict]=mapped_column(JSON)

class LearningModel(Record, Base):
    __tablename__='learning_models'
    task_id: Mapped[str]=mapped_column(String(36),index=True)
    account_id: Mapped[str]=mapped_column(String(36),index=True)
    trainer_id: Mapped[str]=mapped_column(String(36))
    reviewer_id: Mapped[str]=mapped_column(String(36),default='')
    status: Mapped[str]=mapped_column(String(24),default='CANDIDATE')
    version: Mapped[int]=mapped_column(Integer,default=1)
    artifact: Mapped[dict]=mapped_column(JSON)
    artifact_hash: Mapped[str]=mapped_column(String(64))
    reason: Mapped[str]=mapped_column(Text)
    review_reason: Mapped[str]=mapped_column(Text,default='')

class LearningDeployment(Record, Base):
    __tablename__='learning_deployments'
    account_id: Mapped[str]=mapped_column(String(36),unique=True)
    model_id: Mapped[str]=mapped_column(String(36))
    version: Mapped[int]=mapped_column(Integer,default=1)
    publisher_id: Mapped[str]=mapped_column(String(36))

def init_db():
    if engine.dialect.name=='postgresql':
        with engine.begin() as connection:
            connection.execute(ADVISORY_WRITE_LOCK)
            Base.metadata.create_all(connection)
    else:
        Base.metadata.create_all(engine)

def get_db():
    with SessionLocal() as db:
        try:
            yield db
        finally:
            db.rollback()
