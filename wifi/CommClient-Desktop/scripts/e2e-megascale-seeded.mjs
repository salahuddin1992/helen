/**
 * Megascale — using pre-seeded users (bypasses auth).
 *
 * Loads tokens from /tmp/seeded_users.json and pushes socket + call-join
 * counts progressively. Reports the real ceiling of the signaling layer
 * without the bcrypt/SQLite write-lock overhead.
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import { spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const SEED_FILE = process.env.SEED_FILE || path.join(os.tmpdir(), 'seeded_users.json');
const STEPS = (process.env.STEPS || '100,500,1000,2000,5000,10000').split(',').map(Number);
const CONNECT_BATCH = parseInt(process.env.CONNECT_BATCH || '500', 10);
const JOIN_BATCH = parseInt(process.env.JOIN_BATCH || '200', 10);
const CHAT_WAIT_MS = parseInt(process.env.CHAT_WAIT_MS || '15000', 10);

const rand = () => Math.random().toString(36).slice(2, 10);
const log = (...a) => console.log('[mega]', ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => performance.now();

async function httpJson(method, path_, body, token, timeoutMs = 30000) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${BASE}${path_}`, { method, headers, body: body ? JSON.stringify(body) : undefined, signal: ctl.signal });
    const text = await r.text();
    let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) throw new Error(`${method} ${path_} → ${r.status}: ${typeof data === 'string' ? data.slice(0, 120) : JSON.stringify(data).slice(0, 120)}`);
    return data;
  } finally { clearTimeout(to); }
}

function connectSocket(token, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const s = io(BASE, {
      auth: { token }, transports: ['websocket'],
      reconnection: false, timeout: timeoutMs, forceNew: true,
    });
    const to = setTimeout(() => { s.disconnect(); reject(new Error('sock timeout')); }, timeoutMs);
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
  });
}
function ack(s, ev, p, t = 30000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${ev} timeout`)), t);
    s.emit(ev, p, (r) => { clearTimeout(to); resolve(r); });
  });
}

function getServerRss() {
  try {
    const res = spawnSync('powershell.exe', [
      '-NoProfile', '-Command',
      "(Get-Process -Name CommClient-Server -ErrorAction SilentlyContinue | Select-Object -First 1).WorkingSet64",
    ], { encoding: 'utf8', timeout: 3000 });
    return parseInt((res.stdout || '').trim(), 10) || null;
  } catch { return null; }
}

async function runScale(N, pool) {
  if (pool.length < N) throw new Error(`pool only has ${pool.length}, need ${N}`);
  const users = pool.slice(0, N);
  const A = users[0];
  log(`N=${N} — starting`);

  // Channel
  const t1 = now();
  const channel = await httpJson('POST', '/api/channels', {
    type: 'group', name: `s-${N}-${rand()}`, member_ids: users.slice(1).map((u) => u.user_id),
  }, A.token, 120000);
  const tChannel = now() - t1;
  log(`  channel ok (${Math.round(tChannel)}ms)`);

  // Sockets
  const t2 = now();
  const sockets = [];
  let sockFails = 0;
  let firstSockErr = null;
  for (let i = 0; i < N; i += CONNECT_BATCH) {
    const slice = users.slice(i, i + CONNECT_BATCH);
    const results = await Promise.allSettled(slice.map((u) => connectSocket(u.token)));
    for (const r of results) {
      if (r.status === 'fulfilled') sockets.push(r.value);
      else { sockets.push(null); sockFails++; if (!firstSockErr) firstSockErr = String(r.reason?.message || r.reason).slice(0, 80); }
    }
    if (i % 1000 === 0 && i > 0) log(`  sock ${sockets.filter(Boolean).length}/${N} fails=${sockFails}`);
  }
  const tSockets = now() - t2;
  log(`  sockets ok=${sockets.filter(Boolean).length}/${N} fails=${sockFails} (${Math.round(tSockets)}ms)`);

  // Joins
  const t3 = now();
  let joinOk = 0, joinFails = 0, firstJoinErr = null;
  const aJoin = sockets[0] ? await ack(sockets[0], 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 30000).catch((e) => ({ error: e.message })) : { error: 'no sock' };
  if (aJoin?.error) {
    log('  A_join failed:', aJoin.error);
    firstJoinErr = aJoin.error;
  } else {
    joinOk = 1;
    for (let i = 1; i < sockets.length; i += JOIN_BATCH) {
      const slice = sockets.slice(i, i + JOIN_BATCH);
      const results = await Promise.allSettled(slice.map((s) =>
        s ? ack(s, 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 30000) : Promise.reject(new Error('no sock')),
      ));
      for (const r of results) {
        if (r.status === 'fulfilled' && !r.value?.error) joinOk++;
        else {
          joinFails++;
          const err = r.status === 'fulfilled' ? r.value?.error : r.reason?.message;
          if (!firstJoinErr) firstJoinErr = String(err).slice(0, 80);
        }
      }
      if (i % 1000 === 1 && i > 1) log(`  join ${joinOk} fails=${joinFails}`);
    }
  }
  const tJoin = now() - t3;
  log(`  joins ok=${joinOk}/${N} fails=${joinFails} (${Math.round(tJoin)}ms)`);

  const rssPeak = getServerRss();

  // Chat reach
  let chatReach = 0, chatP80 = null;
  const liveSockets = sockets.filter(Boolean);
  if (liveSockets.length > 10 && joinOk > 1) {
    const seenAt = [];
    const tChat = now();
    liveSockets.slice(1).forEach((s) => {
      const h = (p) => {
        if (p?.channel_id === channel.id) {
          seenAt.push(now() - tChat);
          for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.off(ev, h);
        }
      };
      for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.on(ev, h);
    });
    await ack(liveSockets[0], 'v2_chat_send_message', {
      channel_id: channel.id, content: `s-N${N}`, type: 'text', client_id: `s-${N}-${rand()}`,
    }, 10000).catch(() => {});
    await sleep(CHAT_WAIT_MS);
    chatReach = seenAt.length;
    if (seenAt.length) {
      seenAt.sort((a, b) => a - b);
      chatP80 = Math.round(seenAt[Math.floor(seenAt.length * 0.8)]);
    }
    log(`  chat reach=${chatReach}/${liveSockets.length - 1} p80=${chatP80}ms`);
  }

  // Cleanup
  try { if (sockets[0]) await ack(sockets[0], 'v2_call_hangup', { call_id: aJoin.call_id }, 10000); } catch {}
  for (const s of sockets) if (s) try { s.disconnect(); } catch {}
  await sleep(3000);

  return {
    N,
    channel_ms: Math.round(tChannel),
    socks_ok: sockets.filter(Boolean).length,
    sock_fails: sockFails,
    sock_err: firstSockErr,
    sock_ms: Math.round(tSockets),
    joined_ok: joinOk,
    join_fails: joinFails,
    join_err: firstJoinErr,
    join_ms: Math.round(tJoin),
    chat_reach: chatReach,
    chat_p80_ms: chatP80,
    rss_mb: rssPeak ? Math.round(rssPeak / 1024 / 1024) : null,
  };
}

async function main() {
  const pool = JSON.parse(fs.readFileSync(SEED_FILE, 'utf8'));
  log('loaded pool:', pool.length, 'users from', SEED_FILE);
  log('health:', (await httpJson('GET', '/api/health')).status);
  log('server rss start:', Math.round((getServerRss() || 0) / 1024 / 1024), 'MB');

  const results = [];
  for (const N of STEPS) {
    if (N > pool.length) { log(`skip N=${N} (pool too small)`); continue; }
    try {
      const r = await runScale(N, pool);
      results.push(r);
      log('RESULT', JSON.stringify(r));
      if (r.sock_fails > N * 0.1 || r.join_fails > N * 0.2) {
        log('degradation threshold — stopping ascent');
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
    socksOk: r.socks_ok ?? '-',
    sockFails: r.sock_fails ?? '-',
    joinedOk: r.joined_ok ?? '-',
    joinFails: r.join_fails ?? '-',
    sockMs: r.sock_ms ?? '-',
    joinMs: r.join_ms ?? '-',
    chatReach: r.chat_reach ?? '-',
    chatP80ms: r.chat_p80_ms ?? '-',
    rssMb: r.rss_mb ?? '-',
    err: (r.join_err || r.sock_err || r.crash || '').slice(0, 60),
  })));
  process.exit(0);
}

main().catch((e) => { console.error('[mega] FATAL', e.message); process.exit(1); });
