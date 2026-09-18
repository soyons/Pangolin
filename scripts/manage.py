"""Installed service entrypoint and small management CLI (stdlib only)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def command(prefix, action, role, platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        label = 'gui/' + str(os.getuid()) + '/io.pangolin.' + role
        plist = str(Path.home() / 'Library/LaunchAgents' / ('io.pangolin.' + role + '.plist'))
        return {'status': ['launchctl', 'print', label],
                'stop': ['launchctl', 'bootout', label],
                'start': ['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), plist],
                'logs': ['tail', '-n', '100', '-f', str(prefix / (role + '.log'))]}[action]
    mode = [] if os.geteuid() == 0 else ['--user']
    if action == 'logs':
        return ['journalctl', *mode, '-u', 'pangolin-' + role, '-n', '100', '-f']
    return ['systemctl', *mode, action, 'pangolin-' + role]


def runtime(prefix, role):
    config = json.loads((prefix / (role + '.json')).read_text())
    env = dict(os.environ)
    env['PATH'] = config['path']
    if role == 'server':
        env['USER_TOKEN'] = config['user_token']
        env['DEVICE_TOKENS'] = config['device'] + ':' + config['device_token']
        args = ['-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1',
                '--port', str(config['port']), '--workers', '1', '--ws-max-size', '131072']
    else:
        env.pop('USER_TOKEN', None)
        env.pop('DEVICE_TOKENS', None)
        env.update(config.get('model_environment', {}))
        env.update(RELAY_WS_URL=config['relay'], DEVICE_ID=config['device'], DEVICE_TOKEN=config['device_token'])
        # JSON is a YAML subset; use JSON encoding to preserve arbitrary project paths safely.
        path = prefix / 'agent.local.yaml'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'tmux_socket': config.get('tmux_socket', 'pangolin'),
                       'state_path': str(prefix / 'state/sessions.sqlite3'), 'projects': {'example': {
                'path': config['project'], 'agents': config['agents']}}}, f)
        env['AGENT_CONFIG'] = str(path)
        args = ['-m', 'agent.main']
    return args, env


def main():
    p = argparse.ArgumentParser(description='Pangolin service manager')
    p.add_argument('--prefix', type=Path, required=True)
    p.add_argument('action', choices=['run', 'status', 'start', 'stop', 'logs', 'credentials'])
    p.add_argument('role', choices=['server', 'agent'], nargs='?')
    args = p.parse_args()
    prefix = args.prefix.resolve()
    if args.action == 'credentials':
        config = json.loads((prefix / 'server.json').read_text())
        print('浏览器 USER_TOKEN: ' + config['user_token'])
        print('设备 ID: ' + config['device'])
        print('设备 DEVICE_TOKEN: ' + config['device_token'])
        return
    role = args.role
    if role is None:
        roles = [r for r in ('server', 'agent') if (prefix / (r + '.json')).exists()]
        if len(roles) != 1:
            p.error('Specify server or agent')
        role = roles[0]
    if not (prefix / (role + '.json')).exists():
        p.error(role + ' is not installed in this prefix')
    if args.action == 'run':
        command_args, env = runtime(prefix, role)
        os.chdir(prefix / 'app')
        os.umask(0o077)
        python = str(prefix / 'venv/bin/python')
        os.execve(python, [python, *command_args], env)
    else:
        raise SystemExit(subprocess.call(command(prefix, args.action, role)))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        print('Pangolin: ' + str(exc), file=sys.stderr)
        sys.exit(1)
