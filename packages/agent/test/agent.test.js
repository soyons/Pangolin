import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { SessionStore } from '../src/store.js';
import { Sessions } from '../src/sessions.js';
import { choiceKeys, detectInteraction, screenId } from '../src/interactions.js';
import { canonical, id, privateWrite, SessionError, which } from '../src/utils.js';
import { configure, relayUrl, runtimeConfig } from '../src/config.js';
import { serviceFile } from '../src/service.js';
import { once } from 'node:events';
import { WebSocketServer } from 'ws';
import { runAgent } from '../src/client.js';

const MENU = 'Do you want to proceed?\n❯ 1. Yes\n  2. Yes, allow all edits during this session\n  3. No\nEsc to cancel · Tab to amend';
const SID = 'rp-' + 'a'.repeat(32);
const temporary = t => { const dir = mkdtempSync(join(tmpdir(), 'pangolin-node-')); t.after(() => rmSync(dir, { recursive: true, force: true })); return dir; };

test('Codex / Claude menus and text confirmations retain choice meanings', () => {
  for (const screen of [MENU, MENU.replace('❯', '›'), '是否允许执行？\n> 1. 允许一次\n  2. 拒绝']) {
    const interaction = detectInteraction(screen);
    assert.equal(interaction.kind, 'choice');
    assert.deepEqual(choiceKeys(interaction, '2'), ['Down', 'Enter']);
    assert.deepEqual(choiceKeys(interaction, '1'), ['Enter']);
  }
  assert.deepEqual(choiceKeys(detectInteraction(MENU.replace('❯ 1.', '  1.').replace('  3.', '❯ 3.')), '1'), ['Up', 'Up', 'Enter']);
  assert.deepEqual(choiceKeys(detectInteraction('Trust this folder? [y/N]'), 'no'), ['n', 'Enter']);
  assert.deepEqual(choiceKeys(detectInteraction('Continue? [yes/no]'), 'yes'), ['yes', 'Enter']);
  assert.deepEqual(choiceKeys(detectInteraction('Press Enter to continue'), 'continue'), ['Enter']);
  assert.throws(() => choiceKeys(detectInteraction(MENU), '99'));
  for (const screen of ['1. A list\n2. Another item', 'Select a tool:\n1. Test\n2. Build', MENU + '\nFinished'.repeat(8)]) {
    assert.equal(detectInteraction(screen), null);
  }
});

test('configuration validates relay, hides credentials from child environment and preserves pairing', async t => {
  const dir = temporary(t), bins = join(dir, 'bin'); mkdirSync(bins);
  for (const name of ['codex', 'tmux']) writeFileSync(join(bins, name), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const token = join(dir, 'token'); writeFileSync(token, 't'.repeat(40));
  const env = { PATH: bins, OPENAI_API_KEY: 'local-model-credential' };
  const config = await configure({ relay: 'https://example.com', device: 'devbox', project: dir, 'token-file': token }, {}, { env });
  assert.equal(config.relay, 'wss://example.com/ws/agent');
  assert.deepEqual(config.agents, ['codex']);
  const again = await configure({}, config, { env });
  assert.equal(again.device_token, config.device_token);
  const runtime = runtimeConfig(dir, config);
  assert.equal(runtime.environment.OPENAI_API_KEY, 'local-model-credential');
  assert.equal(runtime.environment.DEVICE_TOKEN, undefined);
  assert.equal(runtime.environment.USER_TOKEN, undefined);
  assert.equal(runtime.projects.example.path, dir);
  assert.equal(runtime.state_path, join(dir, 'state/sessions.sqlite3'));
  assert.equal(relayUrl('http://[::1]:8000'), 'ws://[::1]:8000/ws/agent');
  for (const url of ['http://8.8.8.8', 'https://user:pass@example.com', 'https://example.com/?token=oops', 'https://example.com/other', 'file:///etc/passwd', 'https://example.com:99999']) {
    assert.throws(() => relayUrl(url));
  }
});

test('private config and system services preserve paths without embedding secrets', t => {
  const dir = temporary(t), path = join(dir, 'agent.json');
  privateWrite(path, '{"token":"local"}'); privateWrite(path, '{"token":"updated"}');
  assert.equal(statSync(path).mode & 0o777, 0o600);
  const prefix = '/a path/$percent%/quote"&';
  const unit = serviceFile(prefix, 'linux');
  assert.ok(unit.includes('$$percent%%'));
  assert.ok(unit.includes('UMask=0077'));
  const plist = serviceFile(prefix, 'darwin');
  assert.ok(plist.includes('&quot;&amp;'));
  assert.ok(!unit.includes('TOKEN') && !plist.includes('TOKEN'));
});

function stubSessions(t, path) {
  const sessions = new Sessions({ state_path: path });
  sessions.screen = MENU; sessions.calls = [];
  sessions.tmux = async (...args) => { sessions.calls.push(args); return { stdout: args[0] === 'capture-pane' ? sessions.screen : '' }; };
  t.after(() => sessions.store.close());
  return sessions;
}

test('history survives restart, separates message source, and understands Python payload serialization', async t => {
  const path = join(temporary(t), 'history.sqlite3'), sessions = stubSessions(t, path);
  const message = { action: 'session.send', session: SID, message: '请修复 <script>', request_id: id() };
  const result = await sessions.handle(message);
  assert.deepEqual(await sessions.handle(message), result);
  assert.equal(sessions.calls.filter(call => call[0] === 'paste-buffer').length, 1);
  const state = await sessions.handle({ action: 'session.state', session: SID });
  assert.equal(state.events[0].source, 'user');
  assert.equal(state.events[0].text, message.message);
  assert.equal(state.terminal.source, 'terminal');
  assert.equal(state.terminal.text, MENU);
  assert.deepEqual((await sessions.handle({ action: 'session.state', session: SID, after: state.cursor })).events, []);
  const restored = new SessionStore(path);
  assert.deepEqual(restored.events(SID), state.events);
  assert.equal(statSync(path).mode & 0o777, 0o600);
  // Python's json.dumps uses escaped Unicode and spaces; comparison is semantic.
  const pythonPayload = '{"action": "session.send", "message": "\\u4f60", "session": "' + SID + '"}';
  restored.begin(SID, 'b'.repeat(32), pythonPayload, 'user', '你');
  restored.finish(SID, 'b'.repeat(32), { sent: true });
  assert.deepEqual(restored.previous(SID, 'b'.repeat(32), canonical({ action: 'session.send', message: '你', session: SID })), { sent: true });
  restored.close();
  await assert.rejects(sessions.handle({ ...message, message: 'different' }), SessionError);
});

test('stale approvals are rejected and successful retries cannot press twice', async t => {
  const sessions = stubSessions(t), msg = { action: 'session.input', session: SID, choice: '3', screen_id: screenId(MENU), request_id: id() };
  sessions.screen = 'A different approval';
  await assert.rejects(sessions.handle(msg), /已变化/u);
  assert.equal(sessions.calls.filter(call => call[0] === 'send-keys').length, 0);
  sessions.screen = MENU;
  const result = await sessions.handle(msg);
  assert.deepEqual(sessions.calls.at(-1).slice(-3), ['Down', 'Down', 'Enter']);
  sessions.screen = 'Done';
  assert.deepEqual(await sessions.handle(msg), result);
  assert.equal(sessions.calls.filter(call => call[0] === 'send-keys').length, 1);
  assert.equal(sessions.store.events(SID)[0].source, 'interaction');
});

test('two browsers cannot approve the same unchanged prompt using different request IDs', async t => {
  const sessions = stubSessions(t);
  const input = { action: 'session.input', session: SID, choice: '1', screen_id: screenId(MENU) };
  const results = await Promise.allSettled([
    sessions.handle({ ...input, request_id: id() }), sessions.handle({ ...input, request_id: id() })
  ]);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected');
  assert.match(results[1].reason.message, /已处理/);
  assert.equal(sessions.calls.filter(call => call[0] === 'send-keys').length, 1);
});

test('untrusted commands, keys, and terminal escapes cannot reach tmux input', async t => {
  const sessions = stubSessions(t);
  for (const msg of [
    { action: 'shell.exec' }, { action: 'session.stop', session: '*' },
    { action: 'session.create', project: '__proto__', agent: 'codex' },
    { action: 'session.send', session: SID, message: '\x03' },
    ...[{ key: 'C-z' }, { key: 'Enter; sh' }, { key: 'Up', choice: '1' }, {}, { choice: '7' }]
      .map(input => ({ action: 'session.input', session: SID, screen_id: screenId(MENU), ...input }))
  ]) await assert.rejects(sessions.handle(msg));
  assert.ok(!sessions.calls.some(call => ['send-keys', 'paste-buffer'].includes(call[0])));
});

test('partial failure records uncertainty and cannot repeat pasted text', async t => {
  const sessions = stubSessions(t), tmux = sessions.tmux;
  sessions.tmux = async (...args) => { if (args[0] === 'send-keys') throw Error('lost confirmation'); return tmux(...args); };
  const msg = { action: 'session.send', session: SID, message: 'task', request_id: id() };
  await assert.rejects(sessions.handle(msg));
  await assert.rejects(sessions.handle(msg), /不确定/u);
  assert.equal(sessions.calls.filter(call => call[0] === 'paste-buffer').length, 1);
  assert.equal(sessions.store.events(SID)[0].status, 'uncertain');
});

test('large Unicode records fit relay frames and empty pane rows are trimmed', async t => {
  const sessions = stubSessions(t);
  sessions.screen = '中'.repeat(12000) + '\n'.repeat(40);
  for (let i = 0; i < 3; i++) await sessions.handle({ action: 'session.send', session: SID, message: '🤖'.repeat(16000) });
  let cursor = 0, count = 0;
  for (let i = 0; i < 3; i++) {
    const state = await sessions.handle({ action: 'session.state', session: SID, after: cursor });
    assert.ok(Buffer.byteLength(JSON.stringify({ id: id(), ok: true, result: state })) < 131072);
    assert.ok(!state.terminal.text.endsWith('\n'));
    count += state.events.length; cursor = state.cursor;
  }
  assert.equal(count, 3);
});

test('real tmux lifecycle sends literal prompts and restores sessions', { skip: !which('tmux') }, async t => {
  const dir = temporary(t), executable = join(dir, 'codex');
  writeFileSync(executable, '#!/bin/sh\nexec cat\n', { mode: 0o755 });
  const sessions = new Sessions({ tmux_socket: 'pangolin-node-' + id(), environment: { ...process.env, PATH: dir + ':' + process.env.PATH },
    projects: { example: { path: dir, agents: ['codex'] } } });
  try {
    const created = await sessions.handle({ action: 'session.create', project: 'example', agent: 'codex' });
    assert.deepEqual(await sessions.handle({ action: 'session.list' }), [created]);
    const message = 'literal $(echo UNEXPECTED)\nsecond line';
    await sessions.handle({ action: 'session.send', session: created.id, message });
    let state;
    for (let i = 0; i < 40; i++) {
      state = await sessions.handle({ action: 'session.state', session: created.id });
      if (state.terminal.text.includes('second line')) break;
      await delay(50);
    }
    assert.ok(state.terminal.text.includes('$(echo UNEXPECTED)'));
    assert.equal(state.events[0].text, message);
    await sessions.handle({ action: 'session.stop', session: created.id });
    assert.deepEqual(await sessions.handle({ action: 'session.list' }), []);
    assert.deepEqual(sessions.store.events(created.id), []);
  } finally { await sessions.tmux('kill-server', { check: false }); sessions.store.close(); }
});

test('WebSocket authenticates, rejects malformed commands, reconnects, and stops cleanly', { timeout: 10000 }, async t => {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(server, 'listening');
  const controller = new AbortController(), logs = [], done = Promise.withResolvers();
  let connections = 0, running;
  t.after(async () => {
    controller.abort();
    await running;
    for (const socket of server.clients) socket.terminate();
    await new Promise(resolve => server.close(resolve));
  });
  server.on('connection', (socket, request) => {
    const count = ++connections;
    socket.on('message', raw => {
      try {
        assert.equal(request.headers.authorization, 'Bearer ' + 't'.repeat(40));
        assert.equal(request.headers['x-device-id'], 'test');
        const message = JSON.parse(raw.toString());
        if (message.type === 'heartbeat') {
          socket.send(JSON.stringify({ id: 'query', action: count === 1 ? 'shell.exec' : 'project.list' }));
        } else if (count === 1) {
          assert.equal(message.ok, false);
          assert.equal(message.id, 'query');
          assert.match(message.error, /Command rejected/);
          socket.send('null'); // Invalid envelopes close the connection rather than executing.
        } else {
          assert.deepEqual(message, { id: 'query', ok: true, result: [{ id: 'example', agents: ['claude'] }] });
          controller.abort(); done.resolve();
        }
      } catch (error) { controller.abort(); done.reject(error); }
    });
  });
  running = runAgent({ relay: 'ws://127.0.0.1:' + server.address().port, device: 'test', device_token: 't'.repeat(40),
    projects: { example: { path: '/private/project', agents: ['claude'] } } }, { signal: controller.signal, log: line => logs.push(line) });
  await Promise.race([done.promise, running.then(() => { if (connections < 2) throw Error('Agent stopped before reconnecting'); })]);
  await running;
  assert.equal(connections, 2);
  assert.ok(logs.some(line => line.includes('Connected')));
  assert.ok(!logs.join('\n').includes('t'.repeat(40)));
});

test('account login scopes the device and never stores the password', async t => {
  const dir = temporary(t), bins = join(dir, 'bin'); mkdirSync(bins);
  for (const name of ['codex', 'tmux']) writeFileSync(join(bins, name), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const password = ' password with significant spaces ', file = join(dir, 'password'); writeFileSync(file, password + '\n');
  const requests = [], account = 'd'.repeat(32), device = 'dev-' + id();
  const fetcher = async (url, options) => {
    requests.push({ url: String(url), options });
    assert.equal(options.redirect, 'error');
    assert.equal(JSON.parse(options.body).password, password);
    return { ok: true, json: async () => ({ user: { id: account, email: 'me@example.com' }, device, device_token: 't'.repeat(40) }) };
  };
  const config = await configure({ server: 'https://example.com', email: 'Me@Example.com', project: dir, 'password-file': file }, {}, { env: { PATH: bins }, fetcher });
  assert.equal(config.account_id, account); assert.equal(config.email, 'me@example.com');
  assert.equal(config.device, device); assert.equal(config.tmux_socket, 'pangolin-' + device);
  assert.ok(!JSON.stringify(config).includes(password));
  assert.equal(requests[0].url, 'https://example.com/api/auth/device-login');
  assert.equal(JSON.parse(requests[0].options.body).device_id, undefined);
  await configure({ login: true, 'password-file': file }, config, { env: { PATH: bins }, fetcher });
  assert.equal(JSON.parse(requests[1].options.body).expected_account, account);
  assert.equal(JSON.parse(requests[1].options.body).device_id, device);
  await assert.rejects(configure({ server: 'https://elsewhere.test' }, config, { env: { PATH: bins }, fetcher }), /其他服务端/);
});

test('outbox survives restart, updates statuses, and rejects acknowledgements beyond sent records', t => {
  const path = join(temporary(t), 'sync.sqlite3'), binding = { server: 'wss://example.com', account: 'd'.repeat(32), device: 'dev-test' };
  const first = new SessionStore(path);
  first.enableSync(binding); first.create(SID, 'example', 'claude');
  first.begin(SID, id(), '{}', 'user', 'pending message');
  const before = first.batch(); assert.equal(before.entries.length, 2);
  first.close();
  const second = new SessionStore(path); t.after(() => second.close()); second.enableSync(binding);
  const after = second.batch(); assert.equal(after.stream, before.stream);
  assert.equal(after.entries.at(-1).payload.status, 'uncertain');
  assert.throws(() => second.ack(999, after.entries.at(-1).seq));
  assert.equal(second.batch().entries.length, 3);
  second.ack(2, after.entries.at(-1).seq);
  assert.equal(second.batch().entries.length, 1);
  assert.equal(second.batch().entries[0].seq, 3);
});

test('deletion removes queued content without creating sequence gaps or permitting account reassignment', t => {
  const store = new SessionStore(); t.after(() => store.close());
  const binding = { server: 'wss://example.com', account: 'd'.repeat(32), device: 'dev-test' };
  store.enableSync(binding); store.create(SID, 'example', 'codex');
  const request = id(); store.begin(SID, request, '{}', 'user', 'private message'); store.finish(SID, request, { sent: true });
  store.forget(SID);
  const batch = store.batch();
  assert.deepEqual(batch.entries.map(entry => entry.seq), [1, 2, 3, 4]);
  assert.ok(batch.entries.every(entry => entry.kind === 'deleted'));
  assert.ok(!JSON.stringify(batch).includes('private message'));
  assert.deepEqual(store.all(), []);
  assert.throws(() => store.enableSync({ ...binding, account: 'e'.repeat(32) }), /其他账号/);
});

test('legacy history needs explicit upload consent and account stop preserves messages', async t => {
  const dir = temporary(t), path = join(dir, 'state.sqlite3');
  const old = new SessionStore(path); old.create(SID, 'example', 'codex'); old.close();
  const config = { state_path: path, relay: 'wss://example.com/ws/agent', account_id: 'd'.repeat(32), device: 'dev-test' };
  const check = new SessionStore(path);
  assert.throws(() => check.enableSync({ server: 'wss://example.com', account: config.account_id, device: config.device }), /旧会话/);
  check.close();
  const sessions = new Sessions({ ...config, sync_existing: true }); t.after(() => sessions.store.close());
  sessions.tmux = async (...args) => ({ code: 0, stdout: args[0] === 'capture-pane' ? MENU : '' });
  await sessions.handle({ action: 'session.send', session: SID, message: 'kept after stop' });
  await sessions.handle({ action: 'session.stop', session: SID });
  assert.equal(sessions.store.metadata(SID).status, 'stopped');
  assert.equal(sessions.store.events(SID)[0].text, 'kept after stop');
  assert.ok(sessions.store.snapshot(SID).text.includes('proceed'));
  await sessions.handle({ action: 'session.delete', session: SID });
  assert.deepEqual(sessions.store.events(SID), []);
});
