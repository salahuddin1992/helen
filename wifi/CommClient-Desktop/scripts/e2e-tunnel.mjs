/* Full tunnel E2E: REST + WebSocket (socket.io) through the rendezvous. */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const PUB = '28682719245a4';
const BASE = `http://127.0.0.1:9090/t/${PUB}`;
const rand = () => Math.random().toString(36).slice(2, 10);

async function h(method, path, body, tok) {
  const headers = { 'Content-Type': 'application/json' };
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
  catch { return { ok: r.ok, status: r.status, data: t }; }
}

(async () => {
  console.log('[tunnel-e2e] base =', BASE);

  console.log('[tunnel-e2e] /api/health via tunnel');
  const hb = await h('GET', '/api/health');
  console.log('  status=' + hb.status, 'body=' + JSON.stringify(hb.data));
  if (!hb.ok) { console.error('health failed'); process.exit(1); }

  const u = 'tun_' + rand();
  const pw = 'Str0ng!Pass-42';
  console.log('[tunnel-e2e] register via tunnel', u);
  const reg = await h('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
  if (!reg.ok) { console.error('register failed', reg); process.exit(1); }
  console.log('  ok');

  console.log('[tunnel-e2e] login via tunnel');
  const log = await h('POST', '/api/auth/login', { username: u, password: pw });
  if (!log.ok) { console.error('login failed', log); process.exit(1); }
  const tok = log.data.tokens.access_token;
  console.log('  ok, token length=' + tok.length);

  console.log('[tunnel-e2e] socket.io connect via tunnel (WS-upgrade through proxy)');
  // socket.io-client hard-codes /socket.io/ as its path and ignores the
  // URL's path component. To route through the tunnel we pass the origin
  // separately and set `path` to the tunneled /t/<pid>/socket.io/ prefix.
  const origin = BASE.replace(/\/t\/[^/]+$/, '');
  const sock = io(origin, {
    path: `/t/${PUB}/socket.io/`,
    auth: { token: tok },
    transports: ['websocket', 'polling'],
    timeout: 8000,
    reconnection: false,
  });
  await new Promise((resolve, reject) => {
    const tmr = setTimeout(() => reject(new Error('connect timeout')), 10000);
    sock.on('connect', () => { clearTimeout(tmr); resolve(); });
    sock.on('connect_error', (e) => { clearTimeout(tmr); reject(new Error(e.message)); });
  });
  console.log('  connected sid=' + sock.id + ', transport=' + sock.io.engine.transport.name);

  // Try an actual socket.io round-trip — subscribe to nothing, just confirm it stays alive briefly.
  await new Promise((r) => setTimeout(r, 500));
  console.log('  still connected after 500ms:', sock.connected);

  sock.disconnect();
  console.log('[tunnel-e2e] PASS — REST + WS both work through the rendezvous tunnel');
  process.exit(0);
})().catch((e) => { console.error('FAIL', e.message); process.exit(2); });
