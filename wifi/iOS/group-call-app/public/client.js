/**
 * client.js — group call mesh.
 *
 * Joins a Socket.IO room and builds a full peer-to-peer mesh where
 * every participant has one RTCPeerConnection per other participant.
 *
 * Glare-prevention rule we follow:
 *   When you JOIN a room, YOU make the offer to every existing peer.
 *   When someone joins AFTER you, you wait — they offer to you.
 *
 * This is what fixes "every tab sees only itself": before, both tabs
 * tried to be the offerer at the same time, both peer connections
 * landed in `have-local-offer`, and neither could `setRemoteDescription`
 * the other's offer. With this convention, exactly one offer flows
 * per pair, the answer comes back, and media starts.
 */

(() => {
'use strict';

// ── Per-tab identity + room ─────────────────────────────────────────────
const params  = new URL(location.href).searchParams;
const roomId  = params.get('room') || 'lobby';
const myName  = params.get('name')  || _guestName();

document.getElementById('roomLabel').textContent = `Room: ${roomId}`;

// Generate a stable per-tab name fallback.
function _guestName() {
    const pool = ['Sand', 'Cloud', 'Wave', 'Pine', 'Ember', 'Frost',
                  'Ridge', 'Lark', 'Quill', 'Vale', 'Mint', 'Coral'];
    return pool[Math.floor(Math.random() * pool.length)] +
           '-' + Math.floor(Math.random() * 1000);
}

// ── State ──────────────────────────────────────────────────────────────
const peerConnections = {};   // socketId → RTCPeerConnection
const peerInfo        = {};   // socketId → { userId, name }
let   localStream     = null;
let   mySocketId      = null;

// Public STUN — fine for same-LAN testing. Add a TURN server here
// when you cross NATs.
const RTC_CONFIG = {
    iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' },
    ],
};

// ── Local media ────────────────────────────────────────────────────────
// Cap how long we wait for the user to act on the permission prompt. If
// they ignore it (close the popup but leave the page), `getUserMedia`
// hangs forever and the join is deferred forever — the user just sees
// "connecting…" with no recovery. After this timeout we treat it as a
// denial and proceed with no local media (audio/video disabled).
const MEDIA_TIMEOUT_MS = 30_000;

function _withTimeout(promise, ms, label) {
    return Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(
            () => reject(Object.assign(new Error('media permission timeout'), { name: 'TimeoutError' })),
            ms,
        )),
    ]);
}

async function acquireLocalMedia() {
    try {
        localStream = await _withTimeout(navigator.mediaDevices.getUserMedia({
            audio: true,
            video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        }), MEDIA_TIMEOUT_MS, 'video+audio');
    } catch (err) {
        console.warn('[media] getUserMedia(video+audio) failed:', err.name, err.message);
        // Common reasons:
        //   NotReadableError — another app/tab holds the camera. On a
        //                      single laptop with one webcam, only the
        //                      first browser process gets the device.
        //                      Launch Chrome with
        //                      `--use-fake-device-for-media-stream`
        //                      to give every profile its own synthetic
        //                      feed for local multi-tab testing.
        //   NotAllowedError  — user denied permission.
        try {
            localStream = await _withTimeout(
                navigator.mediaDevices.getUserMedia({ audio: true }),
                MEDIA_TIMEOUT_MS, 'audio-only',
            );
            console.warn('[media] running audio-only — camera unavailable');
        } catch (e2) {
            console.error('[media] no audio either:', e2.name, e2.message);
            alert('Cannot access camera or microphone — ' + e2.message);
            return null;
        }
    }
    addTile('self', { name: `${myName} (you)`, isLocal: true }, localStream);
    return localStream;
}

// ── PeerConnection factory ─────────────────────────────────────────────
function createPeer(socketId, info) {
    if (peerConnections[socketId]) return peerConnections[socketId];

    const pc = new RTCPeerConnection(RTC_CONFIG);
    peerConnections[socketId] = pc;
    peerInfo[socketId] = info;

    // Push our outbound tracks (mic + camera).
    if (localStream) {
        for (const track of localStream.getTracks()) pc.addTrack(track, localStream);
    }

    pc.ontrack = (ev) => {
        const stream = ev.streams && ev.streams[0];
        if (!stream) return;
        addTile(socketId, info, stream);
        console.log(`[remote stream added] ${info.name} (${socketId.slice(0,6)})`);
    };

    pc.onicecandidate = (ev) => {
        if (ev.candidate) {
            socket.emit('ice-candidate', { to: socketId, candidate: ev.candidate });
        }
    };

    pc.onconnectionstatechange = () => {
        console.log(`[pc:${socketId.slice(0,6)}] state=${pc.connectionState}`);
        if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
            removePeer(socketId);
        }
    };

    return pc;
}

function removePeer(socketId) {
    const pc = peerConnections[socketId];
    if (pc) {
        try { pc.close(); } catch {}
        delete peerConnections[socketId];
    }
    delete peerInfo[socketId];
    removeTile(socketId);
}

// ── Signaling ──────────────────────────────────────────────────────────
//
// We connect the socket immediately so the page shows "connected"
// quickly, but we hold off on `join-room` until local media is ready.
// Otherwise the joiner can race ahead and emit offers BEFORE local
// tracks are attached — the SDP carries no media, ICE still completes,
// and the remote `ontrack` never fires. Symptom: every tab shows only
// itself, even though pc.connectionState reads 'connected'.
const socket = io();
let _mediaResolved = false;      // true once getUserMedia returns (success or fail)
let _joinSent      = false;

function _maybeJoin() {
    if (_joinSent || !socket.connected || !_mediaResolved) return;
    _joinSent = true;
    socket.emit('join-room', { roomId, name: myName });
    console.log(`[joined room] roomId=${roomId} name=${myName} sid=${socket.id.slice(0,6)}`);
}

socket.on('connect', () => {
    mySocketId = socket.id;
    setStatus('connected');
    _maybeJoin();
});

socket.on('joined-room', async ({ roomId: rid, you, existing }) => {
    setStatus(`in room ${rid}`);
    console.log(`[joined room] ${rid} you=${you.userId} existing=${existing.length}`);
    // I'm the new joiner — I offer to everyone already in the room.
    for (const peer of existing) {
        const pc = createPeer(peer.socketId, peer);
        try {
            const offer = await pc.createOffer({
                offerToReceiveAudio: true,
                offerToReceiveVideo: true,
            });
            await pc.setLocalDescription(offer);
            socket.emit('offer', { to: peer.socketId, sdp: offer });
            console.log(`[offer sent] → ${peer.name} (${peer.socketId.slice(0,6)})`);
        } catch (err) {
            console.error('[offer failed]', err);
        }
    }
});

socket.on('user-joined', (peer) => {
    console.log(`[user joined] ${peer.name} (${peer.socketId.slice(0,6)})`);
    // Pre-create the pc so local tracks are attached and ICE can start.
    // We do NOT offer — the joiner will offer to us.
    createPeer(peer.socketId, peer);
});

socket.on('offer', async ({ from, fromUserId, sdp }) => {
    console.log(`[offer received] ← ${fromUserId} (${from.slice(0,6)})`);
    const info = peerInfo[from] || { userId: fromUserId, name: fromUserId };
    const pc = createPeer(from, info);
    try {
        await pc.setRemoteDescription(sdp);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        socket.emit('answer', { to: from, sdp: answer });
        console.log(`[answer sent] → ${info.name} (${from.slice(0,6)})`);
    } catch (err) {
        console.error('[answer failed]', err);
    }
});

socket.on('answer', async ({ from, sdp }) => {
    console.log(`[answer received] ← ${from.slice(0,6)}`);
    const pc = peerConnections[from];
    if (!pc) return;
    try { await pc.setRemoteDescription(sdp); }
    catch (err) { console.error('[setRemoteDescription failed]', err); }
});

socket.on('ice-candidate', async ({ from, candidate }) => {
    const pc = peerConnections[from];
    if (!pc || !candidate) return;
    try {
        await pc.addIceCandidate(candidate);
        console.log(`[ice candidate received] ← ${from.slice(0,6)}`);
    } catch (err) {
        // It's normal for a stray candidate to fail right after a
        // peer disconnects; only complain if a pc still exists.
        if (peerConnections[from]) {
            console.warn('[addIceCandidate]', err.message);
        }
    }
});

socket.on('user-left', ({ socketId, userId }) => {
    console.log(`[user left] ${userId} (${socketId.slice(0,6)})`);
    removePeer(socketId);
});

socket.on('disconnect', () => setStatus('disconnected'));
socket.on('error-msg', (m) => console.error('[server-error]', m));

// ── Tile / grid renderer ───────────────────────────────────────────────
//
// Every participant — including the local user — gets a tile. The
// grid recomputes its column count whenever the tile count changes.

const grid             = document.getElementById('videoGrid');
const participantCount = document.getElementById('participantCount');

function addTile(id, info, stream) {
    let tile = document.getElementById(`tile-${id}`);
    if (!tile) {
        tile = document.createElement('div');
        tile.id = `tile-${id}`;
        tile.className = 'tile' + (info.isLocal ? ' is-local' : '');
        tile.innerHTML = `
            <video autoplay playsinline ${info.isLocal ? 'muted' : ''}></video>
            <div class="strip"><span class="name"></span></div>
        `;
        grid.appendChild(tile);
    }
    tile.querySelector('.name').textContent = info.name || id.slice(0, 6);
    const v = tile.querySelector('video');
    if (v.srcObject !== stream) v.srcObject = stream;
    updateGridLayout(grid.children.length);
}

function removeTile(id) {
    const tile = document.getElementById(`tile-${id}`);
    if (!tile) return;
    tile.remove();
    updateGridLayout(grid.children.length);
}

function updateGridLayout(count) {
    let cols;
    if (count <= 1)        cols = 1;
    else if (count === 2)  cols = 2;
    else if (count <= 4)   cols = 2;       // 3 → 2 cols (one centred row of 1)
    else if (count <= 9)   cols = 3;
    else if (count <= 16)  cols = 4;
    else                   cols = Math.ceil(Math.sqrt(count));
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    participantCount.textContent = count;
}

// ── Controls ───────────────────────────────────────────────────────────
const btnMute = document.getElementById('btnMute');
const btnCam  = document.getElementById('btnCam');
const btnEnd  = document.getElementById('btnEnd');

btnMute.addEventListener('click', () => {
    if (!localStream) return;
    const tracks = localStream.getAudioTracks();
    if (!tracks.length) return;
    const enabled = !tracks[0].enabled;
    for (const t of tracks) t.enabled = enabled;
    btnMute.classList.toggle('is-active', !enabled);
});
btnCam.addEventListener('click', () => {
    if (!localStream) return;
    const tracks = localStream.getVideoTracks();
    if (!tracks.length) return;
    const enabled = !tracks[0].enabled;
    for (const t of tracks) t.enabled = enabled;
    btnCam.classList.toggle('is-active', !enabled);
    document.getElementById('tile-self')?.classList.toggle('cam-off', !enabled);
});
btnEnd.addEventListener('click', () => {
    socket.emit('leave-room');
    for (const id of Object.keys(peerConnections)) removePeer(id);
    if (localStream) for (const t of localStream.getTracks()) try { t.stop(); } catch {}
    location.reload();
});

window.addEventListener('beforeunload', () => {
    socket.emit('leave-room');
});

// ── Status helpers ─────────────────────────────────────────────────────
function setStatus(text) {
    document.getElementById('status').textContent = text;
}

// ── Duration ticker ────────────────────────────────────────────────────
const durationEl = document.getElementById('duration');
const startedAt  = Date.now();
setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    durationEl.textContent = h ? `${pad(h)}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`;
}, 1000);

// ── Boot ───────────────────────────────────────────────────────────────
//
// Mark media resolved whether getUserMedia succeeded, fell back to
// audio-only, or failed entirely — we still want to join the room so
// the user is at least visible to others (audio + a placeholder tile).
acquireLocalMedia().finally(() => { _mediaResolved = true; _maybeJoin(); });

})();
