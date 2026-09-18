'use strict';
const $ = id => document.getElementById(id);
let projects = [], busy = false, generation = 0, cursor = 0, snapshot = null, polling = null;
let pendingSend = null, pendingInput = null;
const history = new Map();
const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');

async function api(path, method = 'GET', body) {
  const response = await fetch('/api/' + path, {
    method, headers: {Authorization: 'Bearer ' + $('token').value, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let value;
  try { value = await response.json(); } catch { throw Error('服务端返回异常，请检查连接。'); }
  if (!response.ok) throw Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail));
  return value;
}
const machine = () => 'machines/' + encodeURIComponent($('device').value);
const base = () => machine() + '/sessions';
const selected = () => $('device').value && $('session').value ? base() + '/' + encodeURIComponent($('session').value) : null;

function status(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}
function options(el, items) {
  const old = el.value;
  el.replaceChildren(...items.map(([value, label]) => {
    const option = document.createElement('option');
    option.value = value; option.textContent = label; return option;
  }));
  if (items.some(item => item[0] === old)) el.value = old;
}
function controls() {
  for (const id of ['connect', 'device', 'session', 'project', 'agent']) $(id).disabled = busy;
  $('token').readOnly = busy;
  $('refresh').disabled = busy || !$('device').value;
  $('create').disabled = busy || !$('project').value || !$('agent').value;
  for (const id of ['send', 'logs', 'stop']) $(id).disabled = busy || !selected();
  document.querySelectorAll('[data-key], #choices button').forEach(button => { button.disabled = busy || !snapshot; });
}
function resetSession() {
  generation++; cursor = 0; snapshot = null; history.clear();
  $('messages').replaceChildren();
  const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '还没有发送消息';
  $('messages').append(empty);
  $('output').textContent = '选择会话查看输出';
  $('interaction').hidden = true; $('choices').replaceChildren();
  $('updated').textContent = selected() ? '正在读取…' : '请选择会话';
  $('prompt').value = '';
  controls();
}
function renderHistory() {
  if (!history.size) return;
  const list = $('messages'), nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
  list.replaceChildren(...Array.from(history.values()).map(event => {
    const item = document.createElement('article');
    item.className = 'message ' + (event.source === 'user' ? 'user' : 'interaction') + ' ' + event.status;
    const meta = document.createElement('small');
    const state = event.status === 'sent' ? '' : ' · 执行状态待核对';
    meta.textContent = (event.source === 'user' ? '我 · 用户消息' : '我 · 交互操作') + ' · ' + new Date(event.created_at * 1000).toLocaleTimeString() + state;
    const text = document.createElement('p'); text.textContent = event.text;
    item.append(meta, text); return item;
  }));
  if (nearBottom) list.scrollTop = list.scrollHeight;
}
function renderInteraction(interaction, screen) {
  // Keep existing focused buttons stable while polling an unchanged prompt.
  if (snapshot && snapshot.screen_id === screen) return;
  $('interaction').hidden = !interaction;
  $('choices').replaceChildren();
  if (!interaction) return;
  $('question').textContent = interaction.text;
  for (const option of interaction.options) {
    const button = document.createElement('button');
    button.textContent = (option.selected ? '当前 · ' : '') + option.label;
    button.onclick = () => act(() => sendInput({choice: option.id, screen_id: screen}));
    $('choices').append(button);
  }
}
async function poll() {
  const path = selected(), current = generation;
  if (!path) return;
  if (polling && polling.generation === current) return polling.promise;
  const promise = (async () => {
    try {
      const value = await api(path + '/state?after=' + cursor);
      if (current !== generation || path !== selected()) return;
      for (const event of value.events) history.set(event.id, event);
      cursor = value.cursor;
      if (value.events.length) renderHistory();
      const output = $('output'), nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 70;
      if (output.textContent !== value.terminal.text) {
        output.textContent = value.terminal.text || '等待终端输出…';
        if (nearBottom) output.scrollTop = output.scrollHeight;
      }
      renderInteraction(value.interaction, value.screen_id);
      snapshot = value;
      $('updated').textContent = '更新于 ' + new Date().toLocaleTimeString();
      controls();
    } catch (error) {
      if (current !== generation) return;
      snapshot = null; controls();
      $('updated').textContent = '连接中断，等待刷新';
      throw error;
    } finally {
      if (polling && polling.generation === current) polling = null;
    }
  })();
  polling = {generation: current, promise};
  return promise;
}
async function act(fn) {
  if (busy) return;
  busy = true; generation++; controls(); status('处理中…');
  try { await fn(); status('操作完成'); }
  catch (error) { status(error.message, true); }
  finally { busy = false; controls(); }
}
function agentsForProject() {
  options($('agent'), (projects.find(p => p.id === $('project').value)?.agents || []).map(name => [name, name]));
  controls();
}
async function refreshSessions() {
  const previous = selected();
  options($('session'), (await api(base())).map(s => [s.id, (s.project ? s.project + ' · ' + s.agent + ' · ' : '') + s.id.slice(0, 11)]));
  if (previous !== selected()) resetSession();
  await poll();
}
async function loadDevice() {
  $('session').replaceChildren(); resetSession();
  projects = []; $('project').replaceChildren(); agentsForProject();
  if (!$('device').value) return;
  projects = await api(machine() + '/projects');
  options($('project'), projects.map(p => [p.id, p.id])); agentsForProject();
  await refreshSessions();
}
async function sendInput(input) {
  if (!snapshot || !selected()) throw Error('请先刷新终端。');
  const path = selected(), body = {screen_id: snapshot.screen_id, ...input};
  const identity = JSON.stringify([path, body]);
  if (!pendingInput || pendingInput.identity !== identity) pendingInput = {identity, id: requestId()};
  let failure;
  try {
    await api(path + '/input', 'POST', {...body, request_id: pendingInput.id});
    pendingInput = null;
  } catch (error) { failure = error; }
  snapshot = null;
  try { await poll(); } catch (error) { failure = failure || error; }
  if (failure) throw failure;
}
$('connect').onclick = () => act(async () => {
  $('session').replaceChildren(); resetSession();
  projects = []; $('project').replaceChildren(); agentsForProject();
  options($('device'), (await api('machines')).map(d => [d.id, d.id + (d.online ? ' · 在线' : ' · 离线')]));
  await loadDevice();
});
$('refresh').onclick = () => act(refreshSessions);
$('device').onchange = () => act(loadDevice);
$('project').onchange = agentsForProject;
$('session').onchange = () => act(async () => { resetSession(); await poll(); });
$('logs').onclick = () => act(poll);
$('create').onclick = () => act(async () => {
  const session = await api(base(), 'POST', {project: $('project').value, agent: $('agent').value});
  await refreshSessions(); $('session').value = session.id; resetSession(); await poll();
});
$('composer').onsubmit = event => {
  event.preventDefault();
  if (!$('prompt').value.trim() || !selected()) return;
  act(async () => {
    const message = $('prompt').value, path = selected();
    if (!pendingSend || pendingSend.message !== message || pendingSend.path !== path) {
      pendingSend = {message, path, id: requestId()};
    }
    let failure;
    try {
      await api(path + '/send', 'POST', {message, request_id: pendingSend.id});
      pendingSend = null;
      if ($('prompt').value === message) $('prompt').value = '';
    } catch (error) { failure = error; }
    try { await poll(); } catch (error) { failure = failure || error; }
    if (failure) throw failure;
  });
};
$('prompt').onkeydown = event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('composer').requestSubmit(); }
};
$('stop').onclick = () => {
  if (!selected() || !confirm('停止会话将结束当前 CLI 和任务，并清除此会话的消息记录。是否继续？')) return;
  act(async () => { await api(selected(), 'DELETE'); await refreshSessions(); });
};
document.querySelectorAll('[data-key]').forEach(button => {
  button.onclick = () => {
    if (button.dataset.key === 'C-c' && !confirm('发送 Ctrl+C 中断当前操作？')) return;
    act(() => sendInput({key: button.dataset.key}));
  };
});
setInterval(() => {
  if (!busy && $('auto').checked && document.visibilityState === 'visible') poll().catch(error => status(error.message, true));
}, 2000);
controls();
