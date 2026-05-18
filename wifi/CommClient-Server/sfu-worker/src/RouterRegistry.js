/**
 * RouterRegistry — one Router per call_id.
 *
 * A Router is the mediasoup object that owns all Transports / Producers /
 * Consumers for a call. When the call ends we close the Router which closes
 * everything under it in one shot.
 *
 * Idle expiry: rooms with zero active transports for longer than the idle
 * timeout are auto-closed. Prevents leaks if the control server forgets to
 * DELETE the router.
 */

'use strict';

class RouterRegistry {
  /**
   * @param {object} opts
   * @param {import('./WorkerPool').WorkerPool} opts.workerPool
   * @param {object} opts.routerOpts — { mediaCodecs }
   * @param {object} opts.webRtcTransportOpts
   * @param {number} opts.idleTimeoutSec
   * @param {import('pino').Logger} opts.logger
   */
  constructor(opts) {
    this.opts = opts;
    this.logger = opts.logger.child({ component: 'RouterRegistry' });
    /** @type {Map<string, RoomState>} */
    this._rooms = new Map();
    this._sweepTimer = null;
  }

  start() {
    // Periodic idle sweep every 60s
    this._sweepTimer = setInterval(() => this._sweepIdle(), 60_000);
    this._sweepTimer.unref?.();
  }

  async shutdown() {
    if (this._sweepTimer) clearInterval(this._sweepTimer);
    this._sweepTimer = null;
    await Promise.all(
      Array.from(this._rooms.keys()).map((id) => this.closeRoom(id)),
    );
  }

  /**
   * Allocate (or return existing) Router for call_id. Idempotent.
   */
  async getOrCreate(callId) {
    const existing = this._rooms.get(callId);
    if (existing) {
      existing.lastActiveAt = Date.now();
      return existing;
    }

    const { worker } = this.opts.workerPool.pickWorker(callId);
    const router = await worker.createRouter(this.opts.routerOpts);

    /** @type {RoomState} */
    const room = {
      callId,
      router,
      transports: new Map(), // transportId -> mediasoup.Transport
      producers: new Map(), // producerId -> { producer, peerId, transportId }
      consumers: new Map(), // consumerId -> { consumer, peerId, transportId }
      peerProducers: new Map(), // peerId -> Set<producerId>
      // Created on-demand the first time an audio producer is added.
      audioObserver: null,
      // Active recordings keyed by internal recording_id.
      // Each entry: { id, audioProducerId?, videoProducerId?,
      //   audioRtpTransport?, videoRtpTransport?, ffmpegProc?, outputPath }
      recordings: new Map(),
      createdAt: Date.now(),
      lastActiveAt: Date.now(),
    };

    router.observer.on('close', () => {
      this.logger.info({ callId }, 'router closed');
      this._rooms.delete(callId);
    });

    this._rooms.set(callId, room);
    this.logger.info({ callId, workerPid: worker.pid }, 'router created');
    return room;
  }

  get(callId) {
    return this._rooms.get(callId) || null;
  }

  /** Close + remove a room. Safe to call twice. */
  async closeRoom(callId) {
    const room = this._rooms.get(callId);
    if (!room) return false;
    // Stop any active recordings first so ffmpeg flushes cleanly.
    try {
      for (const [, rec] of room.recordings) {
        this._closeRecording(rec).catch(() => {});
      }
      room.recordings.clear();
    } catch {
      /* ignore */
    }
    try {
      room.router.close();
    } catch (err) {
      this.logger.warn({ err, callId }, 'router close failed');
    }
    this._rooms.delete(callId);
    return true;
  }

  /**
   * Close a single recording entry: stop ffmpeg, close mediasoup transports.
   * Safe to call repeatedly.
   */
  async _closeRecording(rec) {
    if (!rec) return;
    try {
      if (rec.ffmpegProc && !rec.ffmpegProc.killed) {
        rec.ffmpegProc.kill('SIGINT');
      }
    } catch {
      /* ignore */
    }
    for (const key of ['audioRtpTransport', 'videoRtpTransport', 'audioConsumer', 'videoConsumer']) {
      const h = rec[key];
      if (h) {
        try { h.close(); } catch { /* ignore */ }
      }
    }
  }

  /** Remove a peer's producers/consumers/transports. Called when a peer leaves. */
  async removePeer(callId, peerId) {
    const room = this._rooms.get(callId);
    if (!room) return;
    const producerIds = room.peerProducers.get(peerId);
    if (producerIds) {
      for (const pid of producerIds) {
        const entry = room.producers.get(pid);
        if (entry) {
          try {
            entry.producer.close();
          } catch {
            /* ignore */
          }
          room.producers.delete(pid);
        }
      }
      room.peerProducers.delete(peerId);
    }
    // Close transports owned by this peer
    for (const [transportId, transport] of room.transports.entries()) {
      if (transport.appData?.peerId === peerId) {
        try {
          transport.close();
        } catch {
          /* ignore */
        }
        room.transports.delete(transportId);
      }
    }
    // Close consumers this peer was receiving
    for (const [cid, entry] of room.consumers.entries()) {
      if (entry.peerId === peerId) {
        try {
          entry.consumer.close();
        } catch {
          /* ignore */
        }
        room.consumers.delete(cid);
      }
    }
  }

  touch(callId) {
    const room = this._rooms.get(callId);
    if (room) room.lastActiveAt = Date.now();
  }

  _sweepIdle() {
    const cutoff = Date.now() - this.opts.idleTimeoutSec * 1000;
    for (const [callId, room] of this._rooms) {
      const hasActivity =
        room.transports.size > 0 ||
        room.producers.size > 0 ||
        room.consumers.size > 0;
      if (!hasActivity && room.lastActiveAt < cutoff) {
        this.logger.info({ callId }, 'idle router swept');
        this.closeRoom(callId).catch(() => {});
      }
    }
  }
}

/**
 * @typedef {object} RoomState
 * @property {string} callId
 * @property {import('mediasoup/node/lib/Router').Router} router
 * @property {Map<string, import('mediasoup/node/lib/WebRtcTransport').WebRtcTransport>} transports
 * @property {Map<string, { producer: any, peerId: string, transportId: string }>} producers
 * @property {Map<string, { consumer: any, peerId: string, transportId: string }>} consumers
 * @property {Map<string, Set<string>>} peerProducers
 * @property {number} createdAt
 * @property {number} lastActiveAt
 */

module.exports = { RouterRegistry };
