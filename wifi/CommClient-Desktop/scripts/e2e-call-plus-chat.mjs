/**
 * Verify chat messages flow while an active group call is in progress.
 * 4 users: A, B, C, D. A+B+C are in a call; A sends chat; D (not in call)
 * and B/C (in call) must all receive the v2_chat_new_message event.
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const pw = 'Str0ng!Pass-42';
const log = (...a) => console.log('[mix]', ...a);
const die = (m, e) => { console.error('[mix] FAIL', m); if (e) console.error(e); process.exit(1); };
const rand = () => Math.random().toString(36).slice(2, 10);

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
    const s = io(BASE, { auth: { token }, transports: ['websocket', 'polling'], reconnection: false, timeout: 8000 });
    const to = setTimeout(() => { s.disconnect(); reject(new Error('socket timeout')); }, 8000);
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (e) => { clearTimeout(to); reject(e); });
  });
}
function ack(s, ev, p, t = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack ${ev}`)), t);
    s.emit(ev, p, (r) => { clearTimeout(to); resolve(r); });
  });
}

async function main() {
  const names = Array.from({ length: 4 }, (_, i) => `mix_${String.fromCharCode(97 + i)}_${rand()}`);
  const users = [];
  for (const n of names) {
    await httpJson('POST', '/api/auth/register', { username: n, display_name: n, password: pw });
    const r = await httpJson('POST', '/api/auth/login', { username: n, password: pw });
    users.push({ uname: n, userId: r.user.id, token: r.tokens?.access_token || r.access_token });
  }
  const [A, B, C, D] = users;
  log('users:', users.map((u) => u.uname).join(', '));

  const ch = await httpJson('POST', '/api/channels', {
    type: 'group', name: `mix-${rand()}`, member_ids: [B.userId, C.userId, D.userId],
  }, A.token);
  log('channel', ch.id);

  const sockets = await Promise.all(users.map((u) => connectSocket(u.token)));
  log('sockets up');

  // A/B/C join the call; D stays out
  const aJoin = await ack(sockets[0], 'v2_call_join_group', { channel_id: ch.id, media_type: 'audio' });
  await ack(sockets[1], 'v2_call_join_group', { channel_id: ch.id, media_type: 'audio' });
  await ack(sockets[2], 'v2_call_join_group', { channel_id: ch.id, media_type: 'audio' });
  log('call active, call_id:', aJoin.call_id);

  // All four should receive the chat message — B/C are in call, D is in channel but not in call.
  const received = users.map(() => null);
  const seenPromises = users.map((_, i) => new Promise((resolve) => {
    const handler = (p) => {
      if (p?.channel_id === ch.id) {
        received[i] = p;
        for (const ev of ['v2_chat_new_message', 'v2_chat:new_message', 'new_message']) sockets[i].off(ev, handler);
        resolve(p);
      }
    };
    for (const ev of ['v2_chat_new_message', 'v2_chat:new_message', 'new_message']) sockets[i].on(ev, handler);
    setTimeout(() => resolve(null), 6000);
  }));

  const clientId = `mix_${Date.now()}_${rand()}`;
  const sendAck = await ack(sockets[0], 'v2_chat_send_message', {
    channel_id: ch.id, content: 'chat-while-calling', type: 'text', client_id: clientId,
  }, 6000);
  if (sendAck?.error) die(`send rejected: ${sendAck.error}`);
  log('send-ack', sendAck.message_id);

  // A is the sender — the server typically excludes the sender from the broadcast;
  // we only require B, C, D to receive.
  const results = await Promise.all(seenPromises);
  const recipients = ['A', 'B', 'C', 'D'];
  for (let i = 1; i < 4; i++) {
    if (!results[i]) die(`${recipients[i]} (${i === 3 ? 'not-in-call' : 'in-call'}) did not receive chat during active call`);
  }
  log('chat during call: B/C (in-call) and D (not-in-call) all received message');

  sockets.forEach((s) => s.disconnect());
  log('PASS');
  process.exit(0);
}

main().catch((e) => die(e.message, e));
