"""Full account workflow with a real relay, Node agent, tmux and two browser contexts."""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid

import httpx
from playwright.sync_api import sync_playwright, expect
from browser_smoke import CLI, ROOT, wait_until

EMAIL, PASSWORD = 'account-smoke@example.com', 'smoke password with spaces'


def main():
    processes = []
    tmux_socket = 'pangolin-account-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='pangolin-account-') as directory:
        temp = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        cli = temp / 'claude'; cli.write_text('#!' + sys.executable + '\n' + CLI); cli.chmod(0o755)
        password_file = temp / 'password'; password_file.write_text(PASSWORD); password_file.chmod(0o600)
        env = {**os.environ, 'PANGOLIN_AUTH_MODE': 'accounts', 'PANGOLIN_DATABASE': str(temp / 'server.sqlite3'),
               'PANGOLIN_PUBLIC_URL': url, 'PANGOLIN_REGISTRATION': 'open', 'PATH': str(temp) + os.pathsep + os.environ['PATH']}
        errors = []
        node_command = ['node', str(ROOT / 'packages/agent/bin/pangolin-agent.js'), 'run', '--prefix', str(temp)]
        with (temp / 'process.log').open('w+') as log:
            def start(command):
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
                processes.append(process)
                return process
            def stop(process):
                process.terminate(); process.wait(timeout=10)
            try:
                start([sys.executable, '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1', '--port', str(port)])
                wait_until(lambda: httpx.get(url + '/healthz').status_code == 200)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=os.environ.get('PANGOLIN_TEST_BROWSER') or shutil.which('google-chrome') or shutil.which('chromium'), args=['--no-sandbox'])
                    context = browser.new_context(viewport={'width': 1280, 'height': 1000})
                    page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url); page.locator('#email').fill(EMAIL); page.locator('#password').fill(PASSWORD); page.locator('#register').click()
                    expect(page.locator('#account-email')).to_have_text(EMAIL)
                    # Use the real client login/configuration API; verify the stored config contains no password.
                    login = '''import { configure, saveConfig } from './packages/agent/src/config.js';
const [relay,prefix,email,passwordFile,socket] = process.argv.slice(1);
const config=await configure({server:relay,email,project:prefix,'password-file':passwordFile},{tmux_socket:socket});
saveConfig(prefix,config);
'''
                    subprocess.run(['node', '--input-type=module', '-e', login, url, str(temp), EMAIL, str(password_file), tmux_socket], cwd=ROOT, env=env, check=True)
                    saved = json.loads((temp / 'agent.json').read_text())
                    assert PASSWORD not in (temp / 'agent.json').read_text()
                    agent = start(node_command)
                    expect(page.locator('#device')).to_contain_text('在线', timeout=15000)
                    expect(page.locator('#project')).to_have_value('example')
                    page.locator('#agent').select_option('claude')
                    page.locator('#create').click()
                    expect(page.locator('#interaction')).to_be_visible(timeout=15000)
                    page.locator('#choices button').filter(has_text='No').click()
                    expect(page.locator('#output')).to_contain_text('Decision: No', timeout=15000)
                    expect(page.locator('#messages .interaction')).to_contain_text('选择：No')
                    dropped = []
                    def lose_first_response(route):
                        response = route.fetch()
                        if not dropped: dropped.append(True); route.abort()
                        else: route.fulfill(response=response)
                    page.route('**/send', lose_first_response)
                    message = '<script>window.injected=true</script> 请同步我的对话'
                    page.locator('#prompt').fill(message); page.locator('#send').click()
                    expect(page.locator('#status')).to_have_class('error')
                    page.locator('#send').click()
                    expect(page.locator('#messages .user')).to_have_count(1, timeout=15000)
                    expect(page.locator('#output')).to_contain_text(message)
                    assert page.evaluate('window.injected') is None
                    page.reload()
                    expect(page.locator('#account-email')).to_have_text(EMAIL)
                    expect(page.locator('#messages .user')).to_have_count(1, timeout=15000)
                    # A second browser with its own cookie session sees the same conversation.
                    second = browser.new_context(viewport={'width': 390, 'height': 844})
                    phone = second.new_page(); phone.on('pageerror', lambda error: errors.append(str(error)))
                    phone.goto(url); phone.locator('#email').fill(EMAIL); phone.locator('#password').fill(PASSWORD); phone.locator('#login').click()
                    expect(phone.locator('#messages .user')).to_contain_text(message, timeout=15000)
                    assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    # Cached history remains readable after the agent leaves; controls become unavailable.
                    stop(agent)
                    expect(phone.locator('#device')).to_contain_text('离线', timeout=15000)
                    expect(phone.locator('#send')).to_be_disabled()
                    phone.reload(); expect(phone.locator('#messages .user')).to_contain_text(message, timeout=15000)
                    expect(phone.locator('#output')).to_contain_text(message)
                    # A real local queue records work while disconnected, then uploads on reconnect.
                    offline = '''import { readConfig,runtimeConfig } from './packages/agent/src/config.js';
import { Sessions } from './packages/agent/src/sessions.js';
const prefix=process.argv[1], sessions=new Sessions(runtimeConfig(prefix,readConfig(prefix)));
try { const [current]=await sessions.handle({action:'session.list'}); await sessions.handle({action:'session.send',session:current.id,message:'离线期间记录的任务'}); }
finally { sessions.store.close(); }
'''
                    subprocess.run(['node', '--input-type=module', '-e', offline, str(temp)], cwd=ROOT, env=env, check=True, stdout=log, stderr=log)
                    agent = start(node_command)
                    expect(phone.locator('#messages .user')).to_have_count(2, timeout=15000)
                    expect(page.locator('#messages .user')).to_have_count(2, timeout=15000)
                    # Stopping retains history; deleting offline leaves a tombstone applied when reconnecting.
                    page.on('dialog', lambda dialog: dialog.accept())
                    page.locator('#stop').click()
                    expect(page.locator('#session')).to_contain_text('已停止', timeout=15000)
                    expect(page.locator('#messages .user')).to_have_count(2)
                    stop(agent)
                    page.locator('#delete').click()
                    expect(page.locator('#session option')).to_have_count(0)
                    agent = start(node_command)
                    wait_until(lambda: httpx.get(url + '/healthz').status_code == 200)
                    expect(phone.locator('#session option')).to_have_count(0, timeout=15000)
                    def local_deleted():
                        with sqlite3.connect(temp / 'state/sessions.sqlite3') as db:
                            return db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0 and db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
                    wait_until(local_deleted)
                    page.locator('#revoke').click()
                    expect(page.locator('#device')).to_contain_text('已解绑', timeout=15000)
                    page.locator('#logout').click()
                    expect(page.locator('#account-login')).to_be_visible()
                    expect(page.locator('#messages .user')).to_have_count(0)
                    assert not errors, errors
                    browser.close()
                print('Account smoke passed: register, Node login, approval, retry, two browsers, offline history, queue recovery, stop/archive, delete, revoke, logout.')
            except Exception:
                log.flush(); log.seek(0); print(log.read(), file=sys.stderr); raise
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                subprocess.run(['tmux', '-L', tmux_socket, 'kill-server'], capture_output=True)


if __name__ == '__main__':
    main()
