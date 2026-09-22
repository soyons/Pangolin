import WebSocket from 'ws';
import { setTimeout as delay } from 'node:timers/promises';
import { Sessions } from './sessions.js';
import { serverRequest } from './api.js';
import { SessionError } from './utils.js';

export async function runAgent(config, { signal, log = console.log } = {}) {
  const sessions = new Sessions(config);
  let backoff = 1000, fatal, pump = () => {}, capturing = null;
  const capture = () => {
    if (!sessions.store.sync || capturing || signal?.aborted) return;
    capturing = sessions.handle({ action: 'sync.capture' }).then(() => pump())
      .catch(() => log('无法采集终端，请检查本机 tmux；已保存消息仍保留。')).finally(() => { capturing = null; });
  };
  const captureTimer = sessions.store.sync ? setInterval(capture, 2000) : null;
  capture();
  try {
    while (!signal?.aborted && !fatal) {
      if (sessions.store.sync) {
        try { await serverRequest(config.relay, '/api/agent/refresh', { device: config.device, token: config.device_token }); }
        catch (error) {
          if (error.status === 401) throw Error('设备授权已失效，请执行 pangolin-agent login 重新登录。');
          log('暂时无法续期设备授权，继续尝试连接。');
        }
      }
      let queue = Promise.resolve();
      await new Promise(resolve => {
        const socket = new WebSocket(config.relay, { maxPayload: 131072, handshakeTimeout: 15000,
          headers: { Authorization: 'Bearer ' + config.device_token, 'X-Device-ID': config.device } });
        let heartbeat, synchronization, renewal, alive = true, queued = 0, inFlight = null;
        const deleting = new Set();
        const stop = () => socket.terminate();
        signal?.addEventListener('abort', stop, { once: true });
        if (signal?.aborted) stop();
        pump = () => {
          if (!sessions.store.sync || inFlight || socket.readyState !== WebSocket.OPEN) return;
          const batch = sessions.store.batch();
          inFlight = { through: batch.entries.at(-1)?.seq ?? Number(sessions.store.meta('ack') || 0), at: Date.now() };
          socket.send(JSON.stringify(batch));
        };
        socket.on('open', () => {
          log('Connected to relay'); backoff = 1000;
          socket.send(JSON.stringify({ type: 'heartbeat' }));
          heartbeat = setInterval(() => {
            if (!alive) { socket.terminate(); return; }
            alive = false; socket.ping(); socket.send(JSON.stringify({ type: 'heartbeat' }));
          }, 25000);
          if (sessions.store.sync) {
            pump();
            synchronization = setInterval(() => {
              if (inFlight && Date.now() - inFlight.at > 15000) { socket.terminate(); return; }
              pump();
            }, 2000);
            renewal = setInterval(() => {
              serverRequest(config.relay, '/api/agent/refresh', { device: config.device, token: config.device_token })
                .catch(error => { if (error.status === 401) { fatal = Error('设备授权已撤销，请重新登录。'); socket.close(); } });
            }, 24 * 60 * 60 * 1000);
          }
        });
        socket.on('pong', () => { alive = true; });
        socket.on('message', raw => {
          let msg;
          try {
            msg = JSON.parse(raw.toString());
            if (!msg || typeof msg !== 'object' || Array.isArray(msg)) throw Error();
            if (sessions.store.sync && msg.type === 'sync.error') {
              fatal = Error('服务端拒绝同步，请检查账号绑定和本地同步数据库。'); socket.close(); return;
            }
            if (sessions.store.sync && msg.type === 'sync.ack') {
              if (!inFlight || msg.stream !== sessions.store.meta('stream') || !Array.isArray(msg.deleted) || msg.deleted.length > 100) throw Error();
              sessions.store.ack(msg.cursor, inFlight.through); inFlight = null;
              for (const session of msg.deleted) {
                if (typeof session !== 'string' || !/^rp-[0-9a-f]{32}$/.test(session)) throw Error();
                if (deleting.has(session)) continue;
                deleting.add(session);
                queue = queue.then(() => sessions.handle({ action: 'session.delete', session }))
                  .catch(() => log('删除的会话尚未在本机清除，将在下次同步重试。'))
                  .finally(() => { deleting.delete(session); pump(); });
              }
              if (sessions.store.batch().entries.length) pump();
              return;
            }
            if (typeof msg.id !== 'string') throw Error();
          } catch { socket.close(1008, 'Invalid command envelope'); return; }
          if (++queued > 32) { socket.close(1008, 'Too many pending commands'); return; }
          queue = queue.then(async () => {
            if (socket.readyState !== WebSocket.OPEN) return;
            let reply;
            try { reply = { id: msg.id, ok: true, result: await sessions.handle(msg) }; }
            catch (error) {
              reply = error instanceof SessionError ? { id: msg.id, ok: false, error: error.message, status: 409 }
                : { id: msg.id, ok: false, error: 'Command rejected or failed; check local configuration/session' };
            }
            if (socket.readyState === WebSocket.OPEN) { pump(); socket.send(JSON.stringify(reply)); }
          }).catch(() => { socket.terminate(); }).finally(() => { queued--; });
        });
        socket.on('error', error => { log('Relay connection error (' + (error.code || error.name) + ')'); });
        socket.on('close', code => {
          clearInterval(heartbeat); clearInterval(synchronization); clearInterval(renewal);
          pump = () => {}; signal?.removeEventListener('abort', stop);
          if (!signal?.aborted && !fatal) log(code === 1008 ? '设备连接被拒绝，请检查登录状态或是否已有 Agent 在运行。' : 'Relay disconnected; reconnecting');
          resolve();
        });
      });
      await queue;
      if (!signal?.aborted && !fatal) {
        await delay(backoff + Math.random() * 500, undefined, { signal }).catch(error => { if (error.name !== 'AbortError') throw error; });
        backoff = Math.min(backoff * 2, 30000);
      }
    }
    if (fatal) throw fatal;
  } finally { clearInterval(captureTimer); await capturing; await sessions.queue; sessions.store.close(); }
}
