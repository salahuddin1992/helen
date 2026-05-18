/**
 * 5k chat diagnostic — narrows down WHY chat_reach=0 at 5k.
 * Tracks:
 *   - sender socket connect/disconnect events with timestamps
 *   - chat ack result (no silent catch)
 *   - live count of connected sockets before chat send
 *   - real-time receive counter while waiting
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import fs from 'fs';
import os from 'os';
import path from 'path';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const SEED_FILE = process.env.SEED_FILE || path.join(os.tmpdir(), 'seeded_users.json');
const N = parseInt(process.env.N || '5000', 10);
const CONNECT_BATCH = parseInt(process.env.CONNECT_BATCH || '500', 10);
const JOIN_BATCH = parseInt(process.env.JOIN_BATCH || '200', 10);
const CHAT_WAIT_MS = parseInt(process.env.CHAT_WAIT_MS || '20000', 10);

const rand = () => Math.random().toString(36).slice(2, 10);
const log = (...a) => console.log('[diag]', ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => performance.now();

async function httpJson(method, p, body, token, t = 120000) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), t);
  try {
    const r = await fetch(`${BASE}${p}`, { method, headers, body: body ? JSON.stringify(body) : undefined, signal: ctl.signal });
    const text = await r.text();
    let d; try { d = text ? JSON.parse(text) : null; } catch { d = text; }
    if (!r.ok) throw new Error(`${method} ${p} → ${r.status}: ${typeof d === 'string' ? d.slice(0, 120) : JSON.stringify(d).slice(0, 120)}`);
    return d;
  } finally { clearTimeout(to); }
}

function connectSocket(token, idx, t = 30000) {
  return new Promise((resolve, reject) => {
    const s = io(BASE, {
      auth: { token }, transports: ['websocket'],
      reconnection: false, timeout: t, forceNew: true,
    });
    s.__idx = idx;
    s.__connectTime = null;
    s.__disconnectTime = null;
    s.__disconnectReason = null;
    const to = setTimeout(() => { s.disconnect(); reject(new Error('sock timeout')); }, t);
    s.on('connect', () => {
      clearTimeout(to);
      s.__connectTime = now();
      resolve(s);
    });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
    s.on('disconnect', (reason) => {
      s.__disconnectTime = now();
      s.__disconnectReason = reason;
    });
  });
}
function ack(s, ev, p, t = 30000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${ev} timeout`)), t);
    s.emit(ev, p, (r) => { clearTimeout(to); resolve(r); });
  });
}

async function main() {
  const pool = JSON.parse(fs.readFileSync(SEED_FILE, 'utf8')).slice(0, N);
  log(`health:`, (await httpJson('GET', '/api/health')).status);
  log(`N=${N}`);

  const A = pool[0];
  const ch = await httpJson('POST', '/api/channels', {
    type: 'group', name: `d-${N}-${rand()}`, member_ids: pool.slice(1).map((u) => u.user_id),
  }, A.token);
  log(`channel ${ch.id}`);

  const sockets = [];
  let sockFails = 0;
  const tS = now();
  for (let i = 0; i < N; i += CONNECT_BATCH) {
    const slice = pool.slice(i, i + CONNECT_BATCH);
    const r = await Promise.allSettled(slice.map((u, k) => connectSocket(u.token, i + k)));
    for (const x of r) {
      if (x.status === 'fulfilled') sockets.push(x.value);
      else { sockets.push(null); sockFails++; }
    }
  }
  log(`sockets ok=${sockets.filter(Boolean).length}/${N} fails=${sockFails} (${Math.round(now() - tS)}ms)`);

  // Track sender disconnect explicitly
  const sender = sockets[0];
  sender.__role = 'sender';
  const senderLog = [];
  sender.io.on('reconnect_attempt', () => senderLog.push([Math.round(now()), 'reconnect_attempt']));
  sender.io.on('error', (e) => senderLog.push([Math.round(now()), 'engine_error', String(e?.message || e)]));
  sender.on('disconnect', (reason) => senderLog.push([Math.round(now()), 'disconnect', reason]));
  sender.on('connect_error', (e) => senderLog.push([Math.round(now()), 'connect_error', String(e?.message || e)]));

  // Joins
  const tJ = now();
  let joinOk = 0, joinFails = 0;
  const aJoin = await ack(sender, 'v2_call_join_group', { channel_id: ch.id, media_type: 'video' }, 30000).catch((e) => ({ error: e.message }));
  if (aJoin?.error) {
    log('A_join FAIL', aJoin.error);
    process.exit(1);
  }
  joinOk = 1;
  for (let i = 1; i < sockets.length; i += JOIN_BATCH) {
    const slice = sockets.slice(i, i + JOIN_BATCH);
    const r = await Promise.allSettled(slice.map((s) =>
      s ? ack(s, 'v2_call_join_group', { channel_id: ch.id, media_type: 'video' }, 30000) : Promise.reject(new Error('no sock'))));
    for (const x of r) {
      if (x.status === 'fulfilled' && !x.value?.error) joinOk++; else joinFails++;
    }
  }
  log(`joins ok=${joinOk}/${N} fails=${joinFails} (${Math.round(now() - tJ)}ms)`);

  // Pre-chat diagnostic snapshot
  const connectedNow = sockets.filter((s) => s && s.connected).length;
  const disconnectedNow = sockets.filter((s) => s && !s.connected).length;
  log(`PRE-CHAT: connected=${connectedNow}/${N} disconnected_during_run=${disconnectedNow}`);
  log(`PRE-CHAT sender: connected=${sender.connected} events=${senderLog.length}`);
  if (senderLog.length) log('sender events:', senderLog.slice(-5));

  // Find the first disconnect time among receivers (if any)
  let firstDiscT = null;
  for (const s of sockets.slice(1, 10)) {
    if (s && !s.connected && s.__disconnectTime) {
      if (firstDiscT === null || s.__disconnectTime < firstDiscT) firstDiscT = s.__disconnectTime;
    }
  }
  log(`earliest receiver disc: ${firstDiscT ? Math.round(firstDiscT) + 'ms' : 'none in sample'}`);

  // Set up chat receivers
  const seenAt = [];
  const receiverSockets = sockets.slice(1).filter(Boolean);
  let receiverBindCount = 0;
  receiverSockets.forEach((s) => {
    const h = (p) => {
      if (p?.channel_id === ch.id) {
        seenAt.push(now());
        s.off('v2_chat:new_message', h);
      }
    };
    s.on('v2_chat:new_message', h);
    receiverBindCount++;
  });
  log(`receivers bound=${receiverBindCount}`);

  // Send chat — capture ack result explicitly (no silent catch)
  const tC = now();
  let chatAckResult = null, chatAckErr = null;
  try {
    chatAckResult = await ack(sender, 'v2_chat_send_message', {
      channel_id: ch.id, content: `diag-${N}`, type: 'text', client_id: `d-${rand()}`,
    }, 15000);
    log(`chat ack (${Math.round(now() - tC)}ms):`, JSON.stringify(chatAckResult).slice(0, 200));
  } catch (e) {
    chatAckErr = e.message;
    log(`chat ack FAIL (${Math.round(now() - tC)}ms):`, e.message);
  }

  // Poll reach live
  for (let i = 0; i < CHAT_WAIT_MS; i += 2000) {
    await sleep(2000);
    log(`  t+${i + 2000}ms reach=${seenAt.length}/${receiverBindCount}`);
  }

  const finalConnected = sockets.filter((s) => s && s.connected).length;
  log(`FINAL: reach=${seenAt.length}/${receiverBindCount} connected=${finalConnected}/${N}`);
  log(`sender final: connected=${sender.connected} log=${JSON.stringify(senderLog)}`);

  // Clean up
  for (const s of sockets) if (s) try { s.disconnect(); } catch {}
  process.exit(0);
}
main().catch((e) => { console.error('[diag] FATAL', e); process.exit(1); });
