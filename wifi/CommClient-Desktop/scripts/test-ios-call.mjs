// Verify the server now embeds caller_username + caller_share_code in
// the `call_incoming` event so the callee UI can show both.
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
console.log('  caller   yousef2', a.id.slice(0,8));
console.log('  callee   yousef3', b.id.slice(0,8));

const sa = io(URL, { auth:{token:a.token}, transports:['websocket'], reconnection:false });
const sb = io(URL, { auth:{token:b.token}, transports:['websocket'], reconnection:false });
await new Promise((res, rej) => {
    let n = 0;
    sa.on('connect', () => { if (++n === 2) res(); });
    sb.on('connect', () => { if (++n === 2) res(); });
    sa.on('connect_error', rej); sb.on('connect_error', rej);
    setTimeout(() => rej(new Error('socket timeout')), 5000);
});

let incoming = null;
sb.on('call_incoming', (m) => { incoming = m; });

console.log();
console.log('  yousef2 → v2_call_initiate target=yousef3');
sa.emit('v2_call_initiate',
    { target_id: b.id, media_type: 'audio' },
    (resp) => console.log('  ack:', JSON.stringify(resp)));

for (let i = 0; i < 30 && !incoming; i++) await new Promise(r => setTimeout(r, 100));

if (!incoming) {
    console.log('  ✗ FAIL — yousef3 never received call_incoming');
    process.exit(1);
}

console.log();
console.log('  ✓ yousef3 received call_incoming:');
console.log('     caller_id          =', incoming.caller_id);
console.log('     caller_name        =', incoming.caller_name);
console.log('     caller_username    =', incoming.caller_username);
console.log('     caller_share_code  =',
    incoming.caller_share_code
        ? incoming.caller_share_code.slice(0,8) + '…' + incoming.caller_share_code.slice(-4)
        : '(missing)');
console.log('     media_type         =', incoming.media_type);
console.log('     call_id            =', incoming.call_id?.slice(0,8));

const ok =
    incoming.caller_username === 'yousef2' &&
    incoming.caller_share_code &&
    incoming.caller_share_code.length === 64;
console.log();
console.log(ok ? '  ✓ PASS — caller identity fully populated'
              : '  ✗ FAIL — caller identity missing fields');

// Hang up so the test doesn't leave a phantom call alive.
sa.emit('v2_call_hangup', { call_id: incoming.call_id });
await new Promise(r => setTimeout(r, 200));

sa.close(); sb.close();
process.exit(ok ? 0 : 1);
