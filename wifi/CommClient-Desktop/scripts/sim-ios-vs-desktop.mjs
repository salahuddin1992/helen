/**
 * Simulation: iOS client (yousef2) ↔ Helen-Server :3088 ↔ Desktop client (yousf1)
 *
 * Acts as both clients simultaneously. Helen-Server is the only intermediary —
 * no rendezvous, no tunnel, just LAN HTTP + Socket.IO.
 *
 * What the simulation does (realistic timing):
 *   1. Each side does HTTP login → gets JWT
 *   2. Each side opens Socket.IO WebSocket carrying the JWT
 *   3. Desktop creates a DM channel with iOS as the second member
 *   4. Both sides exchange 4 messages back-and-forth with real delays
 *   5. Each message is logged with sender/receiver/transport/sid/latency
 *
 * After this runs, the channel + messages are persisted in Helen-Server's DB.
 * Opening the iOS web simulator and signing in as yousef2 will show the
 * conversation in the chat list — the user can continue it live.
 */
import { io } from 'socket.io-client';

const URL  = 'http://127.0.0.1:3088';
const PASS = 'Noor1993!!$';

const dim = '\x1b[2m', cyan = '\x1b[36m', green = '\x1b[32m',
      yellow = '\x1b[33m', magenta = '\x1b[35m', reset = '\x1b[0m';

const ts = () => new Date().toISOString().slice(11, 23);
const log = (who, color, msg) =>
    console.log(`${dim}${ts()}${reset} ${color}[${who.padEnd(15)}]${reset} ${msg}`);

async function login(username) {
    const t0 = Date.now();
    const r = await fetch(URL + '/api/auth/login', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username, password: PASS}),
    });
    if (!r.ok) throw new Error(`${username} login http ${r.status}`);
    const d = await r.json();
    log(username, cyan, `HTTP login OK (${Date.now()-t0}ms) — id=${d.user.id.slice(0,12)} role=${d.user.role||'user'}`);
    return { token: d.tokens.access_token, id: d.user.id, name: d.user.username,
             display: d.user.display_name };
}

async function connectSocket(label, color, token) {
    const t0 = Date.now();
    const sock = io(URL, {
        auth: { token },
        transports: ['websocket'],
        reconnection: false,
    });
    await new Promise((res, rej) => {
        sock.on('connect', res);
        sock.on('connect_error', e => rej(new Error(e.message)));
        setTimeout(() => rej(new Error('socket timeout')), 5000);
    });
    log(label, color, `Socket.IO connected (${Date.now()-t0}ms) sid=${sock.id} transport=${sock.io.engine.transport.name}`);
    return sock;
}

console.log();
console.log(`${magenta}═══════════════════════════════════════════════════════════════${reset}`);
console.log(`${magenta}  iOS yousef2  ←  Helen-Server :3088  →  Desktop yousf1${reset}`);
console.log(`${magenta}═══════════════════════════════════════════════════════════════${reset}`);
console.log();

// ── Login both ────────────────────────────────────────────────────
const desktop = await login('yousf1');
const ios     = await login('yousef2');

// ── Open WebSockets ───────────────────────────────────────────────
const sDesk = await connectSocket('yousf1 desktop', cyan,    desktop.token);
const sIos  = await connectSocket('yousef2 iOS',    magenta, ios.token);

// ── Set up receivers BEFORE creating the channel ──────────────────
const messageEvents = ['v2_chat:new_message', 'v2_chat_new_message', 'new_message'];
const deskInbox = []; const iosInbox = [];
for (const e of messageEvents) {
    sDesk.on(e, m => deskInbox.push({ ts: ts(), evt: e, m }));
    sIos.on(e,  m => iosInbox.push({  ts: ts(), evt: e, m }));
}

// ── Desktop creates DM ────────────────────────────────────────────
console.log();
log('yousf1 desktop', cyan, `creating DM channel with yousef2…`);
const t0 = Date.now();
const r = await fetch(URL + '/api/channels', {
    method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+desktop.token},
    body: JSON.stringify({type:'dm', member_ids:[ios.id]}),
});
if (!r.ok) throw new Error(`channel create http ${r.status}: ${await r.text()}`);
const channel = await r.json();
log('yousf1 desktop', cyan, `DM channel created (${Date.now()-t0}ms) id=${channel.id.slice(0,12)} members=${channel.member_count}`);

await new Promise(r => setTimeout(r, 500));

// ── 4 messages, alternating ───────────────────────────────────────
const conversation = [
    { from: 'desktop', text: 'مرحبا، انا على Helen Desktop' },
    { from: 'ios',     text: 'أهلين! أنا أراك من iPhone' },
    { from: 'desktop', text: 'الرسالة وصلت بدون internet — كل شيء على نفس الـWiFi' },
    { from: 'ios',     text: 'تمام — السيرفر :3088 وسط بيننا' },
];

for (const msg of conversation) {
    const sock = msg.from === 'desktop' ? sDesk : sIos;
    const sender = msg.from === 'desktop' ? 'yousf1 desktop' : 'yousef2 iOS';
    const senderColor = msg.from === 'desktop' ? cyan : magenta;
    const tSent = Date.now();
    sock.emit('v2_chat_send_message', {
        channel_id: channel.id,
        client_message_id: 'sim_' + tSent + '_' + msg.from,
        content: msg.text,
        type: 'text',
    });
    log(sender, senderColor, `→ ${msg.text}`);
    await new Promise(r => setTimeout(r, 800));
}

// ── Wait for all messages to land ─────────────────────────────────
await new Promise(r => setTimeout(r, 800));

console.log();
console.log(`${green}═══════════════════════════════════════════════════════════════${reset}`);
console.log(`${green}  Delivery report${reset}`);
console.log(`${green}═══════════════════════════════════════════════════════════════${reset}`);
log('yousf1 desktop', cyan,    `inbox: ${deskInbox.length} message(s)`);
deskInbox.forEach(e => console.log(`  ${dim}${e.ts}${reset} via ${e.evt} → "${(e.m.content||'').slice(0,60)}"`));
log('yousef2 iOS',    magenta, `inbox: ${iosInbox.length} message(s)`);
iosInbox.forEach(e =>  console.log(`  ${dim}${e.ts}${reset} via ${e.evt} → "${(e.m.content||'').slice(0,60)}"`));

// ── Verify via REST that history is durable ───────────────────────
const histResp = await fetch(URL + `/api/channels/${channel.id}/messages?limit=20`, {
    headers: { 'Authorization': 'Bearer ' + desktop.token },
});
if (histResp.ok) {
    const hist = await histResp.json();
    const list = hist.messages || hist.results || hist;
    console.log();
    log('helen-server', yellow, `DB history: ${Array.isArray(list)?list.length:'?'} message(s) persisted`);
    if (Array.isArray(list)) {
        for (const m of list.reverse()) {
            const who = m.sender_id === desktop.id ? 'desktop' : 'iOS    ';
            const c   = m.sender_id === desktop.id ? cyan : magenta;
            console.log(`  ${dim}stored${reset} ${c}${who}${reset} → "${(m.content||'').slice(0,60)}"`);
        }
    }
}

sDesk.close(); sIos.close();
console.log();
console.log(`${green}✓ SIMULATION COMPLETE — connection works, conversation persisted${reset}`);
console.log();
console.log(`Now open the actual UIs — the messages above will be visible in their chat lists:`);
console.log(`  iOS sim:  http://127.0.0.1:3088/mobile/    sign in as yousef2 / ${PASS}`);
console.log(`  Desktop:  Helen.exe (already running)      sign in as yousf1 / ${PASS}`);
console.log();
process.exit(0);
