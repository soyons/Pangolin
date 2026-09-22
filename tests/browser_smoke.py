"""Optional full-stack check: pip install playwright; python tests/browser_smoke.py.

Uses a local Chrome/Chromium or Playwright's installed Chromium, and a fake CLI.
No model calls, system services, or external relay are involved.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
USER, DEVICE = 'browser-smoke-user-' + 'u' * 32, 'browser-smoke-device-' + 'd' * 32
CLI = '''import os, sys, tty
tty.setraw(sys.stdin.fileno())
selected = 0
def write(text):
    os.write(1, text.encode())
def menu():
    write('\\x1b[2J\\x1b[HDo you want to proceed?\\r\\n')
    for i, label in enumerate(['Yes', 'Yes, allow all edits during this session', 'No']):
        write(('> ' if i == selected else '  ') + str(i + 1) + '. ' + label + '\\r\\n')
menu()
while True:
    key = os.read(0, 1)
    if key == b'\\x1b':
        arrow = os.read(0, 2)
        selected = max(0, min(2, selected + (1 if arrow == b'[B' else -1)))
        menu()
    elif key == b'\\r':
        write('\\x1b[2J\\x1b[HDecision: ' + ['Yes', 'Always', 'No'][selected] + '\\r\\nTask: ')
        break
buffer = b''
while True:
    key = os.read(0, 1)
    if key == b'\\r':
        write('\\r\\nCLI: ' + buffer.decode(errors='replace') + '\\r\\nTask: ')
        buffer = b''
    elif key == b'\\x03':
        write('\\r\\nInterrupted\\r\\nTask: ')
        buffer = b''
    else:
        buffer += key
'''


def wait_until(callback, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if callback():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(.1)
    raise AssertionError('Timed out waiting for local relay/agent')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent', choices=['python', 'node', 'migration'], default='node',
                        help='migration starts Python then restarts as Node with the same database')
    agent_mode = parser.parse_args().agent
    processes = []
    tmux_socket = 'pangolin-browser-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='pangolin-browser-') as directory:
        temp = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        cli = temp / 'claude'
        cli.write_text('#!' + sys.executable + '\n' + CLI)
        cli.chmod(0o755)
        config = temp / 'agent.yaml'
        settings = {'tmux_socket': tmux_socket, 'state_path': str(temp / 'history.sqlite3'),
                    'projects': {'demo': {'path': str(temp), 'agents': ['claude']}}}
        config.write_text(json.dumps(settings))
        (temp / 'agent.json').write_text(json.dumps({**settings, 'relay': url, 'device': 'test',
            'device_token': DEVICE, 'path': str(temp) + os.pathsep + os.environ['PATH']}))
        headers = {'Authorization': 'Bearer ' + USER}
        errors = []
        with (temp / 'process.log').open('w+') as log:
            try:
                env = {**os.environ, 'USER_TOKEN': USER, 'DEVICE_TOKENS': 'test:' + DEVICE,
                       'PANGOLIN_AUTH_MODE': 'legacy',
                       'DEVICE_ID': 'test', 'DEVICE_TOKEN': DEVICE, 'AGENT_CONFIG': str(config),
                       'RELAY_WS_URL': f'ws://127.0.0.1:{port}/ws/agent',
                       'PATH': str(temp) + os.pathsep + os.environ['PATH']}
                processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server.main:app',
                                                   '--host', '127.0.0.1', '--port', str(port)],
                                                  cwd=ROOT, env=env, stdout=log, stderr=log))
                wait_until(lambda: httpx.get(url + '/healthz').status_code == 200)
                node_command = ['node', str(ROOT / 'packages/agent/bin/pangolin-agent.js'), 'run', '--prefix', str(temp)]
                agent_command = node_command if agent_mode == 'node' else [sys.executable, '-m', 'agent.main']
                processes.append(subprocess.Popen(agent_command,
                                                  cwd=ROOT, env=env, stdout=log, stderr=log))
                wait_until(lambda: httpx.get(url + '/api/machines', headers=headers).json()[0]['online'])
                with sync_playwright() as playwright:
                    chrome = os.environ.get('PANGOLIN_TEST_BROWSER') or shutil.which('google-chrome') or shutil.which('chromium')
                    browser = playwright.chromium.launch(executable_path=chrome, args=['--no-sandbox'])
                    page = browser.new_page(viewport={'width': 1280, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.locator('#token').fill(USER)
                    page.locator('#connect').click()
                    expect(page.locator('#project')).to_have_value('demo')
                    expect(page.locator('#agent')).to_have_value('claude')
                    page.locator('#create').click()
                    expect(page.locator('#interaction')).to_be_visible(timeout=10000)
                    page.locator('#choices button').filter(has_text='No').click()
                    expect(page.locator('#output')).to_contain_text('Decision: No', timeout=10000)
                    expect(page.locator('#messages .interaction')).to_contain_text('选择：No')
                    expect(page.locator('#interaction')).to_be_hidden()

                    # Simulate a response lost AFTER the server sent the prompt.
                    # Clicking send again must reuse the same ID and produce one event.
                    dropped = []
                    def lose_first_response(route):
                        response = route.fetch()
                        if not dropped:
                            dropped.append(True)
                            route.abort()
                        else:
                            route.fulfill(response=response)
                    page.route('**/send', lose_first_response)
                    message = '<script>window.injected=true</script> 请检查'
                    page.locator('#prompt').fill(message)
                    page.locator('#send').click()
                    expect(page.locator('#status')).to_have_class('error', timeout=10000)
                    page.locator('#send').click()
                    expect(page.locator('#prompt')).to_have_value('')
                    expect(page.locator('#messages .user')).to_have_count(1)
                    expect(page.locator('#messages .user')).to_contain_text(message)
                    expect(page.locator('#output')).to_contain_text('CLI: ' + message, timeout=10000)
                    assert page.evaluate('window.injected') is None

                    # A runtime upgrade must preserve live tmux sessions and message history.
                    if agent_mode == 'migration':
                        processes[-1].terminate()
                        processes[-1].wait(timeout=5)
                        wait_until(lambda: not httpx.get(url + '/api/machines', headers=headers).json()[0]['online'])
                        processes.append(subprocess.Popen(node_command, cwd=ROOT, env=env, stdout=log, stderr=log))
                        wait_until(lambda: httpx.get(url + '/api/machines', headers=headers).json()[0]['online'])
                    # Page reload restores local history after authentication.
                    page.reload()
                    expect(page.locator('#token')).to_have_value('')
                    page.locator('#token').fill(USER)
                    page.locator('#connect').click()
                    expect(page.locator('#messages .user')).to_have_count(1)
                    expect(page.locator('#messages .interaction')).to_have_count(1)
                    page.locator('#keyboard summary').click()
                    page.on('dialog', lambda dialog: dialog.accept())
                    page.locator('[data-key="C-c"]').click()
                    expect(page.locator('#output')).to_contain_text('Interrupted', timeout=10000)
                    assert page.locator('#output').inner_text().rstrip() == page.locator('#output').inner_text()

                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path='/tmp/pangolin-mobile.png', full_page=True)
                    page.set_viewport_size({'width': 1280, 'height': 1000})
                    page.screenshot(path='/tmp/pangolin-desktop.png', full_page=True)
                    page.locator('#stop').click()
                    expect(page.locator('#session option')).to_have_count(0)
                    expect(page.locator('#send')).to_be_disabled()
                    assert not errors, errors
                    browser.close()
                print(f'Browser smoke passed ({agent_mode}): connect, approve, send/retry, restore, interrupt, mobile, stop.')
            except Exception:
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                subprocess.run(['tmux', '-L', tmux_socket, 'kill-server'], capture_output=True)


if __name__ == '__main__':
    main()
