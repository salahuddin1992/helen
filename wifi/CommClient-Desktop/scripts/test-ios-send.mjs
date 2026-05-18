// Reproduce what the iOS web sim does when sending a message:
// open a socket as a user, find a DM with another user, emit
// v2_chat_send_message, see if the server accepts it.
import { io } from 'socket.io-client';

const URL  = 'http://127.0.0.1:3088';
const PASS = 'Noor1993!!$';

async function login(u) {
    const r = await fetch(URL + '/api/auth/login', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username:u, password:PASS}),
    });
    if (!r.ok) throw new Error(`${u} login http ${r.status}`);
    const d = await r.json();
    return { token: d.tokens.access_token, id: d.user.id, name: d.user.username };
}

const a = await login('yousef2');
const b = await login('yousef3');
console.log('  yousef2.id =', a.id.slice(0,8), '...');
console.log('  yousef3.id =', b.id.slice(0,8), '...');

const sa = io(URL, { auth:{token:a.token}, transports:['websocket'], reconnection:false });
const sb = io(URL, { auth:{token:b.token}, transports:['websocket'], reconnection:false });
await new Promise((res, rej) => {
    let n = 0;
    sa.on('connect', () => { if (++n === 2) res(); });
    sb.on('connect', () => { if (++n === 2) res(); });
    sa.on('connect_error', (e) => rej(e));
    sb.on('connect_error', (e) => rej(e));
    setTimeout(() => rej('timeout'), 5000);
});
console.log('  sockets connected: a=', sa.id.slice(0,8), 'b=', sb.id.slice(0,8));

// Create or fetch the DM
const r = await fetch(URL + '/api/channels', {
    method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+a.token},
    body: JSON.stringify({type:'dm', member_ids:[b.id]}),
});
if (!r.ok) throw new Error('channel create '+r.status);
const ch = await r.json();
console.log('  channel:', ch.id.slice(0,8), 'type=', ch.type);

// Listen on b for incoming message
let received = null;
for (const evt of ['v2_chat:new_message','v2_chat_new_message','new_message']) {
    sb.on(evt, (m) => { received = received || {evt, m}; });
}

// Try the EXACT shape iOS sim sends today
console.log();
console.log('  emitting v2_chat_send_message (iOS sim shape)...');
sa.emit('v2_chat_send_message', {
    channel_id: ch.id,
    content: 'Hello from iOS-sim shape ' + Date.now(),
    client_message_id: 'sim_' + Date.now(),     // wrong field name (server expects client_id)
}, (ack) => {
    console.log('  ack:', JSON.stringify(ack));
});

// Wait briefly for delivery
for (let i=0; i<20 && !received; i++) await new Promise(r=>setTimeout(r,100));
if (received) console.log('  yousef3 received via:', received.evt);
else           console.log('  yousef3 did NOT receive realtime event');

sa.close(); sb.close(); process.exit(0);
