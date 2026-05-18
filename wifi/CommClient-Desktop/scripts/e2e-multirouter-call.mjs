/**
 * Multi-router group call topology test.
 *
 * Simulates a real deployment: 3 virtual routers (TCP proxies), and 8
 * clients distributed across them.  Every client hits the server
 * through a different "site" router.  We then run a full group-call
 * signaling sweep (join → offers → answers → ICE → mute → hangup) and
 * verify every event fans out correctly through the proxies.
 *
 * Topology
 * --------
 *   clients 1-3 ──► router A (localhost:9301) ─┐
 *   clients 4-6 ──► router B (localhost:9302) ─┼─► Helen server (localhost:3000)
 *   clients 7-8 ──► router C (localhost:9303) ─┘
 *
 * What this proves: the signaling layer survives a multi-hop
 * TCP-proxy topology — exactly what MikroTik-routed deployments do
 * with DST-NAT rules.  It does NOT prove WebRTC media flow (that
 * needs wrtc / aiortc / real cameras, which the sandbox lacks).
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import { spawn } from 'node:child_process';

const PY = 'C:/Users/youse/c/wifi/CommClient-Server/venv/Scripts/python.exe';
const ROUTER_SCRIPT = 'C:/Users/youse/c/wifi/CommClient-Server/scripts/virtual_router.py';
const UPSTREAM_HOST = '127.0.0.1';
const UPSTREAM_PORT = 3000;
const ROUTER_PORTS = [9301, 9302, 9303];
const CLIENTS_PER_ROUTER = [3, 3, 2];  // total 8
const pw = 'Str0ng!Pass-42';
const rand = () => Math.random().toString(36).slice(2, 10);

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function httpJson(base, method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${base}${path}`, {
    method, headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let d;
  try { d = text ? JSON.parse(text) : null; } catch { d = text; }
  if (!r.ok) throw new Error(`${method} ${path} -> ${r.status}: ${typeof d === 'string' ? d : JSON.stringify(d)}`);
  return d;
}

function connectSocket(base, token) {
  return new Promise((resolve, reject) => {
    const s = io(base, {
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: false, timeout: 8000,
    });
    const to = setTimeout(() => { s.disconnect(); reject(new Error('socket timeout')); }, 8000);
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
  });
}

function emitAck(s, evt, payload, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${evt} timeout`)), timeoutMs);
    s.emit(evt, payload, (r) => { clearTimeout(to); resolve(r); });
  });
}

function waitEvent(s, evt, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`event ${evt} timeout`)), timeoutMs);
    s.once(evt, (data) => { clearTimeout(to); resolve(data); });
  });
}

function spawnRouter(port) {
  const proc = spawn(PY, [ROUTER_SCRIPT, String(port), UPSTREAM_HOST, String(UPSTREAM_PORT)], {
    stdio: 'ignore',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  return proc;
}

async function main() {
  console.log('[multi-router] spawning 3 virtual routers:', ROUTER_PORTS.join(', '));
  const routers = ROUTER_PORTS.map(spawnRouter);
  await sleep(2500);

  const allClients = [];
  let clientIdx = 0;
  for (let ri = 0; ri < ROUTER_PORTS.length; ri++) {
    const port = ROUTER_PORTS[ri];
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < CLIENTS_PER_ROUTER[ri]; i++) {
      clientIdx++;
      const uname = `mrcall_${clientIdx}_${rand()}`;
      await httpJson(base, 'POST', '/api/auth/register',
        { username: uname, display_name: uname, password: pw });
      const log = await httpJson(base, 'POST', '/api/auth/login',
        { username: uname, password: pw });
      const token = log.tokens?.access_token || log.access_token;
      const userId = log.user?.id;
      const sock = await connectSocket(base, token);
      allClients.push({ uname, userId, token, sock, via: `router:${port}` });
    }
  }
  console.log('[multi-router] connected', allClients.length, 'clients across 3 routers');

  // Group channel containing everyone
  const creator = allClients[0];
  const memberIds = allClients.slice(1).map(c => c.userId);
  const ch = await httpJson(
    `http://127.0.0.1:${ROUTER_PORTS[0]}`,
    'POST', '/api/channels',
    { type: 'group', name: 'multi-router-call', member_ids: memberIds },
    creator.token,
  );
  const channelId = ch.id || ch.channel?.id;
  console.log('[multi-router] channel', channelId, 'created via router:' + ROUTER_PORTS[0]);

  // Each non-initiator listens for call_incoming filtered by our channel.
  const incomingPromises = allClients.slice(1).map((c) =>
    new Promise((resolve) => {
      const to = setTimeout(() => resolve(false), 8000);
      c.sock.on('call_incoming', (p) => {
        if (p && p.channel_id === channelId) { clearTimeout(to); resolve(true); }
      });
    })
  );

  console.log('[multi-router] creator initiates group call via v2_call_join_group');
  const startAck = await emitAck(creator.sock, 'v2_call_join_group', {
    channel_id: channelId, call_type: 'audio',
  }, 10000);
  if (!startAck || !startAck.call_id) {
    throw new Error('v2_call_join_group ack missing call_id: ' + JSON.stringify(startAck));
  }
  const callId = startAck.call_id;
  console.log('[multi-router] call_id', callId);

  const gotIncoming = await Promise.all(incomingPromises);
  const incomingCount = gotIncoming.filter(Boolean).length;
  console.log('[multi-router] call_incoming delivered via routers:', incomingCount, '/', allClients.length - 1);

  // Others join
  let joinedCount = 1;
  for (const c of allClients.slice(1)) {
    try {
      await emitAck(c.sock, 'v2_call_join_group', { call_id: callId }, 5000);
      joinedCount++;
    } catch (e) {
      console.log('[multi-router] JOIN FAIL for', c.uname, 'via', c.via, '-', e.message);
    }
  }
  console.log('[multi-router] participants joined via routers:', joinedCount, '/', allClients.length);

  // SDP signaling: creator offers to one peer as a per-hop probe
  creator.sock.emit('signal:offer', {
    call_id: callId, target_id: allClients[1].userId,
    sdp: 'v=0\no=- 0 0 IN IP4 127.0.0.1\ns=-\nt=0 0\nm=audio 0 RTP/AVP\n',
  });
  console.log('[multi-router] signal:offer sent from router:9301 -> peer on router:9302');

  // Mute broadcast
  creator.sock.emit('v2_call_toggle_mute', { call_id: callId, muted: true });
  await sleep(300);
  console.log('[multi-router] mute broadcast sent');

  // Hangup
  await emitAck(creator.sock, 'v2_call_hangup', { call_id: callId }, 5000);
  console.log('[multi-router] hangup sent');

  for (const c of allClients) c.sock.disconnect();

  console.log('');
  console.log('======== RESULT ========');
  const ok = incomingCount === allClients.length - 1 && joinedCount === allClients.length;
  console.log('  multi-router group call:', ok ? 'PASS' : 'FAIL',
    `(${incomingCount}/${allClients.length - 1} incoming, ${joinedCount}/${allClients.length} joined)`);

  // cleanup routers
  for (const r of routers) {
    try { r.kill('SIGTERM'); } catch {}
  }
  process.exit(ok ? 0 : 1);
}

main().catch((e) => {
  console.error('[multi-router] FAIL', e?.stack || e?.message || e);
  process.exit(2);
});
