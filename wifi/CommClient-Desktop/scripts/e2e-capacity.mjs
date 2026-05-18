/**
 * Capacity stress test for a single group call.
 *
 * Pushes participants upward: 4, 8, 16, 32, 48, 64 (server ceiling), 65 (reject).
 * For each size N, measures:
 *   - time to register + login N users
 *   - time to connect N sockets
 *   - time to have all N join the same group call
 *   - concurrent chat RTT during the call (sender → receivers)
 *   - concurrent file upload (resumable) during the call
 *   - signaling fan-out completeness
 *
 * Also confirms the server rejects the (MAX_CALL_PARTICIPANTS + 1)-th user.
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';
import crypto from 'crypto';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const pw = 'Str0ng!Pass-42';
const STEPS = (process.env.STEPS || '4,8,16,32,48,64,65').split(',').map(Number);
const rand = () => Math.random().toString(36).slice(2, 10);
const log = (...a) => console.log('[cap]', ...a);
const die = (m, e) => { console.error('[cap] FAIL', m); if (e) console.error(e); process.exit(1); };

async function httpJson(method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${BASE}${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status}: ${typeof data === 'string' ? data : JSON.stringify(data)}`);
  return data;
}

function connectSocket(token) {
  return new Promise((resolve, reject) => {
    const s = io(BASE, { auth: { token }, transports: ['websocket'], reconnection: false, timeout: 15000 });
    const to = setTimeout(() => { s.disconnect(); reject(new Error('socket timeout')); }, 15000);
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
  });
}
function ack(s, ev, p, t = 10000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${ev} timeout`)), t);
    s.emit(ev, p, (r) => { clearTimeout(to); resolve(r); });
  });
}
const now = () => performance.now();

async function runSize(N) {
  log(`─── N=${N} ───`);
  const t0 = now();

  const unames = Array.from({ length: N }, (_, i) => `cap${N}_${i}_${rand()}`);
  const users = [];
  // Register + login in parallel batches of 10 so we don't slam the auth limiter.
  for (let i = 0; i < N; i += 10) {
    const batch = unames.slice(i, i + 10);
    const results = await Promise.all(batch.map(async (u) => {
      await httpJson('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
      const r = await httpJson('POST', '/api/auth/login', { username: u, password: pw });
      return { uname: u, userId: r.user.id, token: r.tokens?.access_token || r.access_token };
    }));
    users.push(...results);
  }
  const tAuth = now() - t0;

  const A = users[0];
  const t1 = now();
  const channel = await httpJson('POST', '/api/channels', {
    type: 'group', name: `cap-${N}-${rand()}`, member_ids: users.slice(1).map((u) => u.userId),
  }, A.token);
  const tChannel = now() - t1;

  const t2 = now();
  const sockets = await Promise.all(users.map((u) => connectSocket(u.token)));
  const tSockets = now() - t2;

  const t3 = now();
  const aJoin = await ack(sockets[0], 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 10000);
  if (aJoin?.error) return { N, failed: true, error: aJoin.error, tAuth, tChannel, tSockets };
  const callId = aJoin.call_id;

  // Join the rest in parallel batches of 8 (avoid O(n²) simultaneous broadcasts overwhelming socket buffers).
  const joinResults = [];
  let rejected = null;
  for (let i = 1; i < N; i += 8) {
    const slice = users.slice(i, Math.min(i + 8, N));
    const results = await Promise.all(slice.map((u, k) =>
      ack(sockets[i + k], 'v2_call_join_group', { channel_id: channel.id, media_type: 'video' }, 10000)
        .catch((e) => ({ error: e.message })),
    ));
    joinResults.push(...results);
    // First rejection is the cap
    for (const r of results) if (r?.error && !rejected) rejected = r.error;
  }
  const tJoin = now() - t3;

  const last = joinResults[joinResults.length - 1] || aJoin;
  const reportedCount = last?.participants?.length ?? 1;
  const joinFails = joinResults.filter((r) => r?.error).length;

  // Concurrent chat RTT — A sends a message, time until at least 80% of peers see it.
  const chatRtts = [];
  if (joinFails === 0) {
    const targetSeen = Math.ceil((N - 1) * 0.8);
    const tChat = now();
    let seen = 0;
    const chatSeenPromise = new Promise((resolve) => {
      sockets.slice(1).forEach((s) => {
        const h = (p) => {
          if (p?.channel_id === channel.id) {
            seen++;
            chatRtts.push(now() - tChat);
            for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.off(ev, h);
            if (seen >= targetSeen) resolve();
          }
        };
        for (const ev of ['v2_chat:new_message', 'v2_chat_new_message', 'chat:new_message']) s.on(ev, h);
      });
      setTimeout(() => resolve(), 10000);
    });
    await ack(sockets[0], 'v2_chat_send_message', {
      channel_id: channel.id, content: `cap-test-N${N}`, type: 'text', client_id: `cap-${N}-${rand()}`,
    }, 5000).catch(() => {});
    await chatSeenPromise;
  }

  // Concurrent file upload — A uploads a 256KB file via resumable; measure init→complete time.
  let tUpload = null;
  let uploadOk = false;
  if (joinFails === 0) {
    try {
      const size = 256 * 1024;
      const bytes = crypto.randomBytes(size);
      const sha = crypto.createHash('sha256').update(bytes).digest('hex');
      const tU = now();
      const init = await httpJson('POST', '/api/files/resumable/init', {
        filename: `cap-${N}.bin`, total_size: size, mime_type: 'application/octet-stream',
        chunk_size: 128 * 1024, expected_sha256: sha, channel_id: channel.id,
      }, A.token);
      const sid = init.session_id || init.id;
      const chunkSize = init.chunk_size || 128 * 1024;
      for (let off = 0; off < size; off += chunkSize) {
        const end = Math.min(off + chunkSize, size);
        const chunk = bytes.subarray(off, end);
        const idx = Math.floor(off / chunkSize);
        const r = await fetch(`${BASE}/api/files/resumable/${sid}/chunk/${idx}`, {
          method: 'PUT',
          headers: {
            'Authorization': `Bearer ${A.token}`,
            'Content-Type': 'application/octet-stream',
            'Content-Range': `bytes ${off}-${end - 1}/${size}`,
          },
          body: chunk,
        });
        if (!r.ok) throw new Error(`chunk ${idx} → ${r.status}: ${await r.text()}`);
      }
      await httpJson('POST', `/api/files/resumable/${sid}/complete`, { expected_sha256: sha }, A.token);
      tUpload = now() - tU;
      uploadOk = true;
    } catch (e) {
      tUpload = `error: ${e.message}`;
    }
  }

  // Cleanup: hang up + disconnect all sockets.
  try { await ack(sockets[0], 'v2_call_hangup', { call_id: callId }, 5000); } catch {}
  sockets.forEach((s) => s.disconnect());

  const report = {
    N,
    reportedParticipants: reportedCount,
    rejectedAt: rejected ? `"${rejected}"` : null,
    joinFailures: joinFails,
    tAuth_ms: Math.round(tAuth),
    tChannel_ms: Math.round(tChannel),
    tSockets_ms: Math.round(tSockets),
    tJoin_ms: Math.round(tJoin),
    chat_broadcast_p80_ms: chatRtts.length ? Math.round(chatRtts[Math.floor(chatRtts.length * 0.8)]) : null,
    chat_broadcast_min_ms: chatRtts.length ? Math.round(Math.min(...chatRtts)) : null,
    chat_broadcast_max_ms: chatRtts.length ? Math.round(Math.max(...chatRtts)) : null,
    chat_seen: chatRtts.length,
    upload_256kb_ms: typeof tUpload === 'number' ? Math.round(tUpload) : tUpload,
    upload_ok: uploadOk,
  };
  return report;
}

async function main() {
  log('BASE', BASE, 'STEPS', STEPS);
  const health = await httpJson('GET', '/api/health');
  log('health', health.status);

  const results = [];
  for (const N of STEPS) {
    try {
      const r = await runSize(N);
      results.push(r);
      log(JSON.stringify(r));
    } catch (e) {
      log(`N=${N} CRASH: ${e.message}`);
      results.push({ N, crash: e.message });
    }
    // Small pause between rounds so auth limiter / socket pool settles.
    await new Promise((r) => setTimeout(r, 1500));
  }

  console.log('\n=== SUMMARY ===');
  console.table(results.map((r) => ({
    N: r.N,
    reported: r.reportedParticipants ?? '-',
    rejected: r.rejectedAt ?? '-',
    joinFails: r.joinFailures ?? '-',
    authMs: r.tAuth_ms,
    joinMs: r.tJoin_ms,
    chatP80Ms: r.chat_broadcast_p80_ms,
    chatMaxMs: r.chat_broadcast_max_ms,
    chatSeen: r.chat_seen,
    uploadMs: r.upload_256kb_ms,
  })));
  process.exit(0);
}

main().catch((e) => die(e.message, e));
