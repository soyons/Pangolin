import WebSocket from 'ws';
import { setTimeout as delay } from 'node:timers/promises';
import { Sessions } from './sessions.js';
import { SessionError } from './utils.js';

export async function runAgent(config, { signal, log = console.log } = {}) {
  const sessions = new Sessions(config);
  let backoff = 1000;
  try {
    while (!signal?.aborted) {
      let queue = Promise.resolve();
      await new Promise(resolve => {
        const socket = new WebSocket(config.relay, { maxPayload: 131072, handshakeTimeout: 15000,
          headers: { Authorization: 'Bearer ' + config.device_token, 'X-Device-ID': config.device } });
        let heartbeat, alive = true, queued = 0;
        const stop = () => socket.terminate();
        signal?.addEventListener('abort', stop, { once: true });
        if (signal?.aborted) stop();
        socket.on('open', () => {
          log('Connected to relay'); backoff = 1000;
          socket.send(JSON.stringify({ type: 'heartbeat' }));
          heartbeat = setInterval(() => {
            if (!alive) { socket.terminate(); return; }
            alive = false; socket.ping(); socket.send(JSON.stringify({ type: 'heartbeat' }));
          }, 25000);
        });
        socket.on('pong', () => { alive = true; });
        socket.on('message', raw => {
          let msg;
          try {
            msg = JSON.parse(raw.toString());
            if (!msg || typeof msg !== 'object' || Array.isArray(msg) || typeof msg.id !== 'string') throw Error();
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
            if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(reply));
          }).catch(() => { socket.terminate(); }).finally(() => { queued--; });
        });
        socket.on('error', error => { log('Relay connection error (' + (error.code || error.name) + ')'); });
        socket.on('close', code => {
          clearInterval(heartbeat); signal?.removeEventListener('abort', stop);
          if (!signal?.aborted) log(code === 1008 ? 'Relay rejected connection; check device token/ID or an existing Agent.' : 'Relay disconnected; reconnecting');
          resolve();
        });
      });
      await queue;
      if (!signal?.aborted) {
        await delay(backoff + Math.random() * 500, undefined, { signal }).catch(error => { if (error.name !== 'AbortError') throw error; });
        backoff = Math.min(backoff * 2, 30000);
      }
    }
  } finally { sessions.store.close(); }
}
