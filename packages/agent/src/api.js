export async function serverRequest(relay, path, { method = 'POST', body, device, token, fetcher = fetch } = {}) {
  const url = new URL(relay);
  url.protocol = url.protocol === 'wss:' ? 'https:' : url.protocol === 'ws:' ? 'http:' : url.protocol;
  url.pathname = path; url.search = ''; url.hash = '';
  const headers = { 'Content-Type': 'application/json' };
  if (token) { headers.Authorization = 'Bearer ' + token; headers['X-Device-ID'] = device; }
  let response;
  try { response = await fetcher(url, { method, headers, redirect: 'error', body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) }); }
  catch { throw Error('无法连接服务端，请检查地址、HTTPS 证书和网络。'); }
  let result;
  try { result = await response.json(); }
  catch { throw Error('服务端返回异常，请确认服务端已升级。'); }
  if (!response.ok) {
    const error = Error(typeof result.detail === 'string' ? result.detail : '登录参数无效，请检查邮箱和密码（密码至少 12 位）。');
    error.status = response.status; throw error;
  }
  return result;
}
