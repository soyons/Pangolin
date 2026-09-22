"""Pangolin: account-scoped relay and durable conversation history."""
import asyncio
import hmac
import json
import secrets
import contextlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from . import accounts
from .models import SyncBatch
from .storage import Store
from pydantic import BaseModel, Field, model_validator

peers = {}
pending = {}
user_token = ""
device_tokens = {}


@asynccontextmanager
async def lifespan(app):
    global user_token, device_tokens
    app.state.mode = os.environ.get('PANGOLIN_AUTH_MODE', 'accounts')
    if app.state.mode not in ('accounts', 'legacy'):
        raise RuntimeError('PANGOLIN_AUTH_MODE must be accounts or legacy')
    app.state.registration = os.environ.get('PANGOLIN_REGISTRATION', 'open') == 'open'
    app.state.public_url = os.environ.get('PANGOLIN_PUBLIC_URL', '').rstrip('/')
    app.state.history_days = max(1, int(os.environ.get('PANGOLIN_HISTORY_DAYS', '30')))
    app.state.peers = peers
    app.state.limiter = accounts.Limiter()
    app.state.password_slots = asyncio.Semaphore(4)
    user_token, device_tokens = '', {}
    if app.state.mode == 'legacy':
        user_token = os.environ.get('USER_TOKEN', '')
        entries = os.environ.get('DEVICE_TOKENS', '').split(',')
        device_tokens = dict(entry.strip().split(':', 1) for entry in entries if ':' in entry)
        tokens = [user_token, *device_tokens.values()]
        if not device_tokens or any(len(t) < 32 or 'REPLACE' in t for t in tokens) or len(set(tokens)) != len(tokens):
            raise RuntimeError('Set distinct USER_TOKEN and DEVICE_TOKENS (32+ random characters)')
    else:
        app.state.store = Store(os.environ.get('PANGOLIN_DATABASE', '.pangolin/server.sqlite3'))
        app.state.dummy_password = await asyncio.to_thread(accounts.PASSWORDS.hash, secrets.token_urlsafe(32))
        app.state.store.expire_history(app.state.history_days)

    async def maintenance():
        last_cleanup = time.monotonic()
        while True:
            await asyncio.sleep(5)
            if app.state.mode == 'accounts':
                for device, peer in list(peers.items()):
                    if not app.state.store.device_auth(device, peer['token']):
                        with contextlib.suppress(RuntimeError):
                            await peer['ws'].close(code=1008, reason='Device authorization expired')
                if time.monotonic() - last_cleanup > 3600:
                    app.state.store.expire_history(app.state.history_days)
                    last_cleanup = time.monotonic()
    task = asyncio.create_task(maintenance())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for peer in list(peers.values()):
            with contextlib.suppress(RuntimeError):
                await peer['ws'].close()
        peers.clear()
        if app.state.mode == 'accounts':
            app.state.store.close()


app = FastAPI(title='Pangolin', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(accounts.router)


@app.middleware('http')
async def limits(request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.url.path.startswith('/api/'):
        body = bytearray()
        limit = 8192 if request.url.path.startswith('/api/auth/') else 131072
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return JSONResponse({'detail': '请求内容过大'}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # FastAPI's default validation response includes raw input, which could contain passwords.
    return JSONResponse({'detail': [{'loc': item['loc'], 'msg': item['msg'], 'type': item['type']}
                                    for item in error.errors()]}, status_code=422)


async def auth(request: Request):
    if app.state.mode == 'legacy':
        if not user_token or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + user_token):
            raise HTTPException(401, 'Invalid access token')
        return None
    return await accounts.current_user(request)


def owned(user, device, session=None):
    if app.state.mode == 'legacy':
        if device not in device_tokens:
            raise HTTPException(404, 'Device not found')
        return None
    row = app.state.store.device(user['id'], device)
    if not row:
        raise HTTPException(404, '设备不存在')
    if session is not None and not app.state.store.conversation(device, session):
        raise HTTPException(404, '对话不存在')
    return row


@app.get("/")
async def index():
    return FileResponse(Path(__file__).resolve().parents[1] / "web/index.html")


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/app.js")
async def browser_script():
    return FileResponse(Path(__file__).resolve().parents[1] / "web/app.js", media_type="text/javascript")


@app.get("/app.css")
async def browser_styles():
    return FileResponse(Path(__file__).resolve().parents[1] / "web/app.css", media_type="text/css")


@app.get('/api/machines')
async def machines(user=Depends(auth)):
    if app.state.mode == 'legacy':
        return [{'id': k, 'online': k in peers, 'last_seen': peers.get(k, {}).get('seen')} for k in device_tokens]
    return [{**row, 'online': row['id'] in peers and not row['revoked']} for row in app.state.store.devices(user['id'])]


async def rpc(device, action, **data):
    peer = peers.get(device)
    if peer is None:
        raise HTTPException(503, "Device offline")
    if app.state.mode == 'accounts' and not app.state.store.device_auth(device, peer['token']):
        raise HTTPException(409, '设备登录已失效，请重新登录')
    if sum(p is peer for p, _ in pending.values()) >= 32:
        raise HTTPException(429, "Too many pending commands")
    request_id = uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = (peer, future)
    try:
        await peer["ws"].send_json({"id": request_id, "action": action, **data})
        reply = await asyncio.wait_for(future, 20)
        if not reply.get("ok"):
            raise HTTPException(409 if reply.get("status") == 409 else 400,
                                reply.get("error", "Agent rejected command"))
        return reply.get("result")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Agent timed out; check session state before retrying")
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        raise HTTPException(503, "Device disconnected")
    finally:
        pending.pop(request_id, None)


class Create(BaseModel):
    project: str = Field(min_length=1, max_length=80)
    agent: str = Field(pattern="^(codex|claude)$")


class Prompt(BaseModel):
    message: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern="^[0-9a-f]{32}$")


class TerminalInput(BaseModel):
    screen_id: str = Field(pattern="^[0-9a-f]{64}$")
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern="^[0-9a-f]{32}$")
    key: Optional[Literal["Up", "Down", "Left", "Right", "Enter", "Escape", "Tab", "Space", "BSpace", "C-c"]] = None
    choice: Optional[str] = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.key is None) == (self.choice is None):
            raise ValueError('Supply exactly one key or choice')
        return self


@app.get('/api/machines/{device}/projects')
async def projects(device: str, user=Depends(auth)):
    row = owned(user, device)
    if app.state.mode == 'accounts' and (device not in peers or row['revoked']):
        return json.loads(row['projects'])
    return await rpc(device, 'project.list')


@app.get('/api/machines/{device}/sessions')
async def sessions(device: str, user=Depends(auth)):
    owned(user, device)
    if app.state.mode == 'accounts':
        return app.state.store.sessions(device)
    return await rpc(device, 'session.list')


@app.post('/api/machines/{device}/sessions')
async def create(device: str, body: Create, user=Depends(auth)):
    row = owned(user, device)
    if row and row['revoked']:
        raise HTTPException(409, '设备已解除绑定，请重新登录')
    if row and len(app.state.store.sessions(device)) >= 1000:
        raise HTTPException(409, '会话数量达到上限，请删除旧对话')
    result = await rpc(device, 'session.create', **body.model_dump())
    if app.state.mode == 'accounts':
        app.state.store.created(user['id'], device, result['id'], body.model_dump())
    return result


def writable(user, device, session):
    row = owned(user, device, session)
    if row:
        if row['revoked']:
            raise HTTPException(409, '设备已解除绑定')
        if app.state.store.conversation(device, session)['status'] != 'running':
            raise HTTPException(409, '会话已停止，请创建新会话')


@app.post('/api/machines/{device}/sessions/{session}/send')
async def send(device: str, session: str, body: Prompt, user=Depends(auth)):
    writable(user, device, session)
    return await rpc(device, 'session.send', session=session, **body.model_dump())


@app.get('/api/machines/{device}/sessions/{session}/state')
async def state(device: str, session: str, after: int = Query(default=0, ge=0, le=9007199254740991), user=Depends(auth)):
    row = owned(user, device, session)
    if app.state.mode == 'legacy':
        return await rpc(device, 'session.state', session=session, after=after)
    result = app.state.store.state(device, session, after)
    if device in peers and not row['revoked'] and result['session']['status'] == 'running':
        try:
            live = await rpc(device, 'session.state', session=session, after=0)
            if live.get('live', True):
                result.update(terminal=live['terminal'], screen_id=live['screen_id'], interaction=live['interaction'], live=True)
        except HTTPException as error:
            if error.status_code not in (400, 409, 503, 504):
                raise
    return result


@app.post('/api/machines/{device}/sessions/{session}/input')
async def terminal_input(device: str, session: str, body: TerminalInput, user=Depends(auth)):
    writable(user, device, session)
    return await rpc(device, 'session.input', session=session, **body.model_dump(exclude_none=True))


@app.get('/api/machines/{device}/sessions/{session}/logs')
async def logs(device: str, session: str, user=Depends(auth)):
    owned(user, device, session)
    if app.state.mode == 'accounts':
        return {'text': (await state(device, session, 0, user))['terminal']['text']}
    return await rpc(device, 'session.logs', session=session)


@app.post('/api/machines/{device}/sessions/{session}/stop')
async def archive(device: str, session: str, user=Depends(auth)):
    owned(user, device, session)
    result = await rpc(device, 'session.stop', session=session)
    if app.state.mode == 'accounts':
        with app.state.store.db:
            app.state.store.db.execute("UPDATE conversations SET status='stopped',updated_at=? WHERE device_id=? AND id=?", (time.time(), device, session))
            app.state.store.touch(user['id'])
    return result


@app.delete('/api/machines/{device}/sessions/{session}')
async def stop(device: str, session: str, user=Depends(auth)):
    owned(user, device, session)
    if app.state.mode == 'legacy':
        return await rpc(device, 'session.stop', session=session)
    app.state.store.delete(user['id'], device, session)
    if device in peers:
        try:
            await rpc(device, 'session.delete', session=session)
        except HTTPException:
            return {'deleted': True, 'local_pending': True}
    return {'deleted': True, 'local_pending': device not in peers}


@app.websocket('/ws/agent')
async def agent_socket(ws: WebSocket):
    device = ws.headers.get('x-device-id', '')
    authorization = ws.headers.get('authorization', '')
    token = authorization[7:] if authorization.startswith('Bearer ') else ''
    owner = None
    if app.state.mode == 'legacy':
        expected = device_tokens.get(device)
        valid = expected and hmac.compare_digest(token, expected)
    else:
        owner = app.state.store.device_auth(device, token)
        valid = owner is not None
    if not valid:
        await ws.close(code=1008)
        return
    if device in peers:
        await ws.close(code=1008, reason='Device already connected')
        return
    await ws.accept()
    peer = {'ws': ws, 'seen': time.time(), 'token': token}
    peers[device] = peer
    if owner:
        app.state.store.presence(owner['user_id'], device)
    try:
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), 75)
            if len(raw.encode()) > 131072:
                break
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                break
            peer['seen'] = time.time()
            if owner and not app.state.store.device_auth(device, token):
                break
            if owner and msg.get('type') == 'sync':
                try:
                    reply = app.state.store.ingest(device, SyncBatch.model_validate(msg))
                except (ValueError, ValidationError):
                    await ws.send_json({'type': 'sync.error', 'error': '同步数据或序号无效，请检查本地 Agent 日志与设备绑定'})
                    break
                await ws.send_json(reply)
                continue
            item = pending.get(msg.get('id')) if isinstance(msg.get('id'), str) else None
            if item and item[0] is peer and not item[1].done():
                item[1].set_result(msg)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        if peers.get(device) is peer:
            peers.pop(device)
            if owner:
                app.state.store.presence(owner['user_id'], device)
        for candidate, future in list(pending.values()):
            if candidate is peer and not future.done():
                future.set_exception(ConnectionError('Device disconnected'))
        with contextlib.suppress(RuntimeError):
            await ws.close()
