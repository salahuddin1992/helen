// server-core.js — same signaling logic as server.js but exported as a
// factory so the Electron main process can listen() on its own port.
// server.js delegates to this module so there's a single source of truth.

const path     = require('path');
const crypto   = require('crypto');

function create({ http, express, Server }) {
    const app    = express();
    const server = http.createServer(app);

    const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean);
    const io = new Server(server, {
        cors: {
            origin: (origin, cb) => {
                if (!origin) return cb(null, true);
                if (ALLOWED_ORIGINS.length && ALLOWED_ORIGINS.includes(origin)) return cb(null, true);
                if (/^https?:\/\/(127\.0\.0\.1|localhost|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$/i.test(origin)) {
                    return cb(null, true);
                }
                return cb(new Error('CORS: origin not allowed: ' + origin));
            },
        },
    });

    app.use(express.static(path.join(__dirname, 'public')));
    app.get('/healthz', (_req, res) => res.json({ ok: true, rooms: rooms.size }));

    const rooms = new Map();
    const users = new Map();

    const userIdFor = () => crypto.randomBytes(4).toString('hex');

    io.on('connection', (socket) => {
        const userId = userIdFor();
        users.set(socket.id, { userId, name: null, roomId: null });
        console.log(`[+] connection sid=${socket.id.slice(0, 6)} userId=${userId}`);

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

            const existing = [...room].map((sid) => {
                const v = users.get(sid);
                return v ? { socketId: sid, userId: v.userId, name: v.name } : null;
            }).filter(Boolean);

            room.add(socket.id);
            socket.join(roomId);

            socket.emit('joined-room', { roomId, you: { userId, name: u.name }, existing });
            socket.to(roomId).emit('user-joined', { socketId: socket.id, userId, name: u.name });
            console.log(`[+] ${userId} joined ${roomId}  (room size=${room.size})`);
        });

        socket.on('offer', ({ to, sdp }) => {
            if (!to || !sdp) return;
            const u = users.get(socket.id);
            io.to(to).emit('offer', { from: socket.id, fromUserId: u && u.userId, sdp });
        });
        socket.on('answer', ({ to, sdp }) => {
            if (!to || !sdp) return;
            const u = users.get(socket.id);
            io.to(to).emit('answer', { from: socket.id, fromUserId: u && u.userId, sdp });
        });
        socket.on('ice-candidate', ({ to, candidate }) => {
            if (!to || !candidate) return;
            io.to(to).emit('ice-candidate', { from: socket.id, candidate });
        });

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
        }
        socket.on('leave-room', handleLeave);
        socket.on('disconnect',  handleLeave);
    });

    return { app, server, io, rooms, users };
}

module.exports = { create };
