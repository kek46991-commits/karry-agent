// Shared by the independently deployed monitor and worker apps.
const API = '/api/autonomy';
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v; else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat()) if (child != null) node.append(child instanceof Node ? child : String(child));
  return node;
}
async function api(path, options = {}) {
  const headers = {'X-Karry-Device': '1', ...(options.body && !(options.body instanceof Blob) ? {'Content-Type': 'application/json'} : {}), ...(options.headers || {})};
  const response = await fetch(API + path, {...options, headers, credentials: 'same-origin', cache: 'no-store'});
  let data = null;
  if ((response.headers.get('content-type') || '').includes('json')) data = await response.json().catch(() => null);
  if (!response.ok) { const err = new Error((data && data.detail) || `エラー (${response.status})`); err.status = response.status; throw err; }
  return data;
}
function ago(ts) {
  if (!ts) return 'まだ接続なし';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? `${s}秒前` : s < 3600 ? `${Math.round(s / 60)}分前` : `${Math.round(s / 3600)}時間前`;
}
const STATUS_JA = {planned: '準備中', running: '実行中', paused: '一時停止', completed: '完了', failed: '失敗', stopped: '停止',
  pending: '待機', done: '完了', skipped: '中止', blocked: '保留'};
function setupDevice({role, onReady}) {
  const setup = document.getElementById('setup'), app = document.getElementById('app');
  const form = document.getElementById('enrollForm'), error = document.getElementById('enrollError');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.textContent = '';
    const button = form.querySelector('button'); button.disabled = true; button.textContent = '確認中…';
    try {
      const data = await api('/devices/enroll', {method: 'POST', body: JSON.stringify({role, name: form.name.value, password: form.password.value})});
      form.password.value = '';
      setup.hidden = true; app.hidden = false; onReady(data.device);
    } catch (e) { error.textContent = e.message; } finally { button.disabled = false; button.textContent = '利用開始'; }
  });
  api('/device/me').then((data) => {
    if (data.device.role !== role) throw Object.assign(new Error('role'), {status: 401});
    app.hidden = false; onReady(data.device);
  }).catch(() => { setup.hidden = false; });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/device-sw.js', {scope: window.KARRY_DEVICE_SCOPE || location.pathname}).catch(() => {});
  let deferred = null;
  const install = document.getElementById('installButton');
  addEventListener('beforeinstallprompt', (event) => { event.preventDefault(); deferred = event; install.hidden = false; });
  install.addEventListener('click', async () => { if (deferred) { deferred.prompt(); await deferred.userChoice; deferred = null; install.hidden = true; } });
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone || /KarryDevice\//.test(navigator.userAgent);
  const hint = document.getElementById('installHint');
  if (hint && !standalone) hint.hidden = false;
}
async function forgetDevice() {
  if (!confirm('この端末の登録を解除しますか？もう一度使うにはパスワードが必要です。')) return;
  await api('/device/logout', {method: 'POST'}).catch(() => {});
  location.reload();
}
