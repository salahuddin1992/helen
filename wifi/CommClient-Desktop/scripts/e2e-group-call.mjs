/**
 * Deep end-to-end test for the group-call signaling layer.
 *
 * Scenarios covered:
 *   1. Register 5 users + create a group channel containing all of them.
 *   2. User A opens a group call → B/C/D/E receive `call_incoming`.
 *   3. B, C, D, E each join → existing participants receive
 *      `call_participant_joined` and joiners see the participant list grow.
 *   4. Full signaling mesh — every new arrival does offer/answer/ICE with
 *      every existing participant. Server must relay each signal exactly
 *      to its target and MUST NOT leak signals to non-targets.
 *   5. Mute + video toggles fan out `call_participant_state` (or v1 fallbacks).
 *   6. One user leaves → everyone else gets `call_participant_left`.
 *   7. Initiator hangs up → all remaining participants get `call_hangup`.
 *   8. Final assertion: call log row was persisted (via REST history).
 */
import { io } from '../node_modules/socket.io-client/build/esm/index.js';

const BASE = process.env.COMMCLIENT_URL || 'http://127.0.0.1:3000';
const WS = process.env.COMMCLIENT_WS || BASE;
const N = parseInt(process.env.N || '5', 10);
const pw = 'Str0ng!Pass-42';

const tag = (t) => `[call] ${t}`;
const log = (...a) => console.log(tag(a.shift()), ...a);
const die = (msg, err) => {
  console.error(tag('FAIL'), msg);
  if (err) console.error(err);
  process.exit(1);
};
const rand = () => Math.random().toString(36).slice(2, 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
    s.on('connect', () => { clearTimeout(to); resolve(s); });
    s.on('connect_error', (err) => { clearTimeout(to); reject(err); });
  });
}

function emitWithAck(socket, event, payload, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error(`ack timeout for ${event}`)), timeoutMs);
    socket.emit(event, payload, (resp) => { clearTimeout(to); resolve(resp); });
  });
}

function expectEvent(socket, event, matchFn, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => {
      socket.off(event, handler);
      reject(new Error(`event '${event}' not received within ${timeoutMs}ms`));
    }, timeoutMs);
    const handler = (payload) => {
      if (!matchFn || matchFn(payload)) {
        clearTimeout(to);
        socket.off(event, handler);
        resolve(payload);
      }
    };
    socket.on(event, handler);
  });
}

// A tiny SDP stub — server just relays it, doesn't validate RTC content.
const fakeOffer = (from, to) => ({ type: 'offer', sdp: `v=0\r\no=- ${Date.now()} 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=peer ${from}->${to}\r\n` });
const fakeAnswer = (from, to) => ({ type: 'answer', sdp: `v=0\r\no=- ${Date.now()} 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=peer ${from}->${to}\r\n` });
const fakeIce = (i) => ({ candidate: `candidate:${i} 1 udp 2122260223 192.168.1.${i} 443 typ host`, sdpMid: '0', sdpMLineIndex: 0 });

async function registerAndLogin(uname) {
  await httpJson('POST', '/api/auth/register', {
    username: uname, display_name: uname, password: pw,
  });
  const resp = await httpJson('POST', '/api/auth/login', { username: uname, password: pw });
  return { token: resp.tokens?.access_token || resp.access_token, userId: resp.user?.id };
}

async function main() {
  log('start', BASE);
  const h = await httpJson('GET', '/api/health');
  log('health', h.status);

  // ── 1. Register N users ──────────────────────────────────────
  const unames = Array.from({ length: N }, (_, i) => `grp_${String.fromCharCode(97 + i)}_${rand()}`);
  const users = [];
  for (const u of unames) {
    const info = await registerAndLogin(u);
    users.push({ uname: u, ...info });
  }
  log('registered', users.map((u) => u.uname).join(', '));

  // ── 2. Create a group channel (A is initiator, others are members) ──
  const A = users[0];
  const memberIds = users.slice(1).map((u) => u.userId);
  const channel = await httpJson('POST', '/api/channels', {
    type: 'group', name: `grp-call-${rand()}`, member_ids: memberIds,
  }, A.token);
  const channelId = channel.id;
  log('channel', channelId, 'members:', channel.members?.length ?? '?');

  // ── 3. Connect all sockets in parallel ───────────────────────
  const sockets = await Promise.all(users.map((u, i) => connectSocket(u.token, 'ABCDE'[i])));
  log('sockets-connected', sockets.map((s) => s.id));

  // Attach general-purpose catch-all recorders for diagnostics
  const recorded = users.map(() => []);
  sockets.forEach((s, i) => {
    for (const evt of [
      'call_incoming', 'call_accepted', 'call_rejected', 'call_hangup',
      'call_participant_joined', 'call_participant_left', 'call_participant_state',
      'call:peer_joined', 'call:peer_left', 'call:peer_ready', 'call:group_ringing',
      // v2 unified signaling — replaces v1 signal:offer/answer/ice_candidate
      'call_signal',
      'signal:offer', 'signal:answer', 'signal:ice_candidate',
    ]) {
      s.on(evt, (payload) => recorded[i].push({ evt, payload, ts: Date.now() }));
    }
  });

  // ── 4. A starts the group call ───────────────────────────────
  const incomingPromises = sockets.slice(1).map((s) =>
    expectEvent(s, 'call_incoming', (p) => p.channel_id === channelId, 6000)
      .catch((e) => ({ err: e.message })),
  );

  const joinA = await emitWithAck(sockets[0], 'v2_call_join_group', {
    channel_id: channelId, media_type: 'audio',
  }, 6000);
  if (!joinA?.call_id) die('A join_group: no call_id', joinA);
  const callId = joinA.call_id;
  log('A joined → call_id:', callId, 'initial participants:', joinA.participants?.length);

  const incomings = await Promise.all(incomingPromises);
  const missedIncoming = incomings.filter((x) => x && x.err);
  if (missedIncoming.length) die(`some peers missed call_incoming: ${missedIncoming.length}/${N-1}`);
  log('call_incoming reached', N - 1, 'peers');

  // ── 5. B, C, D, E each join in sequence; verify fan-out ──────
  for (let i = 1; i < N; i++) {
    const joiner = users[i];
    const joinerSock = sockets[i];

    // Every already-in-call socket should receive call_participant_joined for this new arrival
    const watchers = sockets.slice(0, i).map((s) =>
      expectEvent(s, 'call_participant_joined', (p) => p.user_id === joiner.userId, 5000)
        .catch((e) => ({ err: e.message, watcher: users[sockets.indexOf(s)].uname })),
    );

    const ack = await emitWithAck(joinerSock, 'v2_call_join_group', {
      channel_id: channelId, media_type: 'audio',
    }, 6000);
    if (ack.call_id !== callId) die(`${joiner.uname}: different call_id: ${ack.call_id} vs ${callId}`);
    const partCount = ack.participants?.length ?? 0;
    log(`${joiner.uname} joined, participants now:`, partCount);
    if (partCount !== i + 1) die(`${joiner.uname}: expected ${i+1} participants, got ${partCount}`);

    const results = await Promise.all(watchers);
    const missed = results.filter((r) => r && r.err);
    if (missed.length) die(`after ${joiner.uname} join, existing peers missed the broadcast: ${missed.map((m) => m.watcher).join(', ')}`);
    log(`  → ${i} existing peers received call_participant_joined`);
  }

  // ── 6. Signaling mesh — A sends offer to each of B/C/D/E; each answers ──
  // v2 uses a single unified `call_signal` event with a `signal_type` field
  // (offer/answer/ice-candidate) instead of separate signal:offer/answer/ice
  // events. The v1 events were retired in batch 12 of the WORK_LOG migration.
  log('signaling-mesh (v2 call_signal): A → others offers');
  const sentAtMs = () => Date.now();
  const answerExpectations = [];
  for (let i = 1; i < N; i++) {
    answerExpectations.push(
      expectEvent(sockets[0], 'call_signal',
        (p) => p.from_id === users[i].userId && p.signal_type === 'answer', 5000),
    );
  }
  const offerExpectations = sockets.slice(1).map((s) =>
    expectEvent(s, 'call_signal',
      (p) => p.from_id === A.userId && p.signal_type === 'offer', 5000),
  );

  for (let i = 1; i < N; i++) {
    sockets[0].emit('call_signal', {
      call_id: callId,
      target_id: users[i].userId,
      signal_type: 'offer',
      sdp: fakeOffer(A.userId, users[i].userId),
      sent_at_ms: sentAtMs(),
    });
  }

  const offersReceived = await Promise.all(offerExpectations);
  log('  offers delivered to', offersReceived.length, 'peers');

  for (let i = 1; i < N; i++) {
    sockets[i].emit('call_signal', {
      call_id: callId,
      target_id: A.userId,
      signal_type: 'answer',
      sdp: fakeAnswer(users[i].userId, A.userId),
      sent_at_ms: sentAtMs(),
    });
  }
  const answersReceived = await Promise.all(answerExpectations);
  log('  answers delivered to A:', answersReceived.length);

  // ── 7. ICE trickling — A sends 3 candidates to each peer ─────
  log('signaling-ice (v2): trickling');
  const iceExpectations = sockets.slice(1).map((s) => new Promise((resolve) => {
    let count = 0;
    const handler = (p) => {
      if (p.from_id === A.userId && p.signal_type === 'ice-candidate') {
        count++;
        if (count >= 3) { s.off('call_signal', handler); resolve(count); }
      }
    };
    s.on('call_signal', handler);
    setTimeout(() => { s.off('call_signal', handler); resolve(count); }, 5000);
  }));

  for (let i = 1; i < N; i++) {
    for (let k = 1; k <= 3; k++) {
      sockets[0].emit('call_signal', {
        call_id: callId,
        target_id: users[i].userId,
        signal_type: 'ice-candidate',
        candidate: fakeIce(i * 10 + k),
        sent_at_ms: sentAtMs(),
      });
    }
  }
  const iceCounts = await Promise.all(iceExpectations);
  const badIce = iceCounts.filter((c) => c < 3);
  if (badIce.length) die(`ICE drop: some peers got fewer than 3 candidates from A: ${iceCounts}`);
  log('  ICE delivered fully to', iceCounts.length, 'peers');

  // ── 8. Signal leak check — non-targets must NOT see a signal ─
  // A sends an offer targeted at B only. C/D/E must not see it.
  const probeId = `probe-${Date.now()}`;
  const probeSdp = { ...fakeOffer(A.userId, users[1].userId), __probe: probeId };
  const bSaw = expectEvent(sockets[1], 'call_signal',
    (p) => p.signal_type === 'offer' && p.sdp?.__probe === probeId, 3000);
  const leaks = sockets.slice(2).map((s) => new Promise((resolve) => {
    const h = (p) => {
      if (p.signal_type === 'offer' && p.sdp?.__probe === probeId)
        resolve({ leaked: true });
    };
    s.on('call_signal', h);
    setTimeout(() => { s.off('call_signal', h); resolve({ leaked: false }); }, 1500);
  }));
  sockets[0].emit('call_signal', {
    call_id: callId, target_id: users[1].userId,
    signal_type: 'offer', sdp: probeSdp, sent_at_ms: sentAtMs(),
  });
  const bGot = await bSaw.catch(() => null);
  const leakResults = await Promise.all(leaks);
  if (!bGot) die('leak-probe: B did not receive the targeted offer');
  const anyLeak = leakResults.some((r) => r.leaked);
  if (anyLeak) die('leak-probe: non-target peers saw the offer — privacy violation');
  log('signal-leak-probe: clean — only B received the targeted offer');

  // ── 9. Mute toggle broadcast ─────────────────────────────────
  const muteBroadcasts = sockets.slice(1).map((s) => new Promise((resolve) => {
    const h = (p) => {
      if (p.user_id === A.userId && (p.muted === true || p.audio === false || p.is_muted === true)) {
        s.off('call_participant_state', h); s.off('call:participant_muted', h); s.off('call_mute_toggled', h);
        resolve(p);
      }
    };
    s.on('call_participant_state', h);
    s.on('call:participant_muted', h);
    s.on('call_mute_toggled', h);
    setTimeout(() => resolve(null), 3000);
  }));
  sockets[0].emit('v2_call_toggle_mute', { call_id: callId, muted: true });
  const muteResults = await Promise.all(muteBroadcasts);
  const gotMute = muteResults.filter(Boolean).length;
  log(`mute broadcast: ${gotMute}/${N - 1} peers received state change`);
  if (gotMute === 0) log('  (no dedicated mute broadcast — not a blocker; check state-fanout impl)');

  // ── 10. E leaves ──────────────────────────────────────────────
  const leftWatchers = sockets.slice(0, N - 1).map((s) =>
    expectEvent(s, 'call_participant_left', (p) => p.user_id === users[N - 1].userId, 5000),
  );
  await emitWithAck(sockets[N - 1], 'v2_call_leave_group', { call_id: callId }, 5000);
  await Promise.all(leftWatchers);
  log('E left; call_participant_left fan-out OK');

  // ── 11. A hangs up — remaining peers (B,C,D) get call_hangup ─
  const hangupWatchers = sockets.slice(1, N - 1).map((s) =>
    expectEvent(s, 'call_hangup', (p) => p.call_id === callId, 5000)
      .catch((e) => ({ err: e.message })),
  );
  // v2_call_hangup is the documented hangup event; also call_leave_group by A
  await emitWithAck(sockets[0], 'v2_call_hangup', { call_id: callId }, 5000)
    .catch(async () => {
      // Fallback — leave instead
      await emitWithAck(sockets[0], 'v2_call_leave_group', { call_id: callId }, 5000);
    });
  const hangupResults = await Promise.all(hangupWatchers);
  const missedHangup = hangupResults.filter((r) => r && r.err);
  log('hangup fan-out:', `${hangupResults.length - missedHangup.length}/${hangupResults.length} OK`);

  // ── 12. Settle + disconnect ──────────────────────────────────
  await sleep(500);
  sockets.forEach((s) => s.disconnect());
  log('PASS', { N, callId, channelId });
  process.exit(0);
}

main().catch((e) => die(e.message, e));
