"""Bounded file transport for WeChat Cloud Run JSON requests."""
import base64
import binascii
from dataclasses import dataclass, field
import re
from threading import Lock
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
import httpx
from pydantic import BaseModel, Field

from .db import User
from .security import current_user, role

router = APIRouter(prefix='/api/mobile-files', tags=['小程序文件'])
USER = Depends(current_user)
UPLOAD_PART = 48 * 1024
DOWNLOAD_PART = 384 * 1024
UPLOAD_LIMIT = 5 * 1024 * 1024
DOWNLOAD_LIMIT = 24 * 1024 * 1024
CACHE_LIMIT = 48 * 1024 * 1024
TTL = 300

ID = r'[0-9a-f-]{20,40}'
IMPORT_CHUNK = re.compile(rf'/tasks/{ID}/jobs/{ID}/chunks/[0-9]{{1,8}}')
ATTACHMENT = re.compile(rf'/cases/{ID}/attachments')
DOWNLOAD = re.compile(
    rf'/(?:(?:imports|attachments)/{ID}/original|'
    rf'reports/{ID}/export(?:\?format=(?:xlsx|csv)(?:&own_draft=true)?)?)'
)
ATTACHMENT_FIELDS = {
    'purpose', 'external_reference', 'note', 'necessary',
    'replaces_attachment_id', 'original_filename',
}


class UploadStart(BaseModel):
    path: str = Field(max_length=160)
    size: int = Field(ge=1, le=UPLOAD_LIMIT)
    form: dict[str, str] = Field(default_factory=dict, max_length=6)


class UploadPart(BaseModel):
    offset: int = Field(ge=0, le=UPLOAD_LIMIT)
    content: str = Field(min_length=4, max_length=UPLOAD_PART * 4 // 3)


class DownloadStart(BaseModel):
    path: str = Field(max_length=160)


@dataclass
class Transfer:
    owner: str
    size: int
    expires: float
    path: str = ''
    form: dict[str, str] = field(default_factory=dict)
    content: bytes | bytearray = field(default_factory=bytearray)


transfers: dict[str, Transfer] = {}
guard = Lock()


def expire(now):
    for key in [key for key, value in transfers.items() if value.expires <= now]:
        del transfers[key]


def reserve(transfer):
    with guard:
        expire(monotonic())
        if (sum(item.size for item in transfers.values()) + transfer.size > CACHE_LIMIT
                or sum(item.owner == transfer.owner for item in transfers.values()) >= 3):
            raise HTTPException(429, '文件传输繁忙，请稍后重试')
        key = uuid4().hex
        transfers[key] = transfer
        return {'id': key, 'size': transfer.size}


def lookup(key, owner):
    now = monotonic()
    expire(now)
    item = transfers.get(key)
    if item is None or item.owner != owner:
        raise HTTPException(404, '文件传输已过期，请重试')
    item.expires = now + TTL
    return item


async def forward(request, method, path, **kwargs):
    # Re-enter the existing endpoint so parity, authorization and auditing stay in one place.
    headers = {'Authorization': request.headers.get('authorization', '')}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),
                                 base_url='http://bankmind.internal') as client:
        result = await client.request(method, '/api' + path, headers=headers, **kwargs)
    if result.is_error:
        try:
            detail = result.json().get('detail')
        except ValueError:
            detail = None
        raise HTTPException(result.status_code, detail or '文件传输失败')
    return result


@router.post('/uploads')
def start_upload(body: UploadStart, user: User = USER):
    if IMPORT_CHUNK.fullmatch(body.path) and not body.form and body.size <= 1024 * 1024:
        role(user, 'Admin', 'Accountant')
    elif ATTACHMENT.fullmatch(body.path) and not body.form.keys() - ATTACHMENT_FIELDS:
        role(user, 'Admin', 'Accountant', 'Reviewer')
        if any(len(value) > 2000 for value in body.form.values()):
            raise HTTPException(422, '附件表单过长')
    else:
        raise HTTPException(422, '文件上传目标无效')
    return reserve(Transfer(user.id, body.size, monotonic() + TTL, body.path, body.form))


@router.post('/uploads/{key}/part')
def append_upload(key: str, body: UploadPart, user: User = USER):
    try:
        data = base64.b64decode(body.content, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, '文件分块编码无效') from None
    with guard:
        item = lookup(key, user.id)
        if not item.path or not 0 < len(data) <= UPLOAD_PART:
            raise HTTPException(422, '文件分块无效')
        end = body.offset + len(data)
        if body.offset < len(item.content) and item.content[body.offset:end] == data:
            return {'received': len(item.content)}
        if body.offset != len(item.content) or end > item.size:
            raise HTTPException(409, '文件分块顺序或长度无效')
        item.content.extend(data)
        return {'received': end}


@router.post('/uploads/{key}/complete')
async def finish_upload(key: str, request: Request, user: User = USER):
    with guard:
        item = lookup(key, user.id)
        if not item.path or len(item.content) != item.size:
            raise HTTPException(409, '文件未上传完整')
        del transfers[key]
    result = await forward(request, 'POST', item.path, data=item.form, files={
        'file': ('chunk.bin', bytes(item.content), 'application/octet-stream'),
    })
    return result.json()


@router.post('/downloads')
async def start_download(body: DownloadStart, request: Request, user: User = USER):
    if not DOWNLOAD.fullmatch(body.path):
        raise HTTPException(422, '文件下载目标无效')
    result = await forward(request, 'GET', body.path)
    if len(result.content) > DOWNLOAD_LIMIT:
        raise HTTPException(413, '文件过大，请在网页端下载')
    return reserve(Transfer(user.id, len(result.content), monotonic() + TTL, content=result.content))


@router.get('/downloads/{key}')
def download_part(key: str, offset: int = 0, user: User = USER):
    with guard:
        item = lookup(key, user.id)
        if item.path or not 0 <= offset <= item.size:
            raise HTTPException(422, '文件下载位置无效')
        data = item.content[offset:offset + DOWNLOAD_PART]
    return {'content': base64.b64encode(data).decode('ascii'), 'next': offset + len(data)}


@router.post('/{key}/release')
def release(key: str, user: User = USER):
    with guard:
        item = transfers.get(key)
        if item is not None and item.owner == user.id:
            del transfers[key]
    return {'ok': True}
