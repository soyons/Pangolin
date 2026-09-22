import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { homedir, hostname } from 'node:os';
import { dirname, join } from 'node:path';
import { expand, privateWrite, which } from './utils.js';
import { serverRequest } from './api.js';

export const MODEL_KEYS = ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN'];
export const defaultPrefix = () => join(homedir(), '.local/share/pangolin');

export function relayUrl(value) {
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || !url.hostname || !['', '/', '/ws/agent'].includes(url.pathname)) throw Error('Relay 地址必须是站点根地址，不能包含账号、查询参数或其他路径。');
  const protocol = { 'https:': 'wss:', 'http:': 'ws:', 'wss:': 'wss:', 'ws:': 'ws:' }[url.protocol];
  if (!protocol || (protocol === 'ws:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw Error('远程 Relay 必须使用 HTTPS/WSS，本机测试可使用 HTTP。');
  url.protocol = protocol; url.pathname = '/ws/agent';
  return url.toString();
}

export function readConfig(prefix) {
  const file = join(prefix, 'agent.json');
  return existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {};
}

export function validateConfig(config) {
  config.relay = relayUrl(config.relay);
  if (typeof config.device !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/u.test(config.device) || /\s/u.test(config.device)) throw Error('设备 ID 只能包含字母、数字、连字符和下划线。');
  if (typeof config.device_token !== 'string' || !/^[A-Za-z0-9_-]{32,}$/u.test(config.device_token) || /\s/u.test(config.device_token)) throw Error('设备尚未登录或凭据无效，请执行 pangolin-agent login。');
  return config;
}

export async function configure(options, existing, { env = process.env, ask, fetcher } = {}) {
  const config = { ...existing, role: 'agent', runtime: 'node', path: dirname(process.execPath) + ':' + (env.PATH || '/usr/local/bin:/usr/bin:/bin') };
  config.relay = relayUrl(options.server || options.relay || existing.relay || await ask('服务端地址', 'https://agent.example.com'));
  if (existing.account_id && config.relay !== relayUrl(existing.relay)) throw Error('此目录已绑定其他服务端，请使用新的 --prefix。');
  const project = options.project || existing.project || await ask('项目绝对路径', process.cwd());
  config.project = realpathSync(expand(project));
  if (!statSync(config.project).isDirectory()) throw Error('项目路径必须是已有目录。');
  config.agents = ['codex', 'claude'].filter(name => which(name, config.path));
  if (!config.agents.length) throw Error('请先安装并登录 Codex 或 Claude，然后重新运行安装命令。');
  if (!which('tmux', config.path)) throw Error('缺少 tmux，请通过 curl 安装脚本准备依赖。');
  config.model_environment = { ...existing.model_environment };
  for (const key of MODEL_KEYS) if (env[key]) config.model_environment[key] = env[key];
  const legacy = options['token-file'] || (!options.login && !options.email && !existing.account_id && existing.device_token);
  if (legacy) {
    if (existing.account_id) throw Error('已绑定账号的目录不能切换到旧 Token 模式。');
    config.device = options.device || existing.device || 'devbox';
    config.device_token = options['token-file'] ? readFileSync(expand(options['token-file']), 'utf8').trim() : existing.device_token;
  } else if (!existing.account_id || !existing.device_token || options.login || options.email || options['password-file']) {
    if (existing.device_token && !existing.account_id && !options['sync-existing']) throw Error('迁移旧客户端会上传历史，请加 --sync-existing，或用新 --prefix 从空白配置开始。');
    const email = (options.email || existing.email || await ask('邮箱账号')).trim().toLowerCase();
    const password = options['password-file'] ? readFileSync(expand(options['password-file']), 'utf8').replace(/\r?\n$/, '') : await ask('账号密码', '', true);
    const name = options.device || existing.device_name || hostname();
    const body = { email, password, name };
    if (existing.account_id) { body.expected_account = existing.account_id; body.device_id = existing.device; }
    // Legacy device IDs are imported only through the administrator's local migration command.
    else if (options['sync-existing'] && existing.device) body.device_id = existing.device;
    const result = await serverRequest(config.relay, '/api/auth/device-login', { body, fetcher });
    config.account_id = result.user.id; config.email = result.user.email; config.device = result.device;
    config.device_name = name; config.device_token = result.device_token;
    config.sync_existing = options['sync-existing'] === true || existing.sync_existing === true;
    config.tmux_socket = existing.tmux_socket || (options['sync-existing'] ? 'pangolin' : 'pangolin-' + result.device);
  }
  validateConfig(config);
  return config;
}

export function saveConfig(prefix, config) {
  privateWrite(join(prefix, 'agent.json'), JSON.stringify(config, null, 2) + '\n');
}

export function runtimeConfig(prefix, config) {
  validateConfig(config);
  const environment = { ...process.env, PATH: config.path || process.env.PATH };
  for (const key of ['USER_TOKEN', 'DEVICE_TOKEN', 'DEVICE_TOKENS', 'RELAY_WS_URL']) delete environment[key];
  for (const key of MODEL_KEYS) if (config.model_environment?.[key]) environment[key] = config.model_environment[key];
  return { ...config, environment, state_path: config.state_path || join(prefix, 'state/sessions.sqlite3'),
    projects: config.projects || { example: { path: config.project, agents: config.agents } } };
}
