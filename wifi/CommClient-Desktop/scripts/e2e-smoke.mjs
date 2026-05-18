/**
 * End-to-end smoke test against a running CommClient server.
 *   1. Register two users (A, B)
 *   2. Login A (grab access token)
 *   3. Create a DM channel between A and B via REST
 *   4. Connect socket.io as A with the JWT
 *   5. Emit v2_chat_send_message and wait for ACK
 *   6. Login B, connect socket.io, verify B receives the message
 * Exits 0 on full success, non-zero otherwise.
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const WS = process.env.COMMCLIENT_WS || BASE;

const tag = (t) => `[smoke] ${t}`;
const log = (...a) => console.log(tag(a.shift()), ...a);
const die = (msg, err) => {
  console.error(tag('FAIL'), msg);
  if (err) console.error(err);
  process.exit(1);
};

const rand = () => Math.random().toString(36).slice(2, 10);
const unameA = `smoke_a_${rand()}`;
const unameB = `smoke_b_${rand()}`;
const pw = 'Str0ng!Pass-42';

async function httpJson(method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    throw new Error(`${method} ${path} → ${r.status}: ${typeof data === 'string' ? data : JSON.stringify(data)}`);
  }
  return data;
}

function connectSocket(token, label) {
  return new Promise((resolve, reject) => {
    const s = io(WS, {
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: false,
      timeout: 8000,
    });
    const to = setTimeout(() => {
      s.disconnect();
      reject(new Error(`${label}: socket connect timeout`));
    }, 8000);
    s.on('connect', () => {
      clearTimeout(to);
      log(`socket-${label}`, 'connected', s.id);
      resolve(s);
    });
    s.on('connect_error', (err) => {
      clearTimeout(to);
      reject(new Error(`${label}: connect_error ${err.message}`));
    });
  });
}

function emitWithAck(socket, event, payload, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack timeout for ${event}`)), timeoutMs);
    socket.emit(event, payload, (resp) => {
      clearTimeout(to);
      resolve(resp);
    });
  });
}

async function main() {
  log('start', BASE);

  // Health
  const h = await httpJson('GET', '/api/health');
  log('health', h);

  // Register A & B
  log('register', unameA);
  await httpJson('POST', '/api/auth/register', {
    username: unameA, display_name: 'Smoke A', password: pw,
  });
  log('register', unameB);
  await httpJson('POST', '/api/auth/register', {
    username: unameB, display_name: 'Smoke B', password: pw,
  });

  // Login
  const loginA = await httpJson('POST', '/api/auth/login', { username: unameA, password: pw });
  const loginB = await httpJson('POST', '/api/auth/login', { username: unameB, password: pw });
  const tokenA = loginA.tokens?.access_token || loginA.access_token;
  const tokenB = loginB.tokens?.access_token || loginB.access_token;
  const userA = loginA.user?.id;
  const userB = loginB.user?.id;
  if (!tokenA || !tokenB) die('missing access token', { loginA, loginB });
  log('login', { userA, userB });

  // Create DM channel A↔B
  const ch = await httpJson('POST', '/api/channels', {
    type: 'dm', name: 'smoke-dm', member_ids: [userB],
  }, tokenA);
  const channelId = ch.id;
  log('channel', channelId, ch.type);

  // Connect sockets
  const [sockA, sockB] = await Promise.all([
    connectSocket(tokenA, 'A'),
    connectSocket(tokenB, 'B'),
  ]);

  // B subscribes to the channel room so it receives live messages
  try {
    await emitWithAck(sockB, 'v2_chat_subscribe_channel', { channel_id: channelId }, 3000);
  } catch (e) {
    log('subscribe-note', 'v2_chat_subscribe_channel no ack:', e.message);
  }

  // B listens for the new message
  const received = new Promise((resolve) => {
    const handler = (payload) => {
      if (payload?.channel_id === channelId) {
        for (const evt of ['v2_chat_new_message', 'v2_chat:new_message', 'new_message']) {
          sockB.off(evt, handler);
        }
        resolve(payload);
      }
    };
    sockB.on('v2_chat_new_message', handler);
    sockB.on('v2_chat:new_message', handler);
    sockB.on('new_message', handler);
  });

  const clientId = `cid_${Date.now()}_${rand()}`;
  const ack = await emitWithAck(sockA, 'v2_chat_send_message', {
    channel_id: channelId,
    content: 'hello from smoke test',
    type: 'text',
    client_id: clientId,
  }, 8000);
  log('send-ack', ack);
  if (ack?.error) die(`send rejected: ${ack.error}`);
  const serverMsgId = ack?.message_id || ack?.id;

  // Wait up to 6s for B to receive it
  const race = await Promise.race([
    received,
    new Promise((_, rej) => setTimeout(() => rej(new Error('B did not receive message in 6s')), 6000)),
  ]);
  log('received-by-B', { id: race.id || race.message_id, content: race.content });

  sockA.disconnect();
  sockB.disconnect();

  log('PASS', { channelId, serverMsgId, clientId });
  process.exit(0);
}

main().catch((e) => die(e.message, e));
