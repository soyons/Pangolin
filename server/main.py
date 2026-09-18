"""RelayPilot: single-process authenticated WebSocket relay."""
import asyncio
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

peers = {}
pending = {}
user_token = ""
device_tokens = {}


@asynccontextmanager
async def lifespan(app):
    global user_token, device_tokens
    user_token = os.environ.get("USER_TOKEN", "")
    entries = os.environ.get("DEVICE_TOKENS", "").split(",")
    device_tokens = dict(entry.strip().split(":", 1) for entry in entries if ":" in entry)
    tokens = [user_token, *device_tokens.values()]
    if not device_tokens or any(len(t) < 32 or "REPLACE" in t for t in tokens):
        raise RuntimeError("Set USER_TOKEN and DEVICE_TOKENS to distinct random tokens (32+ characters)")
    if len(set(tokens)) != len(tokens):
        raise RuntimeError("Each user/device token must be distinct")
    yield
    for peer in list(peers.values()):
        await peer["ws"].close()
    peers.clear()


app = FastAPI(title="RelayPilot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(authorization: str = Header(default="")):
    if not user_token or not hmac.compare_digest(authorization, "Bearer " + user_token):
        raise HTTPException(401, "Invalid access token")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).resolve().parents[1] / "web/index.html")


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/api/machines", dependencies=[Depends(auth)])
async def machines():
    return [{"id": k, "online": k in peers, "last_seen": peers.get(k, {}).get("seen")} for k in device_tokens]


async def rpc(device, action, **data):
    peer = peers.get(device)
    if peer is None:
        raise HTTPException(503, "Device offline")
    if sum(p is peer for p, _ in pending.values()) >= 32:
        raise HTTPException(429, "Too many pending commands")
    request_id = uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = (peer, future)
    try:
        await peer["ws"].send_json({"id": request_id, "action": action, **data})
        reply = await asyncio.wait_for(future, 20)
        if not reply.get("ok"):
            raise HTTPException(400, reply.get("error", "Agent rejected command"))
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


@app.get("/api/machines/{device}/sessions", dependencies=[Depends(auth)])
async def sessions(device: str):
    return await rpc(device, "session.list")


@app.post("/api/machines/{device}/sessions", dependencies=[Depends(auth)])
async def create(device: str, body: Create):
    return await rpc(device, "session.create", **body.model_dump())


@app.post("/api/machines/{device}/sessions/{session}/send", dependencies=[Depends(auth)])
async def send(device: str, session: str, body: Prompt):
    return await rpc(device, "session.send", session=session, message=body.message)


@app.get("/api/machines/{device}/sessions/{session}/logs", dependencies=[Depends(auth)])
async def logs(device: str, session: str):
    return await rpc(device, "session.logs", session=session)


@app.delete("/api/machines/{device}/sessions/{session}", dependencies=[Depends(auth)])
async def stop(device: str, session: str):
    return await rpc(device, "session.stop", session=session)


@app.websocket("/ws/agent")
async def agent_socket(ws: WebSocket):
    device = ws.headers.get("x-device-id", "")
    expected = device_tokens.get(device)
    if not expected or not hmac.compare_digest(ws.headers.get("authorization", ""), "Bearer " + expected):
        await ws.close(code=1008)
        return
    if device in peers:
        await ws.close(code=1008, reason="Device already connected")
        return
    await ws.accept()
    peer = {"ws": ws, "seen": time.time()}
    peers[device] = peer
    try:
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), 75)
            peer["seen"] = time.time()
            if not isinstance(msg, dict):
                break
            item = pending.get(msg.get("id"))
            if item and item[0] is peer and not item[1].done():
                item[1].set_result(msg)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        if peers.get(device) is peer:
            peers.pop(device)
        for owner, future in list(pending.values()):
            if owner is peer and not future.done():
                future.set_exception(ConnectionError("Device disconnected"))
        try:
            await ws.close()
        except RuntimeError:
            pass
