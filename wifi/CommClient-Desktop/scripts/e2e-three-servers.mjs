/**
 * Three-server + distributed-clients LAN scenario.
 *
 * Simulates 3 independent Helen-Server instances on the same router by
 * spinning up 3 processes on different ports (3301/3302/3303). Each is
 * told to use its own data dir, JWT secret, and server name.
 *
 * Then 8 clients register + log in — distributed roughly equally across
 * the three servers. Within each server, the clients form a single DM-
 * equivalent group and exchange N messages, measuring per-server fan-out.
 *
 * This is the closest-to-real simulation we can run on a single box —
 * actual cross-router topology would need VMs / netns and is outside the
 * scope of localhost-based testing.
 */

import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';

const SERVER_EXE = 'C:/Users/youse/c/wifi/CommClient-Server/dist/Helen-Server/Helen-Server.exe';
const DATA_ROOT = 'C:/Users/youse/c/wifi/CommClient-Server/data_3server_e2e';
const PORTS = [3301, 3302, 3303];
const CLIENTS_PER_SERVER = [3, 3, 2];  // total 8
const pw = 'Str0ng!Pass-42';
const rand = () => Math.random().toString(36).slice(2, 10);

async function httpJson(base, method, path, body, tok) {
  const headers = { 'Content-Type': 'application/json' };
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const r = await fetch(base + path, { method, headers,
    body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
  catch { return { ok: r.ok, status: r.status, data: t }; }
}

async function waitReady(base, timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + '/api/health', { signal: AbortSignal.timeout(1500) });
      if (r.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 800));
  }
  return false;
}

function spawnServer(port, name) {
  const dataDir = join(DATA_ROOT, name);
  if (existsSync(dataDir)) rmSync(dataDir, { recursive: true, force: true });
  mkdirSync(dataDir, { recursive: true });
  const env = {
    ...process.env,
    PORT: String(port),
    SERVER_NAME: 'Helen-' + name,
    COMMCLIENT_DATA_DIR: dataDir,
    JWT_SECRET: 'testsecret-' + name + '-' + rand(),
    // Disable HTTPS sidecar so we don't fight over 3443 across 3 servers.
    HELEN_HTTPS_DISABLED: '1',
    // Unique ports for UDP/mDNS would require app changes; skip federation
    // cross-talk here and treat each server as a true island. Discovery port
    // conflicts are tolerated — only the first bind wins, others log warn.
  };
  const proc = spawn(SERVER_EXE, [], { env, stdio: 'ignore' });
  return { port, name, proc, base: `http://127.0.0.1:${port}` };
}

async function registerAndLogin(base, uname) {
  const reg = await httpJson(base, 'POST', '/api/auth/register',
    { username: uname, display_name: uname, password: pw });
  const log = await httpJson(base, 'POST', '/api/auth/login',
    { username: uname, password: pw });
  if (!log.ok) throw new Error('login failed on ' + base);
  return {
    token: log.data.tokens.access_token,
    userId: log.data.user?.id || reg.data.user?.id,
  };
}

async function connectSocket(base, tok) {
  const sock = io(base, { auth: { token: tok }, transports: ['websocket'],
    timeout: 8000, reconnection: false });
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('connect timeout')), 10000);
    sock.on('connect', () => { clearTimeout(t); res(); });
    sock.on('connect_error', (e) => { clearTimeout(t); rej(new Error(e.message)); });
  });
  return sock;
}

function cleanup(servers) {
  for (const s of servers) {
    try { s.proc.kill('SIGKILL'); } catch {}
  }
}

(async () => {
  console.log('[3srv] spawning 3 servers on ports', PORTS.join(','));
  const servers = PORTS.map((p, i) => spawnServer(p, ['Alpha', 'Bravo', 'Charlie'][i]));
  try {
    console.log('[3srv] waiting for health...');
    for (const s of servers) {
      const up = await waitReady(s.base);
      console.log('  ' + s.name + ' on ' + s.port + ': ' + (up ? 'UP' : 'TIMEOUT'));
      if (!up) throw new Error(s.name + ' never came up');
    }

    // Per-server: register N users, create one channel, join all, and
    // have user[0] send a message — measure how many peers receive it.
    const totals = [];
    for (let si = 0; si < servers.length; si++) {
      const s = servers[si];
      const n = CLIENTS_PER_SERVER[si];
      console.log('[3srv] on ' + s.name + ': spin ' + n + ' users');
      const users = [];
      for (let i = 0; i < n; i++) {
        const u = 's' + si + '_u' + i + '_' + rand();
        const { token: tok, userId } = await registerAndLogin(s.base, u);
        const sock = await connectSocket(s.base, tok);
        users.push({ u, tok, userId, sock });
      }

      const memberIds = users.map(u => u.userId).filter(Boolean);
      const ch = await httpJson(s.base, 'POST', '/api/channels',
        { type: 'group', name: s.name + '-grp', member_ids: memberIds.slice(1) },
        users[0].tok);
      if (!ch.ok) throw new Error('create group on ' + s.name + ': ' + ch.status + ' ' + JSON.stringify(ch.data).slice(0, 200));
      const channelId = ch.data.id || ch.data.channel?.id;

      // Subscribe receivers. Server emits 'v2_chat:new_message' to channel
      // room members (see sync_handlers.py:320).
      const received = new Array(n - 1).fill(0);
      const deliveryPromises = [];
      for (let i = 1; i < n; i++) {
        deliveryPromises.push(new Promise((res) => {
          const handler = (msg) => {
            if (msg && msg.channel_id === channelId) {
              received[i - 1]++;
              if (received[i - 1] >= 1) res();
            }
          };
          users[i].sock.on('v2_chat:new_message', handler);
          users[i].sock.on('v2_chat_new_message', handler);
          users[i].sock.on('new_message', handler);
        }));
      }

      // User 0 sends
      const t0 = Date.now();
      const ack = await new Promise((res) => {
        const t = setTimeout(() => res({ timeout: true }), 5000);
        users[0].sock.emit('v2_chat_send_message', {
          channel_id: channelId,
          content: 'hi from ' + s.name,
          client_message_id: 'cid_' + rand(),
        }, (ack) => { clearTimeout(t); res(ack); });
      });
      const ackMs = Date.now() - t0;

      // Wait for fan-out
      await Promise.race([
        Promise.all(deliveryPromises),
        new Promise((r) => setTimeout(r, 4000)),
      ]);
      const fanOutMs = Date.now() - t0;
      const delivered = received.filter(c => c > 0).length;
      console.log('  ' + s.name + ': ack=' + ackMs + 'ms  fan-out=' + delivered + '/' + (n - 1) + '  total=' + fanOutMs + 'ms');
      totals.push({ name: s.name, n, ack: ackMs, delivered, expected: n - 1, fanOut: fanOutMs });

      // disconnect all
      for (const usr of users) try { usr.sock.disconnect(); } catch {}
    }

    console.log('\n=== 3-SERVER SCENARIO RESULTS ===');
    for (const r of totals) {
      const ok = r.delivered === r.expected ? 'PASS' : 'FAIL';
      console.log('  ' + ok + ' ' + r.name + ': N=' + r.n + ' delivered=' + r.delivered + '/' + r.expected + ' ack=' + r.ack + 'ms fan-out=' + r.fanOut + 'ms');
    }
    const overall = totals.every((r) => r.delivered === r.expected);
    console.log('  OVERALL: ' + (overall ? 'PASS' : 'FAIL'));
    cleanup(servers);
    process.exit(overall ? 0 : 1);
  } catch (e) {
    console.error('[3srv] ERROR:', e.message);
    cleanup(servers);
    process.exit(2);
  }
})();
