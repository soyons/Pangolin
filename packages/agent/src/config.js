import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { expand, privateWrite, which } from './utils.js';

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
  if (typeof config.device_token !== 'string' || !/^[A-Za-z0-9_-]{32,}$/u.test(config.device_token) || /\s/u.test(config.device_token)) throw Error('设备 Token 必须是至少 32 位的 URL-safe 随机字符串。');
  return config;
}

export async function configure(options, existing, { env = process.env, ask } = {}) {
  const config = { ...existing, role: 'agent', runtime: 'node', path: dirname(process.execPath) + ':' + (env.PATH || '/usr/local/bin:/usr/bin:/bin') };
  config.relay = options.relay || existing.relay || await ask('Relay 地址', 'https://agent.example.com');
  config.device = options.device || existing.device || await ask('设备 ID', 'devbox');
  config.device_token = options['token-file'] ? readFileSync(expand(options['token-file']), 'utf8').trim()
    : existing.device_token || await ask('设备 Token（从服务端查看）', '', true);
  validateConfig(config);
  const project = options.project || existing.project || await ask('项目绝对路径', process.cwd());
  config.project = realpathSync(expand(project));
  if (!statSync(config.project).isDirectory()) throw Error('项目路径必须是已有目录。');
  config.agents = ['codex', 'claude'].filter(name => which(name, config.path));
  if (!config.agents.length) throw Error('请先安装并登录 Codex 或 Claude，然后重新运行安装命令。');
  if (!which('tmux', config.path)) throw Error('缺少 tmux，请通过 curl 安装脚本准备依赖。');
  config.model_environment = { ...existing.model_environment };
  for (const key of MODEL_KEYS) if (env[key]) config.model_environment[key] = env[key];
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
