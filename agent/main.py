"""Outbound-only agent. No remote shell execution endpoint."""
import asyncio
import json
import logging
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import uuid
from urllib.parse import urlparse

import yaml
from websockets.asyncio.client import connect

LOG = logging.getLogger("relaypilot")
SESSION = re.compile(r"^rp-[0-9a-f]{32}$")


class Sessions:
    def __init__(self, config):
        self.projects = config.get("projects", {})
        self.socket = config.get("tmux_socket", "relaypilot")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.socket):
            raise ValueError("Invalid tmux socket name")

    def tmux(self, *args, data=None, check=True):
        result = subprocess.run(["tmux", "-f", "/dev/null", "-L", self.socket, *args], input=data,
                                capture_output=True, text=True, timeout=10)
        if check and result.returncode:
            raise ValueError("tmux operation failed; check the agent locally")
        return result

    def handle(self, msg):
        action = msg.get("action")
        if action == "session.list":
            result = self.tmux("list-sessions", "-F", "#{session_name}", check=False)
            return [{"id": s} for s in result.stdout.splitlines() if SESSION.fullmatch(s)]
        if action == "session.create":
            project = self.projects.get(msg.get("project"))
            agent = msg.get("agent")
            if not project or agent not in ("codex", "claude") or agent not in project.get("agents", []):
                raise ValueError("Project or agent is not allowed")
            path = Path(project["path"]).expanduser().resolve(strict=True)
            executable = shutil.which(agent)
            if not path.is_dir() or not executable:
                raise ValueError("Project directory or agent executable unavailable")
            session = "rp-" + uuid.uuid4().hex
            # Multiple command arguments invoke the executable directly, without a shell.
            # 'env' supplies the second argument; no arbitrary command comes from the relay.
            self.tmux("new-session", "-d", "-s", session, "-c", str(path),
                      "-x", "120", "-y", "40", "env", executable)
            return {"id": session, "project": msg["project"], "agent": agent}
        if action not in ("session.send", "session.logs", "session.stop"):
            raise ValueError("Unknown action")
        session = msg.get("session", "")
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise ValueError("Invalid session")
        target = "=" + session
        self.tmux("has-session", "-t", target)
        if action == "session.logs":
            return {"text": self.tmux("capture-pane", "-p", "-t", session + ":0.0", "-S", "-200").stdout[-65536:]}
        if action == "session.stop":
            self.tmux("kill-session", "-t", target)
            return {"stopped": True}
        message = msg.get("message", "")
        if not isinstance(message, str) or not 1 <= len(message) <= 16000:
            raise ValueError("Prompt must contain 1–16000 characters")
        if any(ord(c) < 32 and c not in "\n\t" for c in message) or "\x7f" in message:
            raise ValueError("Terminal control characters are not allowed")
        buffer = "rp-" + uuid.uuid4().hex
        self.tmux("load-buffer", "-b", buffer, "-", data=message)
        try:
            self.tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", session + ":0.0")
            self.tmux("send-keys", "-t", session + ":0.0", "Enter")
        finally:
            self.tmux("delete-buffer", "-b", buffer, check=False)
        return {"sent": True}


async def heartbeat(ws):
    while True:
        await ws.send(json.dumps({"type": "heartbeat"}))
        await asyncio.sleep(25)


async def run():
    url = os.environ["RELAY_WS_URL"]
    parsed = urlparse(url)
    if parsed.scheme != "wss" and not (parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Use wss:// outside loopback")
    with open(os.environ.get("AGENT_CONFIG", "config/agent.local.yaml")) as f:
        sessions = Sessions(yaml.safe_load(f))
    headers = {"Authorization": "Bearer " + os.environ["DEVICE_TOKEN"], "X-Device-ID": os.environ["DEVICE_ID"]}
    delay = 1
    while True:
        try:
            async with connect(url, additional_headers=headers, max_size=131072,
                               ping_interval=20, ping_timeout=20) as ws:
                LOG.info("Connected to relay")
                delay = 1
                beat = asyncio.create_task(heartbeat(ws))
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if not isinstance(msg, dict):
                            raise ValueError("Invalid command envelope")
                        try:
                            result = await asyncio.to_thread(sessions.handle, msg)
                            reply = {"id": msg.get("id"), "ok": True, "result": result}
                        except Exception:
                            # Do not relay local paths, command output or environment details.
                            reply = {"id": msg.get("id"), "ok": False, "error": "Command rejected or failed; check local configuration/session"}
                        await ws.send(json.dumps(reply))
                finally:
                    beat.cancel()
                    await asyncio.gather(beat, return_exceptions=True)
        except Exception as exc:
            LOG.warning("Connection interrupted (%s); reconnecting", type(exc).__name__)
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
