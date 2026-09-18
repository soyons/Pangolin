"""Install into a private user directory; keep configuration separate from code."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Atomic replacement, including when updating an existing configuration.
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content.encode() if isinstance(content, str) else content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def ask(label, default=None, secret=False):
    prompt = label + (f' [{default}]' if default else '') + ': '
    try:
        with open('/dev/tty', 'r+') as tty:
            if secret:
                value = getpass.getpass(prompt, stream=tty)
            else:
                tty.write(prompt)
                tty.flush()
                value = tty.readline().strip()
    except OSError:
        raise ValueError(f'{label}: no interactive terminal; supply the corresponding option or token file')
    return value or default or ''


def relay_url(value):
    p = urlsplit(value)
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Relay URL must have a hostname and no credentials/query/fragment')
    # Validate ports even though urlsplit itself defers this check.
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError('Invalid relay port')
    scheme = {'https': 'wss', 'http': 'ws', 'wss': 'wss', 'ws': 'ws'}.get(p.scheme)
    if not scheme or (scheme == 'ws' and p.hostname not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Remote relay requires https:// or wss:// (http allowed only on loopback)')
    if p.path not in ('', '/', '/ws/agent'):
        raise ValueError('Relay URL must be the site root or /ws/agent')
    return urlunsplit((scheme, p.netloc, '/ws/agent', '', ''))


def configure(args, existing):
    config = dict(existing)
    config['role'] = args.role
    config['path'] = os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    if args.role == 'server':
        if args.device and existing and args.device != existing['device']:
            raise ValueError('Device ID already configured; refusing to silently change paired device')
        config.setdefault('device', args.device or 'devbox')
        config.setdefault('user_token', secrets.token_urlsafe(32))
        config.setdefault('device_token', secrets.token_urlsafe(32))
        config.setdefault('port', args.port or 8000)
        if args.port:
            config['port'] = args.port
        if args.ip:
            address = ipaddress.ip_address(args.ip)
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError('--ip requires a public server IP')
            config['ip'] = str(address)
            config.pop('domain', None)
        if args.domain:
            config.pop('ip', None)
            domain = args.domain.lower()
            if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
                raise ValueError('Domain must be a DNS hostname such as agent.example.com')
            config['domain'] = domain
    else:
        config['relay'] = relay_url(args.relay or config.get('relay') or ask('Relay 地址', 'https://agent.example.com'))
        config['device'] = args.device or config.get('device') or ask('设备 ID', 'devbox')
        token = (Path(args.token_file).read_text().strip() if args.token_file else
                 config.get('device_token') or ask('设备 Token（从服务端查看）', secret=True))
        if len(token) < 32 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            raise ValueError('Device token must be 32+ URL-safe random characters')
        config['device_token'] = token
        config.setdefault('model_environment', {})
        for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN'):
            if os.environ.get(key):
                config['model_environment'][key] = os.environ[key]
        project = Path(args.project or config.get('project') or ask('项目绝对路径')).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError('Project path is not a directory')
        config['project'] = str(project)
        config['agents'] = [name for name in ('codex', 'claude') if shutil.which(name)]
        if not config['agents']:
            raise ValueError('Install and sign in to codex or claude first, then rerun')
        if not shutil.which('tmux'):
            raise ValueError('tmux is required; use install.sh to install prerequisites')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', config['device']):
        raise ValueError('Device ID must contain only letters, numbers, hyphens and underscores')
    return config


def systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def service_file(prefix, role, platform, system=False):
    command = [str(prefix / 'venv/bin/python'), str(prefix / 'app/scripts/manage.py'),
               '--prefix', str(prefix), 'run', role]
    if platform == 'darwin':
        return plistlib.dumps({'Label': 'io.pangolin.' + role, 'ProgramArguments': command,
                              'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                              'StandardOutPath': str(prefix / (role + '.log')),
                              'StandardErrorPath': str(prefix / (role + '.log'))})
    return ('[Unit]\nDescription=Pangolin ' + role + '\nAfter=network-online.target\n'
            '[Service]\nExecStart=' + ' '.join(map(systemd_quote, command)) + '\n'
            'Restart=on-failure\nRestartSec=5\nUMask=0077\n'
            + ('User=pangolin\nGroup=pangolin\n' if system else '')
            + '[Install]\nWantedBy=' + ('multi-user.target' if system else 'default.target') + '\n')


def install_service(prefix, role):
    if sys.platform == 'darwin':
        target = Path.home() / 'Library/LaunchAgents' / ('io.pangolin.' + role + '.plist')
        private_write(target, service_file(prefix, role, sys.platform))
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/io.pangolin.' + role], capture_output=True)
        run('launchctl', 'bootstrap', domain, target)
    elif sys.platform.startswith('linux'):
        system = os.geteuid() == 0
        directory = Path('/etc/systemd/system') if system else Path.home() / '.config/systemd/user'
        target = directory / ('pangolin-' + role + '.service')
        private_write(target, service_file(prefix, role, sys.platform, system=system))
        mode = [] if system else ['--user']
        run('systemctl', *mode, 'daemon-reload')
        run('systemctl', *mode, 'enable', 'pangolin-' + role)
        run('systemctl', *mode, 'restart', 'pangolin-' + role)
    else:
        raise ValueError('Automatic background service supports macOS and systemd Linux only; use --no-start')


def admin(*args):
    run(*(([] if os.geteuid() == 0 else ['sudo']) + list(args)))


def setup_https(prefix, config):
    """Add a dedicated site to existing Caddy configuration, without replacing other sites."""
    if not sys.platform.startswith('linux') or not shutil.which('apt-get'):
        raise ValueError('--domain automatic HTTPS currently supports Ubuntu/Debian only')
    if not shutil.which('caddy'):
        admin('apt-get', 'update')
        admin('apt-get', 'install', '-y', 'caddy')
    target = Path('/etc/caddy/Caddyfile')
    # Read via sudo too, for hosts with restrictive Caddy configuration permissions.
    cmd = ([] if os.geteuid() == 0 else ['sudo']) + ['cat', str(target)]
    old = subprocess.check_output(cmd, text=True)
    start, end = '# BEGIN PANGOLIN', '# END PANGOLIN'
    if start in old:
        if old.count(start) != 1 or old.count(end) != 1:
            raise ValueError('Unrecognized Pangolin block in Caddyfile; review it manually')
        old_without_block = re.sub(re.escape(start) + r'.*?' + re.escape(end), '', old, flags=re.S)
    else:
        old_without_block = old
    if config['domain'] in old_without_block:
        raise ValueError('Domain already appears in Caddyfile; use existing reverse proxy or edit it manually')
    text = (old_without_block.rstrip() + '\n\n' + start + '\n' + config['domain'] + ' {\n'
            '    reverse_proxy 127.0.0.1:' + str(config['port']) + '\n}\n' + end + '\n')
    candidate = prefix / 'Caddyfile.candidate'
    private_write(candidate, text)
    admin('caddy', 'validate', '--config', str(candidate), '--adapter', 'caddyfile')
    backup = '/etc/caddy/Caddyfile.pangolin-backup-' + str(time.time_ns())
    admin('cp', '-p', str(target), backup)
    admin('install', '-m', '644', str(candidate), str(target))
    try:
        admin('systemctl', 'enable', '--now', 'caddy')
        admin('systemctl', 'reload', 'caddy')
    except subprocess.CalledProcessError:
        admin('cp', '-p', backup, str(target))
        raise
    finally:
        candidate.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description='Pangolin one-command installer')
    p.add_argument('role', choices=['server', 'agent'])
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--prefix', type=Path, default=(Path('/opt/pangolin') if os.geteuid() == 0 else Path.home() / '.local/share/pangolin'))
    p.add_argument('--ip', help='Ubuntu 22.04+/Debian 12+: public IP, automatic HTTPS without a domain')
    p.add_argument('--domain', help='Ubuntu/Debian: configure Caddy HTTPS for this domain')
    p.add_argument('--relay', help='Relay HTTPS URL; agent only')
    p.add_argument('--device', help='Device ID; default devbox')
    p.add_argument('--project', help='Local project directory; agent only')
    p.add_argument('--token-file', help='Read device token from a private file instead of a hidden prompt')
    p.add_argument('--port', type=int)
    p.add_argument('--no-start', action='store_true', help='Install only; do not register services or change Caddy')
    args = p.parse_args()
    if args.ip and args.domain:
        p.error('Use either --ip or --domain')
    if args.port is not None and not 1024 <= args.port <= 65535:
        p.error('--port must be 1024–65535')
    if args.role == 'agent' and (args.domain or args.ip or args.port):
        p.error('--domain/--ip/--port belong to server')
    if args.role == 'server' and (args.relay or args.project or args.token_file):
        p.error('--relay/--project/--token-file belong to agent')
    if os.geteuid() == 0 and args.role == 'agent':
        p.error('Run the agent as the user who owns the projects and Codex/Claude login, not root')
    prefix = args.prefix.expanduser().resolve()
    if any(c in str(prefix) for c in '\n\r\x00'):
        p.error('Installation path may not contain control characters')
    if prefix.exists() and any(prefix.iterdir()) and not (prefix / '.pangolin-install').exists():
        raise ValueError('Prefix is not a Pangolin installation; choose an empty directory')
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix.chmod(0o700)
    private_write(prefix / '.pangolin-install', '1\n')
    config_path = prefix / (args.role + '.json')
    existing = json.loads(config_path.read_text()) if config_path.exists() else {}
    config = configure(args, existing)
    if config.get('ip') and not args.no_start:
        if not sys.platform.startswith('linux') or not shutil.which('apt-get') or sys.version_info < (3, 10):
            raise ValueError('IP HTTPS supports Ubuntu 22.04+ / Debian 12+ with Python 3.10+')
    source = args.source.resolve()
    app = prefix / 'app'
    if source == app or app in source.parents or source in prefix.parents or source == prefix:
        raise ValueError('Install prefix must be separate from the source checkout')
    python = prefix / 'venv/bin/python'
    if not python.exists():
        run(sys.executable, '-m', 'venv', prefix / 'venv')
    run(python, '-m', 'pip', 'install', '-r', source / 'requirements.txt')
    for name in ('server', 'agent', 'web', 'scripts'):
        shutil.copytree(source / name, app / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '._*'))
    private_write(config_path, json.dumps(config, indent=2) + '\n')
    launcher = prefix / 'pangolin'
    private_write(launcher, '#!/bin/sh\nexec ' + ' '.join(shlex.quote(str(x)) for x in
                  (python, app / 'scripts/manage.py', '--prefix', prefix)) + ' "$@"\n')
    launcher.chmod(0o700)
    if not args.no_start:
        if os.geteuid() == 0 and sys.platform.startswith('linux'):
            try:
                account = pwd.getpwnam('pangolin')
            except KeyError:
                run('useradd', '--system', '--user-group', '--create-home', '--home-dir', '/var/lib/pangolin', '--shell', '/usr/sbin/nologin', 'pangolin')
                account = pwd.getpwnam('pangolin')
            if account.pw_uid == 0:
                raise ValueError('Refusing to use a privileged pangolin service account')
            for directory, dirs, files in os.walk(prefix):
                for item in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
                    os.chown(item, 0, account.pw_gid, follow_symlinks=False)
                    if not item.is_symlink():
                        item.chmod(0o750 if item.is_dir() or item.stat().st_mode & 0o111 else 0o640)
        install_service(prefix, args.role)
        if args.role == 'server':
            for _ in range(30):
                try:
                    with urlopen(f'http://127.0.0.1:{config["port"]}/healthz', timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.3)
            else:
                raise ValueError('Service registered but health check failed; use pangolin logs server')
            if config.get('domain'):
                setup_https(prefix, config)
            elif config.get('ip'):
                print('配置 IP HTTPS：公网 TCP 80/443 必须可达，首次运行请按 Certbot 提示阅读并同意证书条款。', flush=True)
                admin(sys.executable, str(app / 'scripts/https_ip.py'), '--ip', config['ip'], '--port', str(config['port']))
    print('\n安装完成。管理命令：' + shlex.quote(str(launcher)))
    print('配置保存在权限受限的 ' + str(config_path))
    if args.no_start:
        print('尚未启动。运行：' + shlex.quote(str(launcher)) + ' run ' + args.role)
    if args.role == 'server':
        ip_host = ('[' + config['ip'] + ']' if ':' in config.get('ip', '') else config.get('ip'))
        site = 'https://' + (config.get('domain') or ip_host) if config.get('domain') or ip_host else f'http://127.0.0.1:{config["port"]}'
        print(('配置的访问地址（尚未启用 HTTPS）：' if args.no_start else '访问地址：') + site)
        print('查看浏览器 Token / 设备 Token：' + shlex.quote(str(launcher)) + ' credentials')
        if not config.get('domain') and not config.get('ip'):
            print('当前只监听本机；公网使用需加 --ip、--domain 或接入已有 HTTPS 反向代理。')
    else:
        print('网页项目名：example。安装完成不代表已连接，使用 status agent / logs agent 查看连接状态。')
    if sys.platform.startswith('linux') and not args.no_start and os.geteuid() != 0:
        print('若要退出 SSH 后持续运行并开机启动：sudo loginctl enable-linger ' + shlex.quote(getpass.getuser()))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('安装未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
