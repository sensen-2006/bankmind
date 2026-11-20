import base64
import hashlib
import hmac
import secrets
import json
from datetime import datetime, timezone, timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import User, Token, Audit, now, get_db
from .operations import public_trial_enabled, trial_accounts

def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000)
    return f'{salt}${base64.b64encode(result).decode()}'

def password_ok(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split('$')[0]), encoded)

def issue_token(db, user):
    token = secrets.token_urlsafe(48)
    db.add(Token(tenant_id=user.tenant_id, user_id=user.id, token_hash=digest(token),
                 expires_at=(datetime.now(timezone.utc)+timedelta(hours=8)).isoformat()))
    return token

bearer=HTTPBearer(auto_error=False)

def current_user(request: Request, db: Session = Depends(get_db), credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None:
        raise HTTPException(401, '请先登录')
    token = db.scalar(select(Token).where(Token.token_hash == digest(credentials.credentials)))
    user = db.get(User, token.user_id) if token and token.expires_at > now() else None
    if not user or not user.enabled:
        raise HTTPException(401, '登录已过期或身份已停用')
    if public_trial_enabled():
        access = trial_accounts().get(user.username)
        if not access or user.role != access[0]:
            raise HTTPException(401, '当前账号没有体验权限')
    return user

def role(user, *allowed):
    if user.role not in allowed:
        raise HTTPException(403, '当前角色没有此操作权限')

def scoped(db, model, object_id, user):
    obj = db.get(model, object_id)
    if not obj or obj.tenant_id != user.tenant_id:
        raise HTTPException(404, '对象不存在或不在授权范围内')
    account_id = obj.id if model.__name__=='Account' else getattr(obj, 'account_id', None)
    if account_id and account_id not in user.account_ids:
        raise HTTPException(403, '没有此银行账户的权限')
    return obj

def audit(db, user, action, object_id, before=None, after=None, reason=''):
    db.flush()
    prev = db.scalar(select(Audit).where(Audit.tenant_id == user.tenant_id).order_by(Audit.created_at.desc(), Audit.id.desc()))
    event = Audit(tenant_id=user.tenant_id, actor_id=user.id, action=action, object_id=object_id,
                  before=before or {}, after=after or {}, reason=reason, prev_hash=prev.event_hash if prev else '', created_at=now())
    event.event_hash = digest([event.tenant_id, event.actor_id, event.action, event.object_id,
                               event.before, event.after, event.reason, event.prev_hash, event.created_at])
    db.add(event)

def mask(value):
    return '•••• ' + value[-4:]
