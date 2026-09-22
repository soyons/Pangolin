"""Email/password authentication, browser sessions and independently revocable devices."""
import asyncio
from collections import defaultdict, deque
import hmac
import json
import sqlite3
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .models import Credentials, DeviceLogin, PasswordChange
from .storage import public_user

router = APIRouter(prefix='/api')
PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
COOKIE = 'pangolin_session'


class Limiter:
    def __init__(self):
        self.entries = defaultdict(deque)

    def check(self, key, limit, seconds=300):
        now = time.monotonic()
        if len(self.entries) > 10000:
            self.entries = defaultdict(deque, {k: v for k, v in self.entries.items() if v and v[-1] > now - 3600})
            if len(self.entries) > 10000 and key not in self.entries:
                raise HTTPException(429, '请求过多，请稍后重试')
        values = self.entries[key]
        while values and values[0] <= now - seconds:
            values.popleft()
        if len(values) >= limit:
            raise HTTPException(429, '尝试次数过多，请稍后重试', headers={'Retry-After': str(seconds)})
        values.append(now)


def database(request):
    if request.app.state.mode != 'accounts':
        raise HTTPException(404, '当前服务端使用旧 Token 模式')
    return request.app.state.store


def check_origin(request):
    origin = request.headers.get('origin')
    if not origin:
        return  # CLI calls have no Origin; browsers still need JSON and CSRF for authenticated writes.
    allowed = request.app.state.public_url or str(request.base_url).rstrip('/')
    if origin != allowed:
        raise HTTPException(403, '请求来源不匹配')


async def current_user(request: Request):
    store = database(request)
    user = store.authenticate(request.cookies.get(COOKIE, ''))
    if not user:
        raise HTTPException(401, '请登录账号')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        check_origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf']):
            raise HTTPException(403, '登录校验已失效，请刷新页面')
    return user


def throttle(request, email, purpose):
    ip = request.client.host if request.client else 'unknown'
    request.app.state.limiter.check(('ip', purpose, ip), 30)
    request.app.state.limiter.check(('email', purpose, email), 10)


async def hash_password(request, password):
    async with request.app.state.password_slots:
        return await asyncio.to_thread(PASSWORDS.hash, password)


async def password_matches(request, encoded, password):
    def verify():
        try:
            return PASSWORDS.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
    async with request.app.state.password_slots:
        return await asyncio.to_thread(verify)


async def validate_login(request, body):
    check_origin(request)
    store = database(request)
    throttle(request, body.email, 'login')
    user = store.user(body.email)
    encoded = user['password_hash'] if user else request.app.state.dummy_password
    if not await password_matches(request, encoded, body.password) or user is None:
        raise HTTPException(401, '邮箱或密码错误')
    # A simultaneous password reset must not allow an older verification to create a fresh session.
    user = store.user(body.email)
    if user['password_hash'] != encoded:
        raise HTTPException(401, '密码已变更，请重新登录')
    return user


def set_login(request, response, user):
    database(request).logout(request.cookies.get(COOKIE, ''))
    token, csrf = database(request).login(user['id'])
    secure = urlsplit(request.app.state.public_url).scheme == 'https' if request.app.state.public_url else request.url.scheme == 'https'
    response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, secure=secure, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return {'user': public_user(user), 'csrf': csrf}


async def disconnect(request, device):
    peer = request.app.state.peers.get(device)
    if peer:
        try:
            await peer['ws'].close(code=1008, reason='Device authorization changed')
        except RuntimeError:
            pass


@router.get('/auth/config')
async def config(request: Request):
    return {'mode': request.app.state.mode, 'registration': request.app.state.registration,
            'email_verification': False, 'history_days': request.app.state.history_days}


@router.post('/auth/register', status_code=201)
async def register(body: Credentials, request: Request, response: Response):
    check_origin(request)
    store = database(request)
    if not request.app.state.registration:
        raise HTTPException(403, '注册已关闭，请联系此服务端管理员')
    throttle(request, body.email, 'register')
    encoded = await hash_password(request, body.password)
    try:
        user = store.create_user(body.email, encoded)
    except sqlite3.IntegrityError:
        raise HTTPException(409, '该邮箱无法注册，请尝试登录或联系管理员')
    return set_login(request, response, user)


@router.post('/auth/login')
async def login(body: Credentials, request: Request, response: Response):
    return set_login(request, response, await validate_login(request, body))


@router.get('/auth/me')
async def me(user=Depends(current_user)):
    return {'user': public_user(user), 'csrf': user['csrf']}


@router.post('/auth/logout')
async def logout(request: Request, response: Response, user=Depends(current_user)):
    database(request).logout(request.cookies.get(COOKIE, ''))
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@router.post('/auth/password')
async def change_password(body: PasswordChange, request: Request, response: Response, user=Depends(current_user)):
    throttle(request, user['email'], 'password')
    if not await password_matches(request, user['password_hash'], body.current_password):
        raise HTTPException(401, '当前密码错误')
    encoded = await hash_password(request, body.new_password)
    store = database(request)
    if store.user(user['email'])['password_hash'] != user['password_hash']:
        raise HTTPException(409, '密码已经变更，请重新登录')
    store.change_password(user['id'], encoded)
    for device in store.devices(user['id']):
        await disconnect(request, device['id'])
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True, 'detail': '密码已修改，请在网页和内网客户端重新登录'}


@router.post('/auth/device-login')
async def device_login(body: DeviceLogin, request: Request):
    user = await validate_login(request, body)
    if body.expected_account and body.expected_account != user['id']:
        raise HTTPException(409, '本地历史属于其他账号，请使用独立的配置目录')
    try:
        result = database(request).bind(user['id'], body.name, body.device_id)
    except ValueError as error:
        raise HTTPException(409, str(error))
    await disconnect(request, result['device'])
    return {**result, 'user': public_user(user)}


async def device_identity(request: Request):
    store = database(request)
    authorization = request.headers.get('authorization', '')
    token = authorization[7:] if authorization.startswith('Bearer ') else ''
    device = store.device_auth(request.headers.get('x-device-id', ''), token)
    if not device:
        raise HTTPException(401, '设备登录已失效，请重新登录')
    return device


@router.post('/agent/refresh')
async def refresh_device(request: Request, device=Depends(device_identity)):
    database(request).refresh_device(device['id'])
    return {'ok': True}


@router.delete('/agent/binding')
async def logout_device(request: Request, device=Depends(device_identity)):
    database(request).revoke(device['user_id'], device['id'])
    await disconnect(request, device['id'])
    return {'ok': True}


@router.delete('/machines/{device}/binding')
async def revoke_device(device: str, request: Request, user=Depends(current_user)):
    store = database(request)
    if not store.device(user['id'], device):
        raise HTTPException(404, '设备不存在')
    store.revoke(user['id'], device)
    await disconnect(request, device)
    return {'ok': True}


@router.get('/updates')
async def updates(request: Request, user=Depends(current_user)):
    token = request.cookies.get(COOKIE, '')
    async def stream():
        previous = None
        while not await request.is_disconnected():
            active = database(request).authenticate(token)
            if not active:
                yield 'event: logout\ndata: {}\n\n'
                return
            revision = active['revision']
            if revision != previous:
                yield 'event: change\ndata: ' + json.dumps({'revision': revision}) + '\n\n'
                previous = revision
            else:
                yield ': heartbeat\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
