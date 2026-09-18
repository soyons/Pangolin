import { spawn } from 'node:child_process';
import { openSync, readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { createInterface } from 'node:readline/promises';
import { Writable } from 'node:stream';
import { ReadStream, WriteStream } from 'node:tty';
import { configure, defaultPrefix, readConfig, runtimeConfig, saveConfig } from './config.js';
import { checkServicePrefix, installService, managementCommand } from './service.js';
import { expand } from './utils.js';

const HELP = `Pangolin Node Agent (Node.js 22.13+; macOS / Linux)

pangolin-agent setup [--relay https://HOST] [--device devbox] [--project PATH]
                     [--token-file FILE] [--prefix PATH] [--no-start]
pangolin-agent run|start|stop|status|logs [--prefix PATH]

setup 自动保存配置并注册后台服务；设备 Token 隐藏输入。
--no-start 只配置文件，随后可用 run 前台运行。
默认配置目录：~/.local/share/pangolin（兼容旧 Python Agent 配置）。
`;

export async function ask(label, fallback = '', secret = false) {
  let input, output;
  try { input = new ReadStream(openSync('/dev/tty', 'r')); output = new WriteStream(openSync('/dev/tty', 'w')); }
  catch { input?.destroy(); throw Error(label + '：没有交互终端，请提供对应参数或 --token-file。'); }
  const display = new Writable({ write(chunk, encoding, done) { if (!secret) output.write(chunk, encoding); done(); } });
  const rl = createInterface({ input, output: display, terminal: true });
  const controller = new AbortController();
  rl.on('SIGINT', () => controller.abort());
  output.write(label + (fallback ? ' [' + fallback + ']' : '') + ': ');
  try { return (await rl.question('', { signal: controller.signal })).trim() || fallback; }
  finally { rl.close(); input.destroy(); output.end(secret ? '\n' : ''); }
}

export async function main(argv = process.argv.slice(2)) {
  process.umask(0o077);
  const { values, positionals } = parseArgs({ args: argv, allowPositionals: true, options: {
    relay: { type: 'string' }, device: { type: 'string' }, project: { type: 'string' },
    'token-file': { type: 'string' }, prefix: { type: 'string' },
    'no-start': { type: 'boolean' }, help: { type: 'boolean', short: 'h' }, version: { type: 'boolean', short: 'v' }
  } });
  if (values.version) { console.log(JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version); return; }
  if (values.help || !positionals.length) { console.log(HELP); return; }
  if (positionals.length !== 1) throw Error('请只指定一个操作；使用 --help 查看命令。');
  const action = positionals[0], prefix = expand(values.prefix || defaultPrefix());
  if (/[\x00-\x1f\x7f]/u.test(prefix)) throw Error('安装路径不能包含控制字符。');
  if (!['setup', 'run', 'start', 'stop', 'status', 'logs'].includes(action)) throw Error('未知操作：' + action);
  if (action !== 'setup' && Object.keys(values).some(key => key !== 'prefix')) throw Error('连接配置参数仅用于 setup。');
  if (action === 'setup') {
    if (process.getuid?.() === 0) throw Error('请用拥有项目和 Codex/Claude 登录信息的普通账号安装 Agent，不要 sudo 整条命令。');
    const config = await configure(values, readConfig(prefix), { ask });
    saveConfig(prefix, config);
    if (!values['no-start']) {
      try { await installService(prefix); }
      catch { throw Error('配置已保存，但后台服务未启动。可先用 pangolin-agent run 前台运行；Linux 请检查 systemd user service，macOS 请在登录用户终端中安装。'); }
    }
    console.log(values['no-start'] ? 'Agent 配置已保存，使用 pangolin-agent run 前台启动。' : 'Agent 后台服务已启动，使用 pangolin-agent status / logs 检查连接。');
    console.log('配置目录：' + prefix + '\n网页项目名：example');
    if (process.platform === 'linux' && !values['no-start']) console.log('若需退出 SSH 后持续运行：sudo loginctl enable-linger ' + (process.env.USER || '你的用户名'));
    return;
  }
  checkServicePrefix(prefix);
  if (action === 'run') {
    const { runAgent } = await import('./client.js');
    const controller = new AbortController(), stop = () => controller.abort();
    process.once('SIGINT', stop); process.once('SIGTERM', stop);
    try { await runAgent(runtimeConfig(prefix, readConfig(prefix)), { signal: controller.signal }); }
    finally { process.off('SIGINT', stop); process.off('SIGTERM', stop); }
  } else {
    const [file, ...args] = managementCommand(prefix, action);
    await new Promise((resolve, reject) => {
      const child = spawn(file, args, { stdio: 'inherit' });
      child.on('error', reject);
      child.on('exit', code => { process.exitCode = code ?? 1; resolve(); });
    });
  }
}
