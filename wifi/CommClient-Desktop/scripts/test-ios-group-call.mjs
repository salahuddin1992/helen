// Verify a group call rings all members of the channel.
//
// Setup: yousef2 starts a video group call in some group channel that
// includes yousef3. yousef3 should receive `call_incoming` with
// channel_id + media_type + caller_username + caller_share_code.
import { io } from 'socket.io-client';

const URL  = 'http://127.0.0.1:3088';
const PASS = 'Noor1993!!$';

async function login(u) {
    const r = await fetch(URL + '/api/auth/login', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username:u, password:PASS}),
    });
    return await r.json();
}

const a = await login('yousef2');
const b = await login('yousef3');

// Find an existing GROUP channel that contains both users.
const r = await fetch(URL + '/api/channels',
    { headers: { Authorization: 'Bearer ' + a.tokens.access_token } });
const cd = await r.json();
const channels = cd.channels || cd;
let group = channels.find((c) =>
    c.type === 'group' &&
    (c.members || []).some((m) => m.user_id === b.user_id || m.user_id === b.id || (b.user && m.user_id === b.user.id))
);
if (!group) {
    // Create one.
    const cr = await fetch(URL + '/api/channels', {
        method:'POST',
        headers:{'Content-Type':'application/json',
                 'Authorization':'Bearer '+a.tokens.access_token},
        body: JSON.stringify({type:'group', name:'test-group',
                              member_ids:[b.user.id]}),
    });
    if (!cr.ok) { console.error('create group:', cr.status); process.exit(2); }
    group = await cr.json();
}
console.log('  group channel:', group.id.slice(0,8), 'name=', group.name);
console.log('  members:', (group.members||[]).map(m=>m.username).join(', '));

const sa = io(URL, { auth:{token:a.tokens.access_token}, transports:['websocket'], reconnection:false });
const sb = io(URL, { auth:{token:b.tokens.access_token}, transports:['websocket'], reconnection:false });
await new Promise((res, rej) => {
    let n = 0;
    sa.on('connect', () => { if (++n === 2) res(); });
    sb.on('connect', () => { if (++n === 2) res(); });
    sa.on('connect_error', rej); sb.on('connect_error', rej);
    setTimeout(() => rej(new Error('socket timeout')), 5000);
});

let incoming = null;
sb.on('call_incoming', (m) => { incoming = incoming || m; });

console.log();
console.log('  yousef2 → v2_call_join_group video');
sa.emit('v2_call_join_group',
    { channel_id: group.id, media_type: 'video' },
    (resp) => console.log('  ack:', JSON.stringify(resp)));

for (let i = 0; i < 50 && !incoming; i++) await new Promise(r => setTimeout(r, 100));

if (!incoming) {
    console.log('  ✗ FAIL — yousef3 never received call_incoming for group');
    sa.close(); sb.close();
    process.exit(1);
}
console.log();
console.log('  ✓ yousef3 received call_incoming:');
console.log('    channel_id        =', incoming.channel_id?.slice(0,8));
console.log('    caller_id         =', incoming.caller_id?.slice(0,8));
console.log('    caller_name       =', incoming.caller_name);
console.log('    caller_username   =', incoming.caller_username);
console.log('    caller_share_code =',
    incoming.caller_share_code
        ? incoming.caller_share_code.slice(0,8) + '…' + incoming.caller_share_code.slice(-4)
        : '(missing)');
console.log('    media_type        =', incoming.media_type);

const ok =
    incoming.channel_id === group.id &&
    incoming.caller_username === 'yousef2' &&
    incoming.caller_share_code &&
    incoming.caller_share_code.length === 64;
console.log();
console.log(ok ? '  ✓ PASS' : '  ✗ FAIL — fields incomplete');

sa.close(); sb.close();
process.exit(ok ? 0 : 1);
