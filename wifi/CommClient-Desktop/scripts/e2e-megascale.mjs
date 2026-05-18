/**
 * Megascale stress test — push participants upward until something breaks.
 *
 * Records for each size N:
 *   - auth_ms, socket_ms, join_ms
 *   - process RSS / CPU peak (via Get-Process)
 *   - first failure if any (socket err, join rejection, timeout)
 *   - chat broadcast reach (how many peers actually receive a message)
 *
 * The goal is an empirical scalability ceiling, not a pass/fail.
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import { spawnSync } from 'child_process';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const pw = 'Str0ng!Pass-42';
const STEPS = (process.env.STEPS || '100,500,1000,2000,5000').split(',').map(Number);
const AUTH_BATCH = parseInt(process.env.AUTH_BATCH || '50', 10);
const CONNECT_BATCH = parseInt(process.env.CONNECT_BATCH || '200', 10);
const JOIN_BATCH = parseInt(process.env.JOIN_BATCH || '50', 10);
const CHAT_WAIT_MS = parseInt(process.env.CHAT_WAIT_MS || '10000', 10);

const rand = () => Math.random().toString(36).slice(2, 10);
const log = (...a) => console.log('[mega]', ...a);

async function httpJson(method, path, body, token, timeoutMs = 15000) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${BASE}${path}`, {
      method, headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
    const text = await r.text();
    let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) throw new Error(`${method} ${path} → ${r.status}: ${typeof data === 'string' ? data.slice(0, 120) : JSON.stringify(data).slice(0, 120)}`);
    return data;
  } finally {
    clearTimeout(to);
  }
}

function connectSocket(token, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const s = io(BASE, {
      auth: { token },
      transports: ['websocket'],
      reconnection: false,
      timeout: timeoutMs,
      forceNew: true,
    });
    const to = setTimeout(() => { s.disconnect(); reject(new Error('socket timeout')); }, timeoutMs);
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
  });
}

function ack(s, ev, p, t = 15000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${ev} timeout`)), t);
    s.emit(ev, p, (r) => { clearTimeout(to); resolve(r); });
  });
}
const now = () => performance.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getServerRss() {
  try {
    const res = spawnSync('powershell.exe', [
      '-NoProfile', '-Command',
      "(Get-Process -Name CommClient-Server -ErrorAction SilentlyContinue | Select-Object -First 1).WorkingSet64",
    ], { encoding: 'utf8', timeout: 3000 });
    const out = (res.stdout || '').trim();
    return out ? parseInt(out, 10) : null;
  } catch { return null; }
}

async function runScale(N) {
  const t0 = now();
  log(`N=${N} — starting`);

  // ── 1. Register in parallel batches ────────────────────────────
  const unames = Array.from({ length: N }, (_, i) => `m${N}_${i}_${rand()}`);
  const users = [];
  let authFails = 0;
  for (let i = 0; i < N; i += AUTH_BATCH) {
    const batch = unames.slice(i, i + AUTH_BATCH);
    const results = await Promise.allSettled(batch.map(async (u) => {
      await httpJson('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
      const r = await httpJson('POST', '/api/auth/login', { username: u, password: pw });
      return { uname: u, userId: r.user.id, token: r.tokens?.access_token || r.access_token };
    }));
    for (const r of results) {
      if (r.status === 'fulfilled') users.push(r.value);
      else authFails++;
    }
    if (i % 500 === 0 && i > 0) log(`  auth progress: ${users.length}/${N}, fails=${authFails}`);
  }
  const tAuth = now() - t0;
  log(`  auth done: ${users.length}/${N} ok, ${authFails} fails, ${Math.round(tAuth)}ms`);
  if (users.length < N * 0.5) return { N, phase: 'auth', ok: users.length, fails: authFails, tAuth_ms: Math.round(tAuth), crashed: true };

  // ── 2. Create one group channel for all ─────────────────────────
  const A = users[0];
  const t1 = now();
  let channel;
  try {
    channel = await httpJson('POST', '/api/channels', {
      type: 'group', name: `mega-${N}-${rand()}`, member_ids: users.slice(1).map((u) => u.userId),
    }, A.token, 60000);
  } catch (e) {
    return { N, phase: 'channel', error: e.message, users: users.length };
  }
  const tChannel = now() - t1;
  log(`  channel created: ${Math.round(tChannel)}ms`);

  // ── 3. Connect sockets in batches ───────────────────────────────
  const t2 = now();
  const sockets = [];
  let sockFails = 0;
  for (let i = 0; i < users.length; i += CONNECT_BATCH) {
    const slice = users.slice(i, i + CONNECT_BATCH);
    const results = await Promise.allSettled(slice.map((u) => connectSocket(u.token)));
    for (const r of results) {
      if (r.status === 'fulfilled') sockets.push(r.value);
      else { sockets.push(null); sockFails++; }
    }
    if (i % 1000 === 0 && i > 0) log(`  sock progress: ${sockets.filter(Boolean).length}/${users.length}, fails=${sockFails}`);
  }
  const tSockets = now() - t2;
  log(`  sockets connected: ${sockets.filter(Boolean).length}/${users.length} ok, ${sockFails} fails, ${Math.round(tSockets)}ms`);

  // ── 4. Join the call ─────────────────────────────────────────────
  const t3 = now();
  let rejectedError = null;
  let joinFails = 0;
  let joinOk = 0;
  const aIdx = 0;
  if (!sockets[aIdx]) return { N, phase: 'sockets', users: users.length, sockets: sockets.filter(Boolean).length };
  const aJoin = await ack(sockets[aIdx], 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 20000)
    .catch((e) => ({ error: e.message }));
  if (aJoin?.error) return { N, phase: 'A_join', error: aJoin.error, users: users.length };
  joinOk = 1;
  const callId = aJoin.call_id;

  for (let i = 1; i < sockets.length; i += JOIN_BATCH) {
    const slice = sockets.slice(i, i + JOIN_BATCH);
    const results = await Promise.allSettled(slice.map((s) =>
      s ? ack(s, 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 20000) : Promise.reject(new Error('no sock')),
    ));
    for (const r of results) {
      if (r.status === 'fulfilled' && !r.value?.error) joinOk++;
      else {
        joinFails++;
        if (!rejectedError) rejectedError = r.status === 'fulfilled' ? r.value?.error : r.reason?.message;
      }
    }
    if (i % 1000 === 1 && i > 1) log(`  join progress: ${joinOk}/${users.length}, fails=${joinFails}`);
  }
  const tJoin = now() - t3;
  log(`  joins: ${joinOk}/${users.length} ok, ${joinFails} fails, ${Math.round(tJoin)}ms`);

  const rssAtPeak = getServerRss();

  // ── 5. Chat broadcast reach ─────────────────────────────────────
  let chatReach = 0;
  let chatP80 = null;
  const liveSockets = sockets.filter(Boolean);
  if (liveSockets.length > 10) {
    const seenAt = [];
    const tChat = now();
    const listeners = liveSockets.slice(1).map((s) => {
      const h = (p) => {
        if (p?.channel_id === channel.id) {
          seenAt.push(now() - tChat);
          for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.off(ev, h);
        }
      };
      for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.on(ev, h);
      return h;
    });
    await ack(liveSockets[0], 'v2_chat_send_message', {
      channel_id: channel.id, content: `mega-N${N}`, type: 'text', client_id: `mega-${N}-${rand()}`,
    }, 10000).catch(() => {});
    await sleep(CHAT_WAIT_MS);
    chatReach = seenAt.length;
    if (seenAt.length) {
      seenAt.sort((a, b) => a - b);
      chatP80 = Math.round(seenAt[Math.floor(seenAt.length * 0.8)]);
    }
    log(`  chat reach: ${chatReach}/${liveSockets.length - 1}, p80=${chatP80}ms`);
  }

  // ── 6. Cleanup ──────────────────────────────────────────────────
  try { await ack(sockets[aIdx], 'v2_call_hangup', { call_id: callId }, 5000); } catch {}
  for (const s of sockets) { if (s) try { s.disconnect(); } catch {} }
  // give the server a moment to release sockets before next round
  await sleep(2000);

  return {
    N,
    users_ok: users.length,
    auth_fails: authFails,
    sockets_ok: sockets.filter(Boolean).length,
    sock_fails: sockFails,
    joined_ok: joinOk,
    join_fails: joinFails,
    first_reject: rejectedError ? String(rejectedError).slice(0, 80) : null,
    tAuth_ms: Math.round(tAuth),
    tChannel_ms: Math.round(tChannel),
    tSockets_ms: Math.round(tSockets),
    tJoin_ms: Math.round(tJoin),
    chat_reach: chatReach,
    chat_p80_ms: chatP80,
    server_rss_mb: rssAtPeak ? Math.round(rssAtPeak / 1024 / 1024) : null,
  };
}

async function main() {
  log('BASE', BASE, 'STEPS', STEPS);
  const h = await httpJson('GET', '/api/health');
  log('health', h.status, 'version', h.version);
  const rssStart = getServerRss();
  log('server rss at start:', rssStart ? Math.round(rssStart / 1024 / 1024) + 'MB' : '?');

  const results = [];
  for (const N of STEPS) {
    try {
      const r = await runScale(N);
      results.push(r);
      log('RESULT', JSON.stringify(r));
      if (r.crashed || r.join_fails > N * 0.2) {
        log('degradation detected — stopping ascent');
        break;
      }
    } catch (e) {
      log(`N=${N} CRASH:`, e.message);
      results.push({ N, crash: e.message });
      break;
    }
  }

  console.log('\n=== SUMMARY ===');
  console.table(results.map((r) => ({
    N: r.N,
    usersOk: r.users_ok ?? '-',
    socksOk: r.sockets_ok ?? '-',
    joinedOk: r.joined_ok ?? '-',
    joinFails: r.join_fails ?? '-',
    authMs: r.tAuth_ms ?? '-',
    sockMs: r.tSockets_ms ?? '-',
    joinMs: r.tJoin_ms ?? '-',
    chatReach: r.chat_reach ?? '-',
    chatP80: r.chat_p80_ms ?? '-',
    rssMb: r.server_rss_mb ?? '-',
    firstReject: (r.first_reject || r.crash || r.error || '').slice(0, 50),
  })));
  process.exit(0);
}

main().catch((e) => { console.error('[mega] FATAL', e.message); process.exit(1); });
