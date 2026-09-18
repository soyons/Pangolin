import { execFile } from 'node:child_process';
import { accessSync, closeSync, constants, existsSync, fsyncSync, mkdirSync, openSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

export const id = () => randomUUID().replaceAll('-', '');
export const tail = (value, count) => Array.from(value).slice(-count).join('');
export const expand = value => resolve(value === '~' ? homedir() : value.startsWith('~/') ? join(homedir(), value.slice(2)) : value);
export const canonical = value => JSON.stringify(value, Object.keys(value).sort());
export class SessionError extends Error {}

export function which(name, path = process.env.PATH || '') {
  for (const directory of path.split(':')) {
    const candidate = join(directory, name);
    try {
      accessSync(candidate, constants.X_OK);
      if (statSync(candidate).isFile()) return resolve(candidate);
    } catch {}
  }
  return null;
}

export function privateWrite(path, content) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temp = path + '.' + id() + '.tmp';
  const fd = openSync(temp, 'wx', 0o600);
  try {
    writeFileSync(fd, content);
    fsyncSync(fd);
  } finally { closeSync(fd); }
  try { renameSync(temp, path); }
  finally { if (existsSync(temp)) unlinkSync(temp); }
}

export function command(file, args, { input, check = true, ...options } = {}) {
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, { encoding: 'utf8', timeout: 10000, maxBuffer: 2 * 1024 * 1024, ...options }, (error, stdout, stderr) => {
      if (error && (check || error.killed || typeof error.code !== 'number')) reject(error);
      else resolve({ stdout, stderr, code: error?.code || 0 });
    });
    child.stdin.on('error', () => {}); // The process callback reports early exits.
    child.stdin.end(input);
  });
}
