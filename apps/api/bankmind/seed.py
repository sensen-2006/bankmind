import os
import sys
from sqlalchemy import delete, select
from .db import ROOT, STORAGE, Tenant, Account, User, Task, ImportBatch, Token, uid
from .operations import trial_accounts
from .security import password_hash, password_ok, audit
from .parsing import read_table
from .services import validate_import, execute_run


def set_trial_passwords(db):
    for username, (role, password) in trial_accounts().items():
        user = db.scalar(select(User).where(User.username == username, User.role == role))
        tenant = db.get(Tenant, user.tenant_id) if user else None
        if not tenant or tenant.name != '澄明制造 · 合成演示企业':
            raise RuntimeError('The experience service requires its synthetic demo accounts')
        if not password_ok(password, user.password_hash):
            user.password_hash = password_hash(password)
            db.execute(delete(Token).where(Token.user_id == user.id))
    db.commit()

def seed(db):
    if db.scalar(select(Tenant).limit(1)) or os.getenv('BANKMIND_DEMO','true').lower()!='true': return
    sys.path.insert(0,str(ROOT/'scripts'))
    from generate_demo import generate, ACCOUNT
    generate(ROOT/'data/demo')
    t=Tenant(id=uid(),name='澄明制造 · 合成演示企业');db.add(t)
    a=Account(id=uid(),tenant_id=t.id,name='基本存款账户',number=ACCOUNT,bank='模拟工商银行',currency='CNY');db.add(a)
    users=[]
    for username,name,role in [('admin','林予安','Admin'),('accountant','陈禾','Accountant'),('reviewer','周知远','Reviewer'),('auditor','许清','Auditor')]:
        u=User(id=uid(),tenant_id=t.id,username=username,name=name,role=role,password_hash=password_hash('BankMind@2026'),account_ids=[a.id]);db.add(u);users.append(u)
    other=Tenant(id=uid(),name='隔离验证 · 合成企业');db.add(other)
    oa=Account(id=uid(),tenant_id=other.id,name='隔离测试账户',number='0000000000001122',bank='模拟银行',currency='CNY');db.add(oa)
    db.add(User(tenant_id=other.id,username='other',name='隔离测试员',role='Admin',password_hash=password_hash('BankMind@2026'),account_ids=[oa.id]))
    task=Task(id=uid(),tenant_id=t.id,name='2026年10月 · 基本户月度对账',account_id=a.id,currency='CNY',start_date='2026-10-01',end_date='2026-10-31',creator_id=users[1].id)
    db.add(task);db.flush()
    for side,name in [('BANK','bank'),('BOOK','book')]:
        content=(ROOT/f'data/demo/{name}.csv').read_bytes();table=read_table(content,'.csv')
        key='uploads/'+uid()+'.csv';(STORAGE/'uploads').mkdir(exist_ok=True);(STORAGE/key).write_bytes(content)
        from .security import digest
        import hashlib
        batch=ImportBatch(id=uid(),tenant_id=t.id,task_id=task.id,side=side,filename=f'{name}.csv',sha256=hashlib.sha256(content).hexdigest(),object_key=key,size=len(content),columns=table['columns'],preview=table['rows'][:20],row_count=len(table['rows']))
        db.add(batch);db.flush()
        validate_import(db,batch,{'fields':table['suggested_mapping'],'confirmed':True,'direction':'signed_in','date_format':'ISO','default_account':ACCOUNT,'default_currency':'CNY'},task,users[1])
    execute_run(db,task,users[1]);db.commit()
