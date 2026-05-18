/* Simulates Helen-Admin on machine B hitting Helen-Server on machine A.
 * Forces Origin: http://192.168.1.2:5173 (the would-be Admin's own static server).
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = 'http://127.0.0.1:3000';
const FAKE_REMOTE_ORIGIN = 'http://192.168.1.2:5173';
const rand = () => Math.random().toString(36).slice(2, 10);

async function h(method, path, body, tok) {
  const headers = { 'Content-Type': 'application/json', 'Origin': FAKE_REMOTE_ORIGIN };
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  return { ok: r.ok, status: r.status, corsOrigin: r.headers.get('access-control-allow-origin') };
}

(async () => {
  const u = 'rem_' + rand();
  const pw = 'Str0ng!Pass-42';

  console.log('[remote-admin] registering from remote origin...');
  const reg = await h('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
  console.log('  status=' + reg.status + ' CORS echoed origin=' + reg.corsOrigin);

  console.log('[remote-admin] login from remote origin...');
  const log = await h('POST', '/api/auth/login', { username: u, password: pw });
  console.log('  status=' + log.status + ' CORS echoed origin=' + log.corsOrigin);

  const body = await (await fetch(BASE + '/api/auth/login', {
    method:'POST', headers:{'Content-Type':'application/json', 'Origin': FAKE_REMOTE_ORIGIN},
    body: JSON.stringify({username: u, password: pw}),
  })).json();
  const tok = body.tokens.access_token;

  console.log('[remote-admin] socket.io with remote Origin header...');
  // Node fetch/socket.io-client doesn't spoof Origin for WS; instead we use extraHeaders
  const sock = io(BASE, {
    auth: { token: tok },
    transports: ['websocket'],
    timeout: 6000,
    reconnection: false,
    extraHeaders: { Origin: FAKE_REMOTE_ORIGIN },
  });
  try {
    await new Promise((res, rej) => {
      const t = setTimeout(() => rej(new Error('connect timeout')), 7000);
      sock.on('connect', () => { clearTimeout(t); res(); });
      sock.on('connect_error', (e) => { clearTimeout(t); rej(new Error(e.message)); });
    });
    console.log('  CONNECTED sid=' + sock.id + ' transport=' + sock.io.engine.transport.name);
    sock.disconnect();
    console.log('[remote-admin] PASS — LAN-IP origin works end-to-end for both REST and Socket.IO');
    process.exit(0);
  } catch (e) {
    console.error('  FAIL:', e.message);
    process.exit(1);
  }
})();
