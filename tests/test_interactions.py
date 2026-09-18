import json
import os
import shutil
import stat
import subprocess
import time
import uuid

import pytest

from agent.interactions import choice_keys, detect_interaction, screen_id
from agent.main import Sessions
from agent.state import SessionError, SessionStore


CODEX = '''Would you like to run the following command?
  $ npm test
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with npm test (p)
  3. No, and tell Codex what to do differently (esc)
Press enter to confirm or esc to cancel'''
CLAUDE = '''Do you want to proceed?
❯ 1. Yes
  2. Yes, allow all edits during this session
  3. No
Esc to cancel · Tab to amend'''


@pytest.mark.parametrize('screen', [CODEX, CLAUDE, CODEX.replace('›', '>'),
                                    '是否允许执行？\n❯ 1. 允许一次\n  2. 拒绝'])
def test_common_approval_menus(screen):
    interaction = detect_interaction(screen)
    assert interaction['kind'] == 'choice'
    assert choice_keys(interaction, '2') == ['Down', 'Enter']
    assert choice_keys(interaction, '1') == ['Enter']
    with pytest.raises(ValueError):
        choice_keys(interaction, '99')


def test_selection_from_current_option_and_confirmation():
    screen = CLAUDE.replace('❯ 1.', '  1.').replace('  3.', '❯ 3.')
    assert choice_keys(detect_interaction(screen), '1') == ['Up', 'Up', 'Enter']
    assert choice_keys(detect_interaction('Trust this directory? [y/N]'), 'no') == ['n', 'Enter']
    assert choice_keys(detect_interaction('Continue? [yes/no]'), 'yes') == ['yes', 'Enter']
    assert choice_keys(detect_interaction('Press Enter to continue'), 'continue') == ['Enter']


@pytest.mark.parametrize('screen', ['1. A list\n2. Another item',
                                    'Select a tool:\n1. Test\n2. Build',
                                    CLAUDE + '\n' + 'Finished\n' * 8,
                                    'Which option?\n❯ 1. One\n❯ 2. Two'])
def test_does_not_guess_unmarked_or_historical_menus(screen):
    assert detect_interaction(screen) is None


@pytest.fixture
def fake_sessions(tmp_path, monkeypatch):
    sessions = Sessions({'state_path': str(tmp_path / 'state/history.sqlite3')})
    sessions.screen = CLAUDE
    sessions.calls = []

    def tmux(*args, **kwargs):
        sessions.calls.append(args)
        return subprocess.CompletedProcess(args, 0, sessions.screen if args[0] == 'capture-pane' else '', '')

    monkeypatch.setattr(sessions, 'tmux', tmux)
    yield sessions
    sessions.store.db.close()


SID = 'rp-' + 'a' * 32


def test_history_is_separate_persistent_and_retry_safe(fake_sessions, tmp_path):
    sessions = fake_sessions
    message = {'action': 'session.send', 'session': SID, 'message': '请修复这个问题 <script>', 'request_id': 'b' * 32}
    sent = sessions.handle(message)
    assert sessions.handle(message) == sent
    assert sum(c[0] == 'paste-buffer' for c in sessions.calls) == 1
    state = sessions.handle({'action': 'session.state', 'session': SID})
    assert state['terminal']['source'] == 'terminal'
    assert state['terminal']['text'] == CLAUDE
    assert [e['text'] for e in state['events']] == [message['message']]
    assert state['events'][0]['source'] == 'user'
    assert state['events'][0]['status'] == 'sent'
    assert sessions.handle({'action': 'session.state', 'session': SID, 'after': state['cursor']})['events'] == []
    path = tmp_path / 'state/history.sqlite3'
    restored = SessionStore(path)
    assert restored.events(SID) == state['events']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert restored.previous(SID, message['request_id'], json.dumps(
        {k: v for k, v in message.items() if k != 'request_id'}, sort_keys=True)) == sent
    restored.db.close()
    with pytest.raises(SessionError):
        sessions.handle({**message, 'message': 'different'})


def test_click_rechecks_screen_and_never_sends_stale_approval(fake_sessions):
    sessions = fake_sessions
    command = {'action': 'session.input', 'session': SID, 'screen_id': screen_id(CLAUDE),
               'choice': '3', 'request_id': 'c' * 32}
    sessions.screen = 'A different permission request'
    with pytest.raises(SessionError, match='已变化'):
        sessions.handle(command)
    assert not any(c[0] == 'send-keys' for c in sessions.calls)
    sessions.screen = CLAUDE
    result = sessions.handle(command)
    assert sessions.calls[-1][-3:] == ('Down', 'Down', 'Enter')
    sessions.screen = 'Done'
    assert sessions.handle(command) == result  # Retrying a successful click is a no-op.
    assert sum(c[0] == 'send-keys' for c in sessions.calls) == 1
    event = sessions.store.events(SID)[0]
    assert event['source'] == 'interaction' and event['text'] == '选择：No'


@pytest.mark.parametrize('input', [{'key': 'C-z'}, {'key': 'Enter; sh'}, {'key': '\x03'},
                                  {'key': 'Up', 'choice': '1'}, {}, {'choice': '7'}])
def test_input_is_allowlisted(fake_sessions, input):
    with pytest.raises(ValueError):
        fake_sessions.handle({'action': 'session.input', 'session': SID,
                              'screen_id': screen_id(CLAUDE), **input})
    assert not any(c[0] == 'send-keys' for c in fake_sessions.calls)


def test_partial_failure_cannot_repeat_input(fake_sessions, monkeypatch):
    sessions = fake_sessions
    tmux = sessions.tmux

    def fail(*args, **kwargs):
        if args[0] == 'send-keys':
            raise ValueError('lost confirmation')
        return tmux(*args, **kwargs)

    monkeypatch.setattr(sessions, 'tmux', fail)
    msg = {'action': 'session.send', 'session': SID, 'message': 'task', 'request_id': 'f' * 32}
    with pytest.raises(ValueError):
        sessions.handle(msg)
    with pytest.raises(SessionError, match='不确定'):
        sessions.handle(msg)
    assert sum(c[0] == 'paste-buffer' for c in sessions.calls) == 1
    assert sessions.store.events(SID)[0]['status'] == 'uncertain'


def test_large_unicode_history_fits_websocket_frame_and_paginates(fake_sessions):
    sessions = fake_sessions
    sessions.screen = '文' * 12000
    for _ in range(3):
        sessions.handle({'action': 'session.send', 'session': SID, 'message': '🤖' * 16000})
    cursor, events = 0, []
    for _ in range(3):
        result = sessions.handle({'action': 'session.state', 'session': SID, 'after': cursor})
        assert len(json.dumps({'id': 'a' * 32, 'ok': True, 'result': result}, ensure_ascii=False).encode()) < 131072
        events.extend(result['events'])
        cursor = result['cursor']
    assert len(events) == 3 and len({e['id'] for e in events}) == 3


def test_snapshot_omits_empty_terminal_rows(fake_sessions):
    fake_sessions.screen = 'Visible output\nPrompt: \n' + '\n' * 38
    state = fake_sessions.handle({'action': 'session.state', 'session': SID})
    assert state['terminal']['text'] == 'Visible output\nPrompt:'
    assert state['screen_id'] == screen_id(fake_sessions.screen)


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux not installed')
def test_real_tmux_menu_selection_and_history(tmp_path, monkeypatch):
    import sys
    cli = tmp_path / 'claude'
    cli.write_text('#!' + sys.executable + '\n' + '''import os, sys, termios, tty
tty.setraw(sys.stdin.fileno())
os.write(1, b'Do you want to proceed?\\r\\n> 1. Yes\\r\\n  2. No\\r\\n')
data = b''
while True:
    data += os.read(0, 1)
    if data.endswith(b'\\r'):
        os.write(1, b'\\x1b[2J\\x1b[HRECEIVED:' + data.hex().encode() + b'\\r\\n')
        data = b''
''')
    cli.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    sessions = Sessions({'tmux_socket': 'rp-interact-' + uuid.uuid4().hex,
                         'projects': {'example': {'path': str(tmp_path), 'agents': ['claude']}}})
    try:
        result = sessions.handle({'action': 'session.create', 'project': 'example', 'agent': 'claude'})
        sid = result['id']
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = sessions.handle({'action': 'session.state', 'session': sid})
            if state['interaction']:
                break
            time.sleep(.03)
        assert state['interaction']['kind'] == 'choice'
        sessions.handle({'action': 'session.input', 'session': sid,
                         'screen_id': state['screen_id'], 'choice': '2'})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = sessions.handle({'action': 'session.state', 'session': sid})
            if 'RECEIVED:' in state['terminal']['text']:
                break
            time.sleep(.03)
        assert 'RECEIVED:1b5b420d' in state['terminal']['text']  # Down + Enter, no prompt text.
        assert state['interaction'] is None
        assert state['events'][0]['source'] == 'interaction'
        sessions.handle({'action': 'session.stop', 'session': sid})
        assert sessions.store.events(sid) == []
    finally:
        sessions.tmux('kill-server', check=False)
        sessions.store.db.close()
