import { io } from '../node_modules/socket.io-client/build/esm/index.js';
const TUNNEL_URL = process.env.TUNNEL_URL;
const pw = 'Str0ng!Pass-42';
const rand = () => Math.random().toString(36).slice(2, 10);

async function h(method, path, body, tok) {
  const headers = { 'Content-Type': 'application/json' };
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const r = await fetch(TUNNEL_URL + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
  catch { return { ok: r.ok, status: r.status, data: t }; }
}

(async () => {
  console.log('[ios-bridge] === iPhone via Bridge tunnel ===');
  console.log('[ios-bridge] tunnel URL:', TUNNEL_URL);
  // Step 1: bridgeContinue handler probes /api/discovery
  const d = await h('GET', '/api/discovery');
  console.log('[ios-bridge] discovery via tunnel:', d.ok ? 'OK ' + d.data.name : 'FAIL');
  // Step 2: register + login
  const u = 'iphone_bridge_' + rand();
  await h('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
  const log = await h('POST', '/api/auth/login', { username: u, password: pw });
  if (!log.ok) { console.error('login failed'); process.exit(1); }
  const tok = log.data.tokens.access_token;
  console.log('[ios-bridge] signed in as', u);
  // Step 3: socket.io with rendezvous-aware path (same code path as app.js)
  const tunnelMatch = TUNNEL_URL.match(/^(https?:\/\/[^/]+)(\/t\/[A-Za-z0-9_-]+)\/?$/i);
  const origin = tunnelMatch ? tunnelMatch[1] : TUNNEL_URL;
  const path   = tunnelMatch ? `${tunnelMatch[2]}/socket.io/` : '/socket.io/';
  console.log('[ios-bridge] socket origin:', origin);
  console.log('[ios-bridge] socket path:', path);
  const sock = io(origin, { path, auth: { token: tok }, transports: ['websocket'], reconnection: false });
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('timeout')), 10000);
    sock.on('connect', () => { clearTimeout(t); res(); });
    sock.on('connect_error', (e) => { clearTimeout(t); rej(e); });
  });
  console.log('[ios-bridge] socket connected sid=' + sock.id + ' transport=' + sock.io.engine.transport.name);
  sock.disconnect();
  console.log('[ios-bridge] === PASS — iPhone reaches Helen via Bridge ===');
})().catch((e) => { console.error('FAIL', e.message); process.exit(2); });
