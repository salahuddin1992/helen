// server.js — Express + Socket.IO signaling server for the group call
// demo. The server never sees media; it only relays SDP offers/answers
// and ICE candidates between peers in the same room. The actual audio
// and video flow peer-to-peer over WebRTC.
//
// The room model is deliberately tiny:
//   • Every socket carries a `userId` (assigned when it connects).
//   • A room is just a set of socketIds keyed by `roomId` — clients
//     ask to join one with `join-room`.
//   • When a client joins, we tell the joiner who's already there and
//     tell the existing members about the joiner.
//   • Convention: the *joiner* makes the offer to every existing
//     member. Existing members wait. This avoids WebRTC "glare" where
//     both sides try to offer simultaneously.
//
// Run with:  npm install && npm start
// Then open: http://127.0.0.1:3099/?room=test  in several tabs.

const path     = require('path');
const http     = require('http');
const crypto   = require('crypto');
const express  = require('express');
const { Server } = require('socket.io');

const PORT = Number(process.env.PORT) || 3099;

const app    = express();
const server = http.createServer(app);
// CORS — allow same-origin (the page served by this Express process) and
// any RFC1918 LAN host. Public deployments should set ALLOWED_ORIGINS to
// an explicit comma-separated allowlist.
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
const io     = new Server(server, {
    cors: {
        origin: (origin, cb) => {
            if (!origin) return cb(null, true);                          // same-origin / non-browser
            if (ALLOWED_ORIGINS.length && ALLOWED_ORIGINS.includes(origin)) return cb(null, true);
            // Default: accept localhost + RFC1918 LAN ranges.
            if (/^https?:\/\/(127\.0\.0\.1|localhost|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$/i.test(origin)) {
                return cb(null, true);
            }
            return cb(new Error('CORS: origin not allowed: ' + origin));
        },
    },
});

// Static client assets.
app.use(express.static(path.join(__dirname, 'public')));

// Tiny health check — useful when integrating with monitoring.
app.get('/healthz', (_req, res) => res.json({ ok: true, rooms: rooms.size }));

// ── Room registry ─────────────────────────────────────────────────────
//
// `rooms`  : roomId → Set<socketId>
// `users`  : socketId → { userId, name, roomId }
//
// Two structures so we can answer both
//   "who is in room X?"             (rooms.get(roomId))
//   "what room is socket Y in?"     (users.get(socketId).roomId)
// without an O(n) scan.
const rooms = new Map();
const users = new Map();

function userIdFor(socket) {
    // 8 hex chars are plenty for a single LAN session and short enough
    // to read in logs.
    return crypto.randomBytes(4).toString('hex');
}

io.on('connection', (socket) => {
    const userId = userIdFor(socket);
    users.set(socket.id, { userId, name: null, roomId: null });
    console.log(`[+] connection sid=${socket.id.slice(0, 6)} userId=${userId}`);

    // ── join-room ────────────────────────────────────────────
    //
    // Payload: { roomId, name }
    // Server tells the joiner who's already there and broadcasts a
    // `user-joined` to existing members so they can prep their pcs.
    socket.on('join-room', ({ roomId, name } = {}) => {
        if (!roomId || typeof roomId !== 'string') {
            socket.emit('error-msg', { msg: 'roomId required' });
            return;
        }

        const u = users.get(socket.id);
        u.name   = name || 'Guest';
        u.roomId = roomId;

        if (!rooms.has(roomId)) rooms.set(roomId, new Set());
        const room = rooms.get(roomId);

        // Existing peers (before we add the joiner).
        const existing = [...room].map((sid) => {
            const v = users.get(sid);
            return v ? { socketId: sid, userId: v.userId, name: v.name } : null;
        }).filter(Boolean);

        room.add(socket.id);
        socket.join(roomId);

        // Tell the joiner who's already in — *they* will offer to each.
        socket.emit('joined-room', { roomId, you: { userId, name: u.name }, existing });

        // Tell existing members someone arrived. They prep peer
        // connections but DON'T offer; they wait for the joiner.
        socket.to(roomId).emit('user-joined', {
            socketId: socket.id, userId, name: u.name,
        });

        console.log(`[+] ${userId} joined ${roomId}  (room size=${room.size})`);
    });

    // ── Signaling relays ─────────────────────────────────────
    //
    // Payload shape for all three:  { to: <socketId>, sdp|candidate }
    // We add `from` server-side so the recipient can route to the
    // correct RTCPeerConnection in their `peerConnections[userId]` map.
    socket.on('offer', ({ to, sdp }) => {
        if (!to || !sdp) return;
        const u = users.get(socket.id);
        io.to(to).emit('offer', { from: socket.id, fromUserId: u && u.userId, sdp });
        console.log(`[→] offer  ${u && u.userId} → ${to.slice(0, 6)}`);
    });
    socket.on('answer', ({ to, sdp }) => {
        if (!to || !sdp) return;
        const u = users.get(socket.id);
        io.to(to).emit('answer', { from: socket.id, fromUserId: u && u.userId, sdp });
        console.log(`[←] answer ${u && u.userId} → ${to.slice(0, 6)}`);
    });
    socket.on('ice-candidate', ({ to, candidate }) => {
        if (!to || !candidate) return;
        io.to(to).emit('ice-candidate', { from: socket.id, candidate });
    });

    // ── Departure ─────────────────────────────────────────────
    function handleLeave() {
        const u = users.get(socket.id);
        if (!u) return;
        const { roomId, userId: leaverId } = u;
        users.delete(socket.id);
        if (!roomId) return;
        const room = rooms.get(roomId);
        if (!room) return;
        room.delete(socket.id);
        if (room.size === 0) rooms.delete(roomId);
        socket.to(roomId).emit('user-left', { socketId: socket.id, userId: leaverId });
        console.log(`[-] ${leaverId} left ${roomId} (room size=${room.size})`);
    }
    socket.on('leave-room', handleLeave);
    socket.on('disconnect',  handleLeave);
});

server.listen(PORT, () => {
    console.log();
    console.log(`Group call server listening on http://127.0.0.1:${PORT}`);
    console.log(`Open in several tabs:  http://127.0.0.1:${PORT}/?room=test`);
    console.log();
});
