import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.main import app
from server.models import SyncBatch
from server.storage import Store

PASSWORD = 'correct horse battery staple'
SID = 'rp-' + 'a' * 32
STREAM = 'b' * 32


@pytest.fixture
def account_client(tmp_path, monkeypatch):
    monkeypatch.setenv('PANGOLIN_AUTH_MODE', 'accounts')
    monkeypatch.setenv('PANGOLIN_DATABASE', str(tmp_path / 'server.sqlite3'))
    monkeypatch.setenv('PANGOLIN_PUBLIC_URL', 'https://pangolin.test')
    monkeypatch.setenv('PANGOLIN_REGISTRATION', 'open')
    with TestClient(app, base_url='https://pangolin.test') as client:
        yield client


def register(client, email='one@example.com'):
    response = client.post('/api/auth/register', json={'email': email, 'password': PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def bind(client, email='one@example.com', **extra):
    response = client.post('/api/auth/device-login', json={'email': email, 'password': PASSWORD, 'name': 'devbox', **extra})
    assert response.status_code == 200, response.text
    return response.json()


def device_headers(device):
    return {'Authorization': 'Bearer ' + device['device_token'], 'X-Device-ID': device['device']}


def batch(entries, stream=STREAM):
    return {'type': 'sync', 'stream': stream, 'entries': entries}


def entry(seq, kind, payload, session=SID):
    return {'seq': seq, 'kind': kind, 'session': session, 'payload': payload}


def session(seq=1):
    return entry(seq, 'session', {'project': 'example', 'agent': 'codex', 'status': 'running'})


def event(seq=2, status='pending', text='请检查项目'):
    return entry(seq, 'event', {'id': 1, 'source': 'user', 'text': text, 'created_at': time.time(), 'status': status})


def ingest(device, values):
    return app.state.store.ingest(device['device'], SyncBatch.model_validate(batch(values)))


def test_registration_sessions_csrf_and_password_privacy(account_client, tmp_path):
    client = account_client
    response = client.post('/api/auth/register', json={'email': 'One@Example.com', 'password': PASSWORD})
    assert response.status_code == 201
    assert response.json()['user']['email'] == 'one@example.com'
    assert response.json()['user']['email_verified'] is False
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    assert client.get('/api/auth/me').status_code == 200
    assert client.post('/api/auth/logout').status_code == 403
    csrf = response.json()['csrf']
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf, 'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/api/machines').status_code == 401
    assert client.get('/api/machines', headers={'Authorization': 'Bearer ' + 'u' * 40}).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'one@example.com', 'password': 'incorrect-password'}).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'one@example.com', 'password': PASSWORD}).status_code == 200
    row = app.state.store.user('one@example.com')
    assert row['password_hash'].startswith('$argon2id$') and PASSWORD not in row['password_hash']
    assert stat.S_IMODE((tmp_path / 'server.sqlite3').stat().st_mode) == 0o600
    assert client.post('/api/auth/register', json={'email': 'one@example.com', 'password': PASSWORD}).status_code == 409
    invalid = client.post('/api/auth/register', json={'email': 'bad', 'password': 'secret'})
    assert invalid.status_code == 422 and 'secret' not in invalid.text and 'input' not in invalid.json()['detail'][0]
    assert client.post('/api/auth/register', content='x' * 9000).status_code == 413


def test_registration_policy_and_rate_limit(account_client):
    client = account_client
    app.state.registration = False
    assert client.post('/api/auth/register', json={'email': 'one@example.com', 'password': PASSWORD}).status_code == 403
    app.state.registration = True
    register(client)
    for _ in range(10):
        assert client.post('/api/auth/login', json={'email': 'one@example.com', 'password': 'incorrect-password'}).status_code == 401
    response = client.post('/api/auth/login', json={'email': 'one@example.com', 'password': PASSWORD})
    assert response.status_code == 429 and response.headers['retry-after']


def test_every_device_endpoint_checks_owner(account_client):
    client = account_client
    one = register(client)
    device = bind(client)
    ingest(device, [session(), event(status='sent')])
    two = register(client, 'two@example.com')
    assert client.get('/api/machines').json() == []
    path = '/api/machines/' + device['device']
    for method, suffix, body in [
        ('GET', '/projects', None), ('GET', '/sessions', None),
        ('GET', '/sessions/' + SID + '/state', None), ('GET', '/sessions/' + SID + '/logs', None),
        ('POST', '/sessions', {'project': 'example', 'agent': 'codex'}),
        ('POST', '/sessions/' + SID + '/send', {'message': 'oops'}),
        ('POST', '/sessions/' + SID + '/input', {'key': 'Enter', 'screen_id': 'c' * 64}),
        ('POST', '/sessions/' + SID + '/stop', None), ('DELETE', '/sessions/' + SID, None), ('DELETE', '/binding', None),
    ]:
        assert client.request(method, path + suffix, json=body, headers={'X-CSRF-Token': two['csrf']}).status_code == 404, suffix
    result = client.post('/api/auth/device-login', json={'email': 'two@example.com', 'password': PASSWORD, 'name': 'stolen', 'device_id': device['device']})
    assert result.status_code == 409
    result = client.post('/api/auth/device-login', json={'email': 'two@example.com', 'password': PASSWORD, 'name': 'stolen', 'expected_account': one['user']['id']})
    assert result.status_code == 409
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/agent', headers={'X-Device-ID': device['device'], 'Authorization': 'Bearer ' + 'x' * 40}):
            pass


def test_sync_status_revisions_offline_history_and_restart(account_client, tmp_path):
    client = account_client
    register(client); device = bind(client)
    path = '/api/machines/' + device['device'] + '/sessions/' + SID
    with client.websocket_connect('/ws/agent', headers=device_headers(device)) as ws:
        ws.send_json(batch([session(), event(), entry(3, 'terminal', {'text': 'CLI output', 'observed_at': time.time()})]))
        assert ws.receive_json()['cursor'] == 3
        # Duplicate upload is acknowledged but creates no additional event.
        ws.send_json(batch([session(), event(), entry(3, 'terminal', {'text': 'duplicate', 'observed_at': time.time()})]))
        assert ws.receive_json()['cursor'] == 3
        row = app.state.store.state(device['device'], SID, 0)
        first_cursor = row['cursor']
        assert row['events'][0]['status'] == 'pending' and row['terminal']['text'] == 'CLI output'
        ws.send_json(batch([event(4, 'sent')]))
        assert ws.receive_json()['cursor'] == 4
        update = app.state.store.state(device['device'], SID, first_cursor)
        assert len(update['events']) == 1 and update['events'][0]['id'] == 1 and update['events'][0]['status'] == 'sent'
    result = client.get(path + '/state').json()
    assert result['events'][0]['text'] == '请检查项目'
    assert result['terminal']['text'] == 'CLI output' and result['live'] is False
    assert result['interaction'] is None and result['screen_id'] is None
    assert client.post(path + '/send', json={'message': 'offline'}, headers={'X-CSRF-Token': client.get('/api/auth/me').json()['csrf']}).status_code == 503
    restored = Store(tmp_path / 'server.sqlite3')
    try:
        assert restored.state(device['device'], SID, 0)['events'] == result['events']
    finally:
        restored.close()


def test_delete_tombstone_blocks_old_and_new_uploads(account_client):
    client = account_client
    user = register(client); device = bind(client)
    ingest(device, [session(), event(status='sent')])
    path = '/api/machines/' + device['device'] + '/sessions/' + SID
    assert client.delete(path, headers={'X-CSRF-Token': user['csrf']}).status_code == 200
    assert client.get(path + '/state').status_code == 404
    answer = ingest(device, [session(3), event(4, 'sent', 'must not return')])
    assert answer['deleted'] == [SID] and answer['cursor'] == 4
    assert app.state.store.sessions(device['device']) == []
    assert app.state.store.db.execute('SELECT count(*) FROM events').fetchone()[0] == 0
    answer = ingest(device, [entry(5, 'deleted', {})])
    assert answer['deleted'] == []


def test_sync_batch_atomicity_stream_binding_and_payload_bounds(account_client):
    client = account_client
    register(client); device = bind(client)
    with pytest.raises(ValueError):
        ingest(device, [session(), event(4)])
    assert app.state.store.sessions(device['device']) == []
    assert app.state.store.device_auth(device['device'], device['device_token'])['cursor'] == 0
    ingest(device, [session()])
    with pytest.raises(ValueError):
        app.state.store.ingest(device['device'], SyncBatch.model_validate(batch([event()], 'c' * 32)))
    with pytest.raises(ValueError):
        ingest(device, [event(2, text='x' * 16001)])
    assert app.state.store.device_auth(device['device'], device['device_token'])['cursor'] == 1


def test_password_change_revokes_cookies_and_devices(account_client):
    client = account_client
    user = register(client); device = bind(client)
    with client.websocket_connect('/ws/agent', headers=device_headers(device)) as ws:
        response = client.post('/api/auth/password', headers={'X-CSRF-Token': user['csrf']},
                               json={'current_password': PASSWORD, 'new_password': 'new password with enough length'})
        assert response.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/agent/refresh', headers=device_headers(device)).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'one@example.com', 'password': PASSWORD}).status_code == 401
    assert client.post('/api/auth/login', json={'email': 'one@example.com', 'password': 'new password with enough length'}).status_code == 200


def test_revoke_only_own_device_and_expired_credentials(account_client):
    client = account_client
    user = register(client); first = bind(client); second = bind(client)
    assert client.delete('/api/machines/' + first['device'] + '/binding', headers={'X-CSRF-Token': user['csrf']}).status_code == 200
    assert client.post('/api/agent/refresh', headers=device_headers(first)).status_code == 401
    assert client.post('/api/agent/refresh', headers=device_headers(second)).status_code == 200
    with app.state.store.db:
        app.state.store.db.execute('UPDATE devices SET expires_at=0 WHERE id=?', (second['device'],))
    assert client.post('/api/agent/refresh', headers=device_headers(second)).status_code == 401
    rebound = bind(client, device_id=second['device'])
    assert rebound['device'] == second['device'] and rebound['device_token'] != second['device_token']
    assert client.post('/api/agent/refresh', headers=device_headers(rebound)).status_code == 200


def test_history_retention_keeps_running_sessions(account_client):
    register(account_client); device = bind(account_client)
    ingest(device, [session(), event(status='sent')])
    with app.state.store.db:
        app.state.store.db.execute('UPDATE conversations SET updated_at=0')
    app.state.store.expire_history(30)
    assert app.state.store.conversation(device['device'], SID)
    with app.state.store.db:
        app.state.store.db.execute("UPDATE conversations SET status='stopped'")
    app.state.store.expire_history(30)
    assert app.state.store.conversation(device['device'], SID) is None
