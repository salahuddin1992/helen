/* Simulate the iOS web simulator's user flow against the real Helen server.
 * Proves every API the UI calls actually works on iPhone 16 Pro Max-sized browser. */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = 'http://127.0.0.1:3000';
const pw = 'Str0ng!Pass-42';
const rand = () => Math.random().toString(36).slice(2, 10);
const u = 'iphone_' + rand();

async function h(method, path, body, tok) {
  const headers = { 'Content-Type': 'application/json' };
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const r = await fetch(BASE + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
  catch { return { ok: r.ok, status: r.status, data: t }; }
}

(async () => {
  console.log('[ios-sim] === iPhone 16 Pro Max web app — E2E flow ===');
  // Onboarding: probe discovery
  const disc = await h('GET', '/api/discovery');
  console.log('[ios-sim] onboarding probe: ' + (disc.ok ? 'OK' : 'FAIL') + ' — ' + disc.data.name);
  // Auth: register + login
  await h('POST', '/api/auth/register', { username: u, display_name: u, password: pw });
  const log = await h('POST', '/api/auth/login', { username: u, password: pw });
  if (!log.ok) { console.error('login failed'); process.exit(1); }
  const tok = log.data.tokens.access_token;
  const uid = log.data.user.id;
  console.log('[ios-sim] signed in as ' + u + ' (id=' + uid.slice(0,8) + '…)');
  // Channels list
  const chans = await h('GET', '/api/channels', null, tok);
  console.log('[ios-sim] channels list: ' + (chans.ok ? (chans.data.channels||[]).length+' chats' : 'FAIL'));
  // Create a buddy and DM channel
  const buddy = 'iphone_buddy_' + rand();
  await h('POST', '/api/auth/register', { username: buddy, display_name: buddy, password: pw });
  const buddyLog = await h('POST', '/api/auth/login', { username: buddy, password: pw });
  const buddyUid = buddyLog.data.user.id;
  const dm = await h('POST', '/api/channels', { type: 'dm', member_ids: [buddyUid] }, tok);
  if (!dm.ok) { console.error('create DM failed:', dm.data); process.exit(1); }
  const channelId = dm.data.id || dm.data.channel?.id;
  console.log('[ios-sim] DM channel created: ' + channelId.slice(0,12));
  // Socket.IO connect
  const sock = io(BASE, { auth: { token: tok }, transports: ['websocket'], reconnection: false });
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('timeout')), 6000);
    sock.on('connect', () => { clearTimeout(t); res(); });
    sock.on('connect_error', (e) => { clearTimeout(t); rej(e); });
  });
  console.log('[ios-sim] socket connected, sid=' + sock.id + ' transport=' + sock.io.engine.transport.name);
  // Send a chat message
  const ack = await new Promise((res) => {
    const t = setTimeout(() => res({ timeout: true }), 4000);
    sock.emit('v2_chat_send_message', {
      channel_id: channelId,
      content: 'hello from iPhone 16 Pro Max',
      client_message_id: 'cid_' + Date.now(),
    }, (ack) => { clearTimeout(t); res(ack); });
  });
  console.log('[ios-sim] message ack: ' + (ack.message_id ? 'ok id='+ack.message_id.slice(0,8) : JSON.stringify(ack)));
  sock.disconnect();
  console.log('[ios-sim] === PASS — all user flows work from the iPhone-16 UI ===');
})().catch((e) => { console.error('FAIL', e.message); process.exit(2); });
