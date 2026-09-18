import { realpathSync, statSync } from 'node:fs';
import { SessionStore } from './store.js';
import { choiceKeys, detectInteraction, KEYS, screenId } from './interactions.js';
import { canonical, command, expand, id, SessionError, tail, which } from './utils.js';

const SESSION = /^rp-[0-9a-f]{32}$/u;

export class Sessions {
  constructor(config) {
    this.projects = config.projects || {};
    this.socket = config.tmux_socket || 'pangolin';
    if (!/^[a-zA-Z0-9_-]+$/u.test(this.socket) || /\s/u.test(this.socket)) throw Error('Invalid tmux socket');
    this.env = config.environment || process.env;
    this.store = new SessionStore(config.state_path);
    this.queue = Promise.resolve();
  }
  async tmux(...args) {
    const options = typeof args.at(-1) === 'object' ? args.pop() : {};
    try { return await command('tmux', ['-f', '/dev/null', '-L', this.socket, ...args], { env: this.env, ...options }); }
    catch { throw Error('tmux operation failed; check the agent locally'); }
  }
  async visible(session) { return (await this.tmux('capture-pane', '-p', '-t', session + ':0.0')).stdout; }
  handle(message) {
    const next = this.queue.then(() => this.execute(message));
    this.queue = next.catch(() => {});
    return next;
  }
  async execute(msg) {
    const action = msg.action;
    if (action === 'project.list') return Object.entries(this.projects).map(([name, project]) => ({
      id: name, agents: (project.agents || []).filter(agent => ['codex', 'claude'].includes(agent))
    }));
    if (action === 'session.list') {
      const result = await this.tmux('list-sessions', '-F', '#{session_name}', { check: false });
      return result.stdout.split('\n').filter(name => name.length === 35 && SESSION.test(name)).map(name => this.store.metadata(name));
    }
    if (action === 'session.create') {
      const project = Object.hasOwn(this.projects, msg.project) ? this.projects[msg.project] : null;
      if (!project || !['codex', 'claude'].includes(msg.agent) || !project.agents?.includes(msg.agent)) throw Error('Project or agent is not allowed');
      const path = realpathSync(expand(project.path)), executable = which(msg.agent, this.env.PATH);
      if (!statSync(path).isDirectory() || !executable) throw Error('Project directory or executable unavailable');
      const session = 'rp-' + id();
      await this.tmux('new-session', '-d', '-s', session, '-c', path, '-x', '120', '-y', '40', 'env', executable);
      this.store.create(session, msg.project, msg.agent);
      return { id: session, project: msg.project, agent: msg.agent };
    }
    if (!['session.send', 'session.logs', 'session.stop', 'session.state', 'session.input'].includes(action)) throw Error('Unknown action');
    const session = msg.session;
    if (typeof session !== 'string' || session.length !== 35 || !SESSION.test(session)) throw Error('Invalid session');
    await this.tmux('has-session', '-t', '=' + session);
    if (action === 'session.logs') return { text: tail((await this.tmux('capture-pane', '-p', '-t', session + ':0.0', '-S', '-200')).stdout, 30000) };
    if (action === 'session.state') {
      const after = msg.after ?? 0;
      if (!Number.isSafeInteger(after) || after < 0) throw Error('Invalid event cursor');
      const screen = await this.visible(session), events = this.store.events(session, after);
      return { session: this.store.metadata(session), events, cursor: events.at(-1)?.id || after,
        terminal: { source: 'terminal', text: tail((await this.tmux('capture-pane', '-p', '-t', session + ':0.0', '-S', '-200')).stdout.trimEnd(), 8000) },
        screen_id: screenId(screen), interaction: detectInteraction(screen) };
    }
    if (action === 'session.stop') {
      await this.tmux('kill-session', '-t', '=' + session);
      this.store.forget(session);
      return { stopped: true };
    }
    const request = msg.request_id ?? id();
    if (typeof request !== 'string' || request.length !== 32 || !/^[0-9a-f]+$/u.test(request)) throw Error('Invalid request ID');
    const payload = canonical(Object.fromEntries(Object.entries(msg).filter(([key]) => !['id', 'request_id'].includes(key))));
    const previous = this.store.previous(session, request, payload);
    if (previous) return previous;
    let keys, label;
    if (action === 'session.input') {
      const screen = await this.visible(session);
      if (screenId(screen) !== msg.screen_id) throw new SessionError('终端内容已变化，请刷新并核对当前提示后再操作。');
      if ((msg.key == null) === (msg.choice == null)) throw Error('Supply exactly one key or choice');
      if (msg.key != null) {
        if (!KEYS.has(msg.key)) throw Error('Key is not allowed');
        keys = [msg.key]; label = '终端按键：' + msg.key;
      } else {
        const interaction = detectInteraction(screen);
        if (!interaction) throw new SessionError('当前没有可识别的选项，请查看终端并使用按键操作。');
        keys = choiceKeys(interaction, msg.choice);
        label = '选择：' + interaction.options.find(option => option.id === msg.choice).label;
      }
    } else {
      if (typeof msg.message !== 'string' || !msg.message.length || Array.from(msg.message).length > 16000) throw Error('Prompt must contain 1–16000 characters');
      if (/[\x00-\x08\x0b-\x1f\x7f]/u.test(msg.message)) throw Error('Terminal control characters are not allowed');
      label = msg.message;
    }
    this.store.begin(session, request, payload, action === 'session.send' ? 'user' : 'interaction', label);
    try {
      if (keys) await this.tmux('send-keys', '-t', session + ':0.0', ...keys);
      else {
        const buffer = 'rp-' + id();
        try {
          await this.tmux('load-buffer', '-b', buffer, '-', { input: msg.message });
          await this.tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', session + ':0.0');
          await this.tmux('send-keys', '-t', session + ':0.0', 'Enter');
        } finally { await this.tmux('delete-buffer', '-b', buffer, { check: false }).catch(() => {}); }
      }
    } catch (error) { this.store.finish(session, request); throw error; }
    const result = { sent: true, request_id: request };
    this.store.finish(session, request, result);
    return result;
  }
}
