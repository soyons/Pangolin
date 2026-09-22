import concurrent.futures
import os
import shutil
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from agent.main import Sessions
from server.main import app

USER = 'u' * 40
DEVICE = 'd' * 40


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('PANGOLIN_AUTH_MODE', 'legacy')
    monkeypatch.setenv('USER_TOKEN', USER)
    monkeypatch.setenv('DEVICE_TOKENS', 'test:' + DEVICE)
    with TestClient(app) as c:
        yield c


def test_auth_and_offline(client):
    assert client.get('/api/machines').status_code == 401
    headers = {'Authorization': 'Bearer ' + USER}
    assert client.get('/api/machines', headers=headers).json()[0]['online'] is False
    assert client.get('/api/machines/test/sessions', headers=headers).status_code == 503
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/agent', headers={'X-Device-ID': 'test', 'Authorization': 'Bearer bad'}):
            pass


def test_relay_roundtrip_disconnect_and_duplicate(client):
    headers = {'Authorization': 'Bearer ' + USER}
    device_headers = {'Authorization': 'Bearer ' + DEVICE, 'X-Device-ID': 'test'}
    with client.websocket_connect('/ws/agent', headers=device_headers) as ws:
        ws.send_json({'type': 'heartbeat'})
        assert client.get('/api/machines', headers=headers).json()[0]['online'] is True
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/agent', headers=device_headers):
                pass
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(client.get, '/api/machines/test/sessions', headers=headers)
            cmd = ws.receive_json()
            assert cmd['action'] == 'session.list'
            ws.send_json({'id': cmd['id'], 'ok': True, 'result': [{'id': 'test-session'}]})
            assert future.result(timeout=5).json() == [{'id': 'test-session'}]
            future = pool.submit(client.get, '/api/machines/test/sessions', headers=headers)
            ws.receive_json()
            ws.close()
            assert future.result(timeout=5).status_code == 503


def test_agent_rejects_untrusted_commands(tmp_path):
    s = Sessions({'projects': {'example': {'path': str(tmp_path), 'agents': ['codex']}}})
    for msg in [
        {'action': 'shell.exec'},
        {'action': 'session.create', 'project': '../other', 'agent': 'codex'},
        {'action': 'session.create', 'project': 'example', 'agent': 'sh'},
        {'action': 'session.stop', 'session': '*'},
        {'action': 'session.logs', 'session': 'other:0'},
    ]:
        with pytest.raises(ValueError):
            s.handle(msg)


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux not installed')
def test_real_tmux_lifecycle(tmp_path, monkeypatch):
    # A fake interactive CLI exercises real tmux without making model API calls.
    cli = tmp_path / 'codex'
    cli.write_text('#!/bin/sh\nexec cat\n')
    cli.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    s = Sessions({'tmux_socket': 'rp-test-' + uuid.uuid4().hex,
                  'projects': {'example': {'path': str(tmp_path), 'agents': ['codex']}}})
    try:
        result = s.handle({'action': 'session.create', 'project': 'example', 'agent': 'codex'})
        sid = result['id']
        assert result in s.handle({'action': 'session.list'})
        message = 'literal $(echo UNEXPECTED)\nsecond line'
        s.handle({'action': 'session.send', 'session': sid, 'message': message})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            logs = s.handle({'action': 'session.logs', 'session': sid})['text']
            if 'second line' in logs:
                break
            time.sleep(.05)
        assert '$(echo UNEXPECTED)' in logs and 'second line' in logs
        with pytest.raises(ValueError):
            s.handle({'action': 'session.send', 'session': sid, 'message': '\x03'})
        s.handle({'action': 'session.stop', 'session': sid})
        assert s.handle({'action': 'session.list'}) == []
    finally:
        s.tmux('kill-server', check=False)


def test_placeholder_tokens_fail(monkeypatch):
    monkeypatch.setenv('PANGOLIN_AUTH_MODE', 'legacy')
    monkeypatch.setenv('USER_TOKEN', 'REPLACE_WITH_RANDOM_USER_TOKEN')
    monkeypatch.setenv('DEVICE_TOKENS', 'test:REPLACE_WITH_RANDOM_DEVICE_TOKEN')
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


@pytest.mark.parametrize('path,method,body,action', [
    ('/projects', 'GET', None, 'project.list'),
    ('/sessions/rp-test/state?after=12', 'GET', None, 'session.state'),
    ('/sessions/rp-test/input', 'POST', {'screen_id': 'a' * 64, 'choice': '2'}, 'session.input'),
    ('/sessions/rp-test/send', 'POST', {'message': '任务', 'request_id': 'b' * 32}, 'session.send'),
])
def test_new_api_roundtrip_and_auth(client, path, method, body, action):
    url = '/api/machines/test' + path
    assert client.request(method, url, json=body).status_code == 401
    with client.websocket_connect('/ws/agent', headers={
            'Authorization': 'Bearer ' + DEVICE, 'X-Device-ID': 'test'}) as ws:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(client.request, method, url, json=body, headers={'Authorization': 'Bearer ' + USER})
            command = ws.receive_json()
            assert command['action'] == action
            if action == 'session.state':
                assert command['after'] == 12
            if body:
                for key, value in body.items():
                    assert command[key] == value
            ws.send_json({'id': command['id'], 'ok': True, 'result': {'accepted': True}})
            assert future.result(timeout=5).json() == {'accepted': True}


def test_input_conflict_is_visible(client):
    with client.websocket_connect('/ws/agent', headers={
            'Authorization': 'Bearer ' + DEVICE, 'X-Device-ID': 'test'}) as ws:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(client.post, '/api/machines/test/sessions/rp-test/input',
                                 json={'key': 'Enter', 'screen_id': 'f' * 64},
                                 headers={'Authorization': 'Bearer ' + USER})
            command = ws.receive_json()
            ws.send_json({'id': command['id'], 'ok': False, 'status': 409, 'error': '终端内容已变化'})
            response = future.result(timeout=5)
            assert response.status_code == 409
            assert response.json()['detail'] == '终端内容已变化'


@pytest.mark.parametrize('body', [
    {'key': 'C-z', 'screen_id': 'a' * 64},
    {'key': 'Enter', 'screen_id': 'a' * 64, 'choice': '1'},
    {'screen_id': 'a' * 64}, {'key': 'Enter', 'screen_id': 'bad'},
    {'key': 'Enter', 'screen_id': 'a' * 64, 'request_id': 'invalid'},
])
def test_input_schema_rejects_unsafe_requests(client, body):
    assert client.post('/api/machines/test/sessions/rp-test/input', json=body,
                       headers={'Authorization': 'Bearer ' + USER}).status_code == 422


def test_frontend_assets(client):
    assert client.get('/').status_code == 200
    assert 'text/javascript' in client.get('/app.js').headers['content-type']
    assert 'text/css' in client.get('/app.css').headers['content-type']
