import { chmodSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { command, privateWrite } from './utils.js';

const CLI = fileURLToPath(new URL('../bin/pangolin-agent.js', import.meta.url));
export const systemdQuote = value => '"' + String(value).replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('%', '%%').replaceAll('$', () => '$$') + '"';
const xml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&apos;');

export function serviceFile(prefix, platform = process.platform, executable = process.execPath, cli = CLI) {
  const args = [executable, cli, 'run', '--prefix', prefix];
  if (platform === 'darwin') return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.pangolin.agent</string>
<key>ProgramArguments</key><array>${args.map(arg => '<string>' + xml(arg) + '</string>').join('')}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>${xml(join(prefix, 'agent.log'))}</string>
<key>StandardErrorPath</key><string>${xml(join(prefix, 'agent.log'))}</string>
<key>Umask</key><integer>63</integer>
</dict></plist>\n`;
  if (platform !== 'linux') throw Error('后台服务仅支持 macOS 和 systemd Linux。');
  return `[Unit]\nDescription=Pangolin Node Agent\nAfter=network-online.target\n[Service]\nExecStart=${args.map(systemdQuote).join(' ')}\nRestart=on-failure\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=default.target\n`;
}

export function servicePath(platform = process.platform) {
  return platform === 'darwin' ? join(homedir(), 'Library/LaunchAgents/io.pangolin.agent.plist')
    : join(homedir(), '.config/systemd/user/pangolin-agent.service');
}

export async function installService(prefix) {
  const path = servicePath();
  privateWrite(path, serviceFile(prefix));
  if (process.platform === 'darwin') {
    chmodSync(path, 0o600);
    const domain = 'gui/' + process.getuid();
    await command('launchctl', ['bootout', domain + '/io.pangolin.agent'], { check: false });
    await command('launchctl', ['bootstrap', domain, path]);
    await command('launchctl', ['print', domain + '/io.pangolin.agent']);
  } else {
    await command('systemctl', ['--user', 'daemon-reload']);
    await command('systemctl', ['--user', 'enable', 'pangolin-agent']);
    await command('systemctl', ['--user', 'restart', 'pangolin-agent']);
    await command('systemctl', ['--user', 'is-active', 'pangolin-agent']);
  }
}

export function managementCommand(prefix, action, platform = process.platform) {
  if (platform === 'darwin') {
    const domain = 'gui/' + process.getuid(), label = domain + '/io.pangolin.agent';
    return { status: ['launchctl', 'print', label], start: ['launchctl', 'bootstrap', domain, servicePath(platform)],
      stop: ['launchctl', 'bootout', label], logs: ['tail', '-n', '100', '-f', join(prefix, 'agent.log')] }[action];
  }
  if (platform !== 'linux') throw Error('后台服务仅支持 macOS 和 systemd Linux。');
  return action === 'logs' ? ['journalctl', '--user', '-u', 'pangolin-agent', '-n', '100', '-f']
    : ['systemctl', '--user', action, 'pangolin-agent'];
}

export function checkServicePrefix(prefix) {
  if (!existsSync(join(prefix, 'agent.json'))) throw Error('尚未配置 Agent，请先运行 pangolin-agent setup。');
}
