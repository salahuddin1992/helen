/* Simulates the Electron client's socket.manager passing a rendezvous URL directly. */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = `http://127.0.0.1:9090/t/28682719245a4`;
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
  const u = 'tun2_' + rand();
  const pw = 'Str0ng!Pass-42';
  await h('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
  const log = await h('POST', '/api/auth/login', { username: u, password: pw });
  const tok = log.data.tokens.access_token;
  console.log('[v2] logged in as', u);

  // Emulate the socket.manager logic verbatim.
  const tunnelMatch = BASE.match(/^(https?:\/\/[^/]+)(\/t\/[^/]+)\/?$/i);
  const origin = tunnelMatch ? tunnelMatch[1] : BASE;
  const path = tunnelMatch ? `${tunnelMatch[2]}/socket.io/` : '/socket.io/';
  console.log('[v2] derived origin=', origin, 'path=', path);

  const sock = io(origin, { path, auth: { token: tok }, transports: ['websocket', 'polling'], timeout: 8000, reconnection: false });
  await new Promise((res, rej) => {
    const tm = setTimeout(() => rej(new Error('timeout')), 10000);
    sock.on('connect', () => { clearTimeout(tm); res(); });
    sock.on('connect_error', (e) => { clearTimeout(tm); rej(new Error(e.message)); });
  });
  console.log('[v2] connected sid=' + sock.id + ' transport=' + sock.io.engine.transport.name);

  // Round-trip: send a v2_chat_send_message to nonexistent channel, expect an error back (not a disconnect).
  const r = await new Promise((res) => {
    const tm = setTimeout(() => res({timeout:true}), 3000);
    sock.emit('v2_chat_send_message', { channel_id: 'does-not-exist', content: 'x', client_message_id: 'cid_' + rand() }, (ack) => {
      clearTimeout(tm); res(ack);
    });
  });
  console.log('[v2] ack or error from server:', r);

  sock.disconnect();
  console.log('[v2] PASS — autorouted WS through tunnel works');
})();
