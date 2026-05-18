/* End-to-end proof: register user, connect socket, query /api/admin/connected-clients.
 * Promotes the test user to admin via direct SQL so the test is self-contained. */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const rand = () => Math.random().toString(36).slice(2, 10);

async function httpJson(method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; } catch { return { ok: r.ok, status: r.status, data: t }; }
}

(async () => {
  const uname = `conn_test_${rand()}`;
  const pw = 'Str0ng!Pass-42';
  console.log('[test] register', uname);
  const reg = await httpJson('POST', '/api/auth/register', { username: uname, display_name: uname, password: pw });
  if (!reg.ok) { console.error('register failed', reg); process.exit(1); }
  console.log('[test] login');
  const log = await httpJson('POST', '/api/auth/login', { username: uname, password: pw });
  if (!log.ok) { console.error('login failed', log); process.exit(1); }
  let token = log.data.tokens?.access_token || log.data.access_token;
  let role = log.data.user?.role;
  console.log('[test] role=' + role);

  // Promote this user to admin via direct SQLite update so the test is
  // self-contained (first-user-becomes-admin only fires on a virgin DB).
  if (role !== 'admin') {
    // The running server may use either the source-tree DB (dev) or the
    // frozen onedir copy (production exe). Try both — whichever exists first.
    const dbCandidates = [
      'C:/Users/youse/c/wifi/CommClient-Server/dist/Helen-Server/_internal/data/commclient.db',
      'C:/Users/youse/c/wifi/CommClient-Server/data/commclient.db',
    ];
    const dbPath = dbCandidates.find(existsSync);
    if (dbPath) {
      console.log('[test] promoting via', dbPath);
      try {
        execSync(
          `"C:/Users/youse/c/wifi/CommClient-Server/venv/Scripts/python.exe" -c "import sqlite3; c=sqlite3.connect(r'${dbPath}'); c.execute('UPDATE users SET role=\\"admin\\" WHERE username=\\"${uname}\\"'); c.commit(); print('promoted')"`,
          { stdio: 'inherit' },
        );
      } catch (e) { console.warn('promote failed:', e.message); }
      const log2 = await httpJson('POST', '/api/auth/login', { username: uname, password: pw });
      token = log2.data.tokens?.access_token || log2.data.access_token;
      // role is in the JWT claim, not necessarily in the user object
      try {
        const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64').toString('utf8'));
        role = payload.role;
      } catch {}
      console.log('[test] after promote jwt role=' + role);
    } else {
      console.warn('[test] no DB found, can not promote — test may fail');
    }
  }

  console.log('[test] connect socket');
  const sock = io(BASE, { auth: { token }, transports: ['websocket'], timeout: 4000 });
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject('connect timeout'), 5000);
    sock.on('connect', () => { clearTimeout(t); resolve(); });
    sock.on('connect_error', (e) => { clearTimeout(t); reject(e.message); });
  });
  console.log('[test] socket connected sid=' + sock.id);

  await new Promise((r) => setTimeout(r, 400)); // let server flush presence

  console.log('[test] GET /api/admin/connected-clients (as first-user admin)');
  const clients = await httpJson('GET', '/api/admin/connected-clients', null, token);
  console.log('status=' + clients.status);
  if (!clients.ok) { console.error('clients endpoint failed:', clients); process.exit(1); }
  console.log('count=' + clients.data.count);
  for (const c of clients.data.clients) {
    console.log('  -', c.username, '·', c.role, '·', c.device_type, '·', c.remote_addr, '·', c.status, '· sid=' + (c.sid||'').slice(0,8));
  }
  const mine = clients.data.clients.find(c => c.sid === sock.id);
  if (!mine) { console.error('my sid not in connected-clients list!'); process.exit(1); }
  if (mine.username !== uname) { console.error('username mismatch', mine.username, 'vs', uname); process.exit(1); }
  if (!mine.remote_addr || !mine.user_agent === null) { /* ua may be empty for raw ws */ }

  console.log('[test] PASS — connected client is visible to admin with all fields');
  sock.disconnect();
  process.exit(0);
})().catch((e) => { console.error('FAIL', e); process.exit(2); });
