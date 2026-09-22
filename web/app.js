'use strict';
const $ = id => document.getElementById(id);
let projects = [], devices = [], busy = false, generation = 0, accountEpoch = 0, cursor = 0, snapshot = null, polling = null;
let pendingSend = null, pendingInput = null, mode = 'accounts', csrf = '', user = null, updates = null, refreshing = false;
let authSettings = {}, refreshRequested = false, nextRefresh = 0;
const history = new Map();
const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');

async function api(path, method = 'GET', body) {
  const epoch = accountEpoch;
  const headers = { 'Content-Type': 'application/json' };
  if (mode === 'legacy') headers.Authorization = 'Bearer ' + $('token').value;
  else if (csrf) headers['X-CSRF-Token'] = csrf;
  const response = await fetch('/api/' + path, { method, headers, credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body) });
  let value;
  try { value = await response.json(); } catch { throw Error('服务端返回异常，请检查连接。'); }
  if (epoch !== accountEpoch) throw Error('账号状态已改变，请重新操作。');
  if (!response.ok) {
    if (response.status === 401 && user && !['auth/password', 'auth/login'].includes(path)) signedOut();
    const detail = typeof value.detail === 'string' ? value.detail : '输入不符合要求，请检查邮箱、密码或操作参数。';
    const error = Error(detail); error.status = response.status; throw error;
  }
  return value;
}
const machine = () => 'machines/' + encodeURIComponent($('device').value);
const base = () => machine() + '/sessions';
const selected = () => $('device').value && $('session').value ? base() + '/' + encodeURIComponent($('session').value) : null;
const online = () => devices.find(device => device.id === $('device').value)?.online === true;
const live = () => Boolean(snapshot && (mode === 'legacy' || snapshot.live));
function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
function options(el, items) {
  const old = el.value;
  el.replaceChildren(...items.map(([value, label]) => { const option = document.createElement('option'); option.value = value; option.textContent = label; return option; }));
  if (items.some(item => item[0] === old)) el.value = old;
}
function controls() {
  for (const id of ['connect', 'device', 'session', 'project', 'agent', 'login', 'register', 'logout']) $(id).disabled = busy;
  $('token').readOnly = busy;
  $('refresh').disabled = busy || (mode === 'accounts' && !user);
  $('create').disabled = busy || !online() || !$('project').value || !$('agent').value;
  for (const id of ['send', 'stop']) $(id).disabled = busy || !selected() || !live();
  $('logs').disabled = busy || !selected();
  $('delete').disabled = busy || !selected();
  $('revoke').disabled = busy || !$('device').value || devices.find(d => d.id === $('device').value)?.revoked;
  document.querySelectorAll('[data-key], #choices button').forEach(button => { button.disabled = busy || !live(); });
}
function resetSession() {
  generation++; cursor = 0; snapshot = null; history.clear(); pendingSend = null; pendingInput = null;
  $('messages').replaceChildren();
  const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '还没有发送消息'; $('messages').append(empty);
  $('output').textContent = '选择会话查看输出'; $('interaction').hidden = true; $('choices').replaceChildren();
  $('updated').textContent = selected() ? '正在读取…' : '请选择会话'; $('prompt').value = ''; controls();
}
function renderHistory() {
  if (!history.size) return;
  const list = $('messages'), nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
  list.replaceChildren(...Array.from(history.values()).sort((a, b) => a.id - b.id).map(event => {
    const item = document.createElement('article'); item.className = 'message ' + (event.source === 'user' ? 'user' : 'interaction') + ' ' + event.status;
    const meta = document.createElement('small'), state = event.status === 'sent' ? '' : ' · 执行状态待核对';
    meta.textContent = (event.source === 'user' ? '我 · 用户消息' : '我 · 交互操作') + ' · ' + new Date(event.created_at * 1000).toLocaleTimeString() + state;
    const text = document.createElement('p'); text.textContent = event.text; item.append(meta, text); return item;
  }));
  if (nearBottom) list.scrollTop = list.scrollHeight;
}
function renderInteraction(interaction, screen) {
  if (snapshot && snapshot.screen_id === screen) return;
  $('interaction').hidden = !interaction; $('choices').replaceChildren();
  if (!interaction) return;
  $('question').textContent = interaction.text;
  for (const option of interaction.options) {
    const button = document.createElement('button'); button.textContent = (option.selected ? '当前 · ' : '') + option.label;
    button.onclick = () => act(() => sendInput({ choice: option.id, screen_id: screen })); $('choices').append(button);
  }
}
async function poll() {
  const path = selected(), current = generation;
  if (!path) return;
  if (polling && polling.generation === current) return polling.promise;
  const promise = (async () => {
    try {
      let value;
      do {
        value = await api(path + '/state?after=' + cursor);
        if (current !== generation || path !== selected()) return;
        for (const event of value.events) history.set(event.id, event);
        cursor = value.cursor;
        if (value.events.length) renderHistory();
      } while (value.has_more);
      const output = $('output'), nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 70;
      if (output.textContent !== value.terminal.text) { output.textContent = value.terminal.text || '等待终端输出…'; if (nearBottom) output.scrollTop = output.scrollHeight; }
      renderInteraction(value.interaction, value.screen_id); snapshot = value;
      const observed = value.terminal.observed_at ? new Date(value.terminal.observed_at * 1000).toLocaleString() : '暂无终端快照';
      $('updated').textContent = live() ? '在线 · 更新于 ' + new Date().toLocaleTimeString()
        : (value.session.status === 'stopped' ? '会话已停止' : '设备离线或暂不可用') + ' · 历史快照：' + observed;
      controls();
    } catch (error) {
      if (current !== generation) return;
      snapshot = null; controls(); $('updated').textContent = '读取失败，请刷新'; throw error;
    } finally { if (polling && polling.generation === current) polling = null; }
  })();
  polling = { generation: current, promise }; return promise;
}
async function act(fn) {
  if (busy) return;
  busy = true; generation++; controls(); status('处理中…');
  try { await fn(); status('操作完成'); } catch (error) { status(error.message, true); }
  finally { busy = false; controls(); }
}
function agentsForProject() {
  options($('agent'), (projects.find(p => p.id === $('project').value)?.agents || []).map(name => [name, name])); controls();
}
async function refreshSessions() {
  if (!$('device').value) return;
  const previous = selected(), current = generation, path = base(), values = await api(path);
  if (current !== generation || path !== base()) return;
  options($('session'), values.map(s => [s.id, (s.project ? s.project + ' · ' + s.agent + ' · ' : '') + s.id.slice(0, 11) + (s.status === 'stopped' ? ' · 已停止' : '')]));
  if (previous !== selected()) resetSession();
  await poll();
}
async function loadDevice() {
  $('session').replaceChildren(); resetSession(); projects = []; $('project').replaceChildren(); agentsForProject();
  if (!$('device').value) return;
  const current = generation, values = await api(machine() + '/projects');
  if (current !== generation) return;
  projects = values; options($('project'), projects.map(p => [p.id, p.id])); agentsForProject(); await refreshSessions();
}
async function refreshDevices() {
  const current = generation, old = $('device').value, values = await api('machines');
  if (current !== generation) return;
  devices = values;
  options($('device'), devices.map(d => [d.id, (d.name || d.id) + (d.revoked ? ' · 已解绑' : d.online ? ' · 在线' : ' · 离线')]));
  $('device-hint').hidden = Boolean(devices.length); controls();
  if (old !== $('device').value) await loadDevice();
  else if ($('device').value) {
    if (!projects.length && online()) {
      const values = await api(machine() + '/projects');
      if (current !== generation) return;
      projects = values; options($('project'), projects.map(p => [p.id, p.id])); agentsForProject();
    }
    await refreshSessions();
  }
}
async function sendInput(input) {
  if (!live() || !selected()) throw Error('请先连接设备并刷新终端。');
  const path = selected(), body = { screen_id: snapshot.screen_id, ...input }, identity = JSON.stringify([path, body]);
  if (!pendingInput || pendingInput.identity !== identity) pendingInput = { identity, id: requestId() };
  let failure;
  try { await api(path + '/input', 'POST', { ...body, request_id: pendingInput.id }); pendingInput = null; }
  catch (error) { failure = error; }
  snapshot = null; try { await poll(); } catch (error) { failure ||= error; }
  if (failure) throw failure;
}
function signedOut() {
  accountEpoch++; updates?.close(); updates = null; csrf = ''; user = null; devices = []; projects = [];
  $('workbench').hidden = true; $('account-bar').hidden = true; $('account-login').hidden = false;
  $('device').replaceChildren(); $('session').replaceChildren(); $('project').replaceChildren(); $('agent').replaceChildren();
  $('password').value = ''; $('current-password').value = ''; $('new-password').value = ''; $('account-email').textContent = '';
  resetSession();
}
async function signedIn(result) {
  accountEpoch++; user = result.user; csrf = result.csrf;
  $('password').value = ''; $('account-login').hidden = true; $('account-bar').hidden = false; $('workbench').hidden = false;
  $('account-email').textContent = user.email; $('retention').textContent = '停止会话后历史保留 ' + authSettings.history_days + ' 天；删除对话会清除同步历史。';
  await refreshDevices();
  updates?.close(); updates = new EventSource('/api/updates');
  updates.addEventListener('change', () => { refreshRequested = true; });
  updates.addEventListener('logout', () => { signedOut(); status('登录已失效，请重新登录。', true); });
}
$('login-form').onsubmit = event => {
  event.preventDefault();
  const action = event.submitter?.id === 'register' ? 'register' : 'login';
  act(async () => { const result = await api('auth/' + action, 'POST', { email: $('email').value, password: $('password').value }); await signedIn(result); });
};
$('logout').onclick = () => act(async () => { await api('auth/logout', 'POST'); signedOut(); });
$('password-form').onsubmit = event => {
  event.preventDefault();
  act(async () => { await api('auth/password', 'POST', { current_password: $('current-password').value, new_password: $('new-password').value }); signedOut(); });
};
$('connect').onclick = () => act(async () => { $('session').replaceChildren(); resetSession(); await refreshDevices(); });
$('refresh').onclick = () => act(refreshDevices);
$('device').onchange = () => act(loadDevice);
$('project').onchange = agentsForProject;
$('session').onchange = () => act(async () => { resetSession(); await poll(); });
$('logs').onclick = () => act(poll);
$('create').onclick = () => act(async () => {
  const session = await api(base(), 'POST', { project: $('project').value, agent: $('agent').value });
  await refreshSessions(); $('session').value = session.id; resetSession(); await poll();
});
$('composer').onsubmit = event => {
  event.preventDefault(); if (!$('prompt').value.trim() || !selected() || !live()) return;
  act(async () => {
    const message = $('prompt').value, path = selected();
    if (!pendingSend || pendingSend.message !== message || pendingSend.path !== path) pendingSend = { message, path, id: requestId() };
    let failure;
    try { await api(path + '/send', 'POST', { message, request_id: pendingSend.id }); pendingSend = null; if ($('prompt').value === message) $('prompt').value = ''; }
    catch (error) { failure = error; }
    try { await poll(); } catch (error) { failure ||= error; }
    if (failure) throw failure;
  });
};
$('prompt').onkeydown = event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('composer').requestSubmit(); } };
$('stop').onclick = () => {
  const warning = mode === 'legacy' ? '停止会话会结束 CLI 并清除旧版客户端记录，是否继续？' : '停止会话会结束当前 CLI 和任务，保留历史。是否继续？';
  if (!selected() || !confirm(warning)) return;
  act(async () => { await api(selected() + (mode === 'legacy' ? '' : '/stop'), mode === 'legacy' ? 'DELETE' : 'POST'); await refreshSessions(); });
};
$('delete').onclick = () => {
  if (!selected() || !confirm('删除此对话的同步历史，并结束对应 CLI。设备离线时将在重连后清除本机记录。是否继续？')) return;
  act(async () => { await api(selected(), 'DELETE'); await refreshSessions(); });
};
$('revoke').onclick = () => {
  if (!confirm('解除绑定后，此设备不能继续同步或接收操作，历史保留。客户端需要重新登录。是否继续？')) return;
  act(async () => { await api(machine() + '/binding', 'DELETE'); await refreshDevices(); });
};
document.querySelectorAll('[data-key]').forEach(button => {
  button.onclick = () => { if (button.dataset.key === 'C-c' && !confirm('发送 Ctrl+C 中断当前操作？')) return; act(() => sendInput({ key: button.dataset.key })); };
});
setInterval(async () => {
  if (busy || refreshing || !$('auto').checked || document.visibilityState !== 'visible' || (mode === 'accounts' && !user)) return;
  if (!refreshRequested && Date.now() < nextRefresh) return;
  nextRefresh = Date.now() + (mode === 'accounts' ? 5000 : 2000);
  refreshing = true;
  try {
    if (mode === 'accounts') { refreshRequested = false; await refreshDevices(); }
    else await poll();
  } catch (error) { status(error.message, true); }
  finally { refreshing = false; }
}, 1000);
(async () => {
  try {
    authSettings = await api('auth/config'); mode = authSettings.mode;
    $('legacy-login').hidden = mode !== 'legacy'; $('revoke').hidden = mode === 'legacy'; $('delete').hidden = mode === 'legacy';
    if (mode === 'legacy') { $('workbench').hidden = false; return; }
    $('register').hidden = !authSettings.registration;
    try { await signedIn(await api('auth/me')); }
    catch (error) { if (error.status === 401) signedOut(); else throw error; }
  } catch (error) { status(error.message, true); }
  finally { controls(); }
})();
controls();
