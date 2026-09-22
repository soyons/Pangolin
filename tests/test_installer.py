import argparse
import json
from pathlib import Path
import plistlib
import stat
import os
import shutil
import subprocess

import pytest

from scripts.install import configure, private_write, relay_url, service_file
from scripts.https_ip import merge_caddy, public_ip, site_block, RENEW_HOOK
from scripts.manage import runtime


def args(**overrides):
    defaults = dict(role='server', device=None, port=None, domain=None, ip=None,
                    relay=None, project=None, token_file=None)
    return argparse.Namespace(**{**defaults, **overrides})


def test_server_preserves_pairing_and_generates_distinct_secrets():
    first = configure(args(), {})
    second = configure(args(port=8123), first)
    assert first['user_token'] != first['device_token']
    assert len(first['device_token']) >= 32
    assert first['user_token'] == second['user_token']
    assert first['device_token'] == second['device_token']
    assert second['port'] == 8123
    with pytest.raises(ValueError):
        configure(args(device='different'), first)


@pytest.mark.parametrize('url', ['http://8.8.8.8', 'https://user:pass@example.com',
                                 'https://example.com/?token=oops', 'https://example.com/prefix',
                                 'file:///etc/passwd', 'https://example.com:99999'])
def test_reject_unsafe_relay_urls(url):
    with pytest.raises(ValueError):
        relay_url(url)


def test_relay_url_normalization():
    assert relay_url('https://8.8.8.8/') == 'wss://8.8.8.8/ws/agent'
    assert relay_url('http://127.0.0.1:8000') == 'ws://127.0.0.1:8000/ws/agent'
    assert relay_url('https://[2606:4700:4700::1111]') == 'wss://[2606:4700:4700::1111]/ws/agent'


def test_agent_config_runtime_and_secret_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.install.shutil.which', lambda tool: '/usr/bin/' + tool if tool in ('codex', 'tmux') else None)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-placeholder-key')
    project = tmp_path / 'path with spaces $(); quotes "'
    project.mkdir()
    token = tmp_path / 'token'
    token.write_text('t' * 40)
    config = configure(args(role='agent', relay='https://8.8.8.8', project=str(project),
                            device='devbox', token_file=str(token)), {})
    path = tmp_path / 'agent.json'
    private_write(path, json.dumps(config))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    private_write(path, json.dumps(config))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.setenv('USER_TOKEN', 'must-not-reach-agent')
    command, env = runtime(tmp_path, 'agent')
    assert command == ['-m', 'agent.main']
    assert 'USER_TOKEN' not in env
    assert env['OPENAI_API_KEY'] == 'test-placeholder-key'
    local = json.loads(Path(env['AGENT_CONFIG']).read_text())
    assert local['projects']['example']['path'] == str(project)
    assert local['projects']['example']['agents'] == ['codex']


def test_services_preserve_paths_without_embedding_secrets(tmp_path):
    prefix = tmp_path / 'space $percent% quote"'
    plist = plistlib.loads(service_file(prefix, 'agent', 'darwin'))
    assert plist['ProgramArguments'][0] == str(prefix / 'venv/bin/python')
    unit = service_file(prefix, 'server', 'linux', system=True)
    assert 'User=pangolin' in unit and 'WantedBy=multi-user.target' in unit
    assert '$$percent%%' in unit
    assert 'TOKEN' not in unit and 'TOKEN' not in str(plist)


def test_public_ip_and_caddy_config():
    for value in ['127.0.0.1', '192.168.1.1', '::1', '8.8.8.8\n}']:
        with pytest.raises(ValueError):
            public_ip(value)
    address = public_ip('8.8.8.8')
    before = 'existing.example.com {\n respond "untouched"\n}\n'
    challenge = merge_caddy(before, site_block(address, 8000), address)
    assert 'reverse_proxy' not in challenge  # Never expose the API over plain HTTP.
    final = merge_caddy(challenge, site_block(address, 8000, tls=True), address)
    assert before.strip() in final
    assert final.count('# BEGIN PANGOLIN') == 1
    assert 'https://8.8.8.8' in final
    assert 'reverse_proxy 127.0.0.1:8000' in final
    assert 'tls /var/lib/caddy/pangolin-cert/fullchain.pem' in final
    assert '-m 640' in RENEW_HOOK and 'systemctl reload caddy' in RENEW_HOOK


def test_ip_config_rejects_private_addresses_and_domain_injection():
    with pytest.raises(ValueError):
        configure(args(ip='10.0.0.1'), {})
    with pytest.raises(ValueError):
        configure(args(domain='example.com {\n respond bad'), {})
    c = configure(args(ip='8.8.8.8'), {})
    assert c['ip'] == '8.8.8.8'
    c = configure(args(domain='agent.example.com'), c)
    assert 'ip' not in c and c['domain'] == 'agent.example.com'


@pytest.fixture
def bootstrap(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'source'
    (source / 'scripts').mkdir(parents=True)
    shutil.copy(root / 'install.sh', source / 'install.sh')
    (source / 'scripts/install.py').write_text('# Native installer placeholder')
    (source / 'scripts/docker-server.sh').write_text('printf "docker\\n%s\\n" "$@" > "$CAPTURE"\n')
    bins = tmp_path / 'bin'
    bins.mkdir()
    for name, content in {
        'python3': '#!/bin/bash\nif [[ "$1" == -c ]]; then exit 0; fi\nprintf "%s\\n" "$@" > "$CAPTURE"\n',
        'id': '#!/bin/sh\necho 1000\n',
        'gh': '#!/bin/sh\nexit 1\n',
        'git': '#!/bin/bash\nprintf "%s\\n" "$@" > "$GIT_CAPTURE"\ncp -R "$FIXTURE" "${!#}"\n',
    }.items():
        path = bins / name
        path.write_text(content)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'],
           'CAPTURE': str(tmp_path / 'args'), 'GIT_CAPTURE': str(tmp_path / 'git-args'), 'FIXTURE': str(source)}
    return source, env


@pytest.mark.parametrize('arguments,native', [
    (['server', '--ip', '8.8.8.8', '--no-start'], True),
    (['server', '--domain', 'example.com'], True),
    (['server', '--native', '--no-start'], True),
    (['server', '--docker', '--port', '18080'], False),
    (['agent', '--relay', 'https://example.com', '--project', '/path with spaces'], False),
    (['agent', '--python', '--relay', 'https://example.com', '--project', '/path with spaces'], True),
])
def test_bootstrap_routes_documented_commands(bootstrap, arguments, native):
    source, env = bootstrap
    if arguments[0] == 'agent' and '--python' not in arguments:
        installer = source / 'packages/agent/install.sh'
        installer.parent.mkdir(parents=True)
        installer.write_text('printf "node-agent\\n%s\\n" "$@" > "$CAPTURE"\n')
        installer.chmod(0o755)
    subprocess.run(['bash', str(source / 'install.sh'), *arguments], env=env, check=True, capture_output=True)
    dispatched = Path(env['CAPTURE']).read_text().splitlines()
    if native:
        assert dispatched[0] == str(source / 'scripts/install.py')
    else:
        assert dispatched[0] == ('node-agent' if arguments[0] == 'agent' else 'docker')
    assert dispatched[-1] == arguments[-1]


def test_piped_bootstrap_fetches_selected_ref_and_cleans_temp_checkout(bootstrap):
    source, env = bootstrap
    env['PANGOLIN_REF'] = 'v1.2.3'
    subprocess.run(['bash', '-s', '--', 'server', '--no-start'], input=(source / 'install.sh').read_text(),
                   text=True, env=env, check=True, capture_output=True)
    args = Path(env['CAPTURE']).read_text().splitlines()
    assert args[1] == 'server' and args[-1] == '--no-start'
    assert not Path(args[0]).exists()
    assert '--branch\nv1.2.3' in Path(env['GIT_CAPTURE']).read_text()


def test_failed_download_never_runs_installer(bootstrap):
    source, env = bootstrap
    (Path(env['PATH'].split(os.pathsep)[0]) / 'git').write_text('#!/bin/sh\nexit 9\n')
    result = subprocess.run(['bash', '-s', '--', 'server'], input=(source / 'install.sh').read_text(),
                            text=True, env=env, capture_output=True)
    assert result.returncode != 0
    assert not Path(env['CAPTURE']).exists()


def test_account_install_defaults_and_explicit_legacy_migration(tmp_path):
    fresh = configure(args(), {})
    assert fresh['auth_mode'] == 'accounts'
    legacy = {key: value for key, value in fresh.items() if key not in ('auth_mode', 'registration', 'history_days')}
    assert configure(args(), legacy)['auth_mode'] == 'legacy'
    upgraded = configure(args(auth_mode='accounts', registration='closed', history_days=90), legacy)
    assert upgraded['auth_mode'] == 'accounts'
    assert upgraded['registration'] == 'closed'
    assert upgraded['history_days'] == 90
    private_write(tmp_path / 'server.json', json.dumps(upgraded))
    command, env = runtime(tmp_path, 'server')
    assert env['PANGOLIN_AUTH_MODE'] == 'accounts'
    assert env['PANGOLIN_DATABASE'] == str(tmp_path / 'state/server.sqlite3')
    assert env['PANGOLIN_REGISTRATION'] == 'closed'
    assert env['PANGOLIN_HISTORY_DAYS'] == '90'
    assert '--workers' in command and command[command.index('--workers') + 1] == '1'


def test_local_account_admin_create_reset_import_backup(tmp_path):
    import sys
    from server.storage import Store
    from server.accounts import PASSWORDS
    database = tmp_path / 'server.sqlite3'
    password = tmp_path / 'password'
    password.write_text('private test password with spaces')
    command = [sys.executable, '-m', 'server.admin']
    def invoke(action, *arguments):
        return subprocess.run([*command, action, '--database', str(database), *arguments], check=True, capture_output=True, text=True)
    invoke('create-user', '--email', 'test@example.com', '--password-file', str(password))
    store = Store(database)
    user = store.user('test@example.com')
    token, _ = store.login(user['id'])
    device = store.bind(user['id'], 'test')
    invoke('import-device', '--email', 'test@example.com', '--device', 'devbox')
    assert store.device(user['id'], 'devbox')['revoked'] == 1
    backup = tmp_path / 'backup.sqlite3'
    invoke('backup', '--output', str(backup))
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    restored = Store(backup)
    assert restored.user('test@example.com')['id'] == user['id']
    restored.close()
    password.write_text('new private test password')
    invoke('reset-password', '--email', 'test@example.com', '--password-file', str(password))
    assert store.authenticate(token) is None
    assert store.device_auth(device['device'], device['device_token']) is None
    assert PASSWORDS.verify(store.user('test@example.com')['password_hash'], 'new private test password')
    store.close()
