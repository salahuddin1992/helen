/**
 * TopologyCoordinator — client-side half of the Mesh ⇄ SFU hybrid topology.
 *
 * Responsibilities:
 *   • Listen on the shared Socket.IO connection for `topology_switch`
 *     (emitted by the server's TopologyManager).
 *   • On switch: tear down PeerConnections from older generations, renegotiate
 *     per the new routing ("p2p" | "mesh" | "sfu"), and send back
 *     `call_topology_ack` with the new generation.
 *   • Periodically sample RTCStatsReport from the active peer connections and
 *     emit `call_quality_report` so the server's QualityOracle can adapt.
 *   • Handle signal replay on reconnect (`call_signal_replay`).
 *
 * This class is ADDITIVE — it does NOT modify GroupCallManager. It composes
 * with an existing GroupCallManager instance and an SFU adapter (mediasoup
 * or a stub that logs and no-ops until the SFU bridge is wired up).
 */

import { socketManager } from '../socket.manager';
import type { GroupCallManager, GroupParticipant } from './GroupCallManager';
import type { PeerConnection } from './PeerConnection';

// ─── Types ──────────────────────────────────────────────────────────────────

export type CallRoutingMode = 'p2p' | 'mesh' | 'sfu' | 'hybrid';

export interface SFUInfo {
  backend?: string;
  url?: string;
  producer_token?: string;
  rtp_capabilities?: any;
  transport_options?: any;
}

export interface TopologySwitchEvent {
  call_id: string;
  new_routing: CallRoutingMode;
  generation: number;
  sfu?: SFUInfo | null;
  reason?: string;
}

export interface QualitySample {
  peer_id: string;
  packet_loss: number;  // 0..1
  rtt_ms: number;
  jitter_ms: number;
}

export interface TopologyCoordinatorConfig {
  /** Call id, must match server-side identifier. */
  callId: string;
  /** Local user id (for ack payloads). */
  localUserId: string;
  /** The GroupCallManager instance managing the mesh PeerConnections. */
  groupManager: GroupCallManager;
  /** Optional SFU adapter. If omitted, `sfu` switches log-and-warn. */
  sfuAdapter?: ISFUAdapter | null;
  /** How often to sample quality and emit call_quality_report (ms). */
  qualitySampleIntervalMs?: number;
  /** Lifecycle hooks for UI. */
  onRoutingChanged?: (routing: CallRoutingMode, generation: number) => void;
  onError?: (msg: string) => void;
}

export interface ISFUAdapter {
  connect(info: NonNullable<TopologySwitchEvent['sfu']>): Promise<void>;
  publish(stream: MediaStream): Promise<void>;
  consume(
    peerId: string,
    onStream: (stream: MediaStream) => void,
  ): Promise<void>;
  disconnect(): Promise<void>;
  /** Pause/resume producers of a given kind. Returns affected count. */
  setProducerPaused?(kind: 'audio' | 'video', paused: boolean): Promise<number>;
  /** Publish a screen-share stream as a tagged producer. */
  publishScreenShare?(stream: MediaStream): Promise<void>;
  /** Stop and close the local screen-share producer(s). */
  unpublishScreenShare?(): Promise<void>;
  /**
   * Swap the underlying track on an existing camera/mic producer without
   * renegotiating — used when the user switches input devices mid-call
   * (hardware camera → paired phone, etc.). Returns true if a producer of
   * the requested kind was found and the swap succeeded.
   */
  replaceTrack?(kind: 'audio' | 'video', newTrack: MediaStreamTrack): Promise<boolean>;
}

// ─── Stub adapter (used until mediasoup-client is installed) ────────────────

class NoopSFUAdapter implements ISFUAdapter {
  async connect(_info: NonNullable<TopologySwitchEvent['sfu']>): Promise<void> {
    console.warn('[Topology] No SFU adapter installed — staying on mesh');
  }
  async publish(_s: MediaStream): Promise<void> {}
  async consume(_p: string, _cb: (s: MediaStream) => void): Promise<void> {}
  async disconnect(): Promise<void> {}
}

// ─── Coordinator ────────────────────────────────────────────────────────────

const DEFAULT_QUALITY_INTERVAL_MS = 5_000;

export class TopologyCoordinator {
  private _generation = 1;
  private _routing: CallRoutingMode = 'mesh';
  private _unsubs: Array<() => void> = [];
  private _qualityTimer: ReturnType<typeof setInterval> | null = null;
  private _destroyed = false;
  private _sfu: ISFUAdapter;
  private _config: TopologyCoordinatorConfig;
  /**
   * Set by the `disconnect` listener; cleared by the next `connect`. While
   * true, the very next `connect` event triggers a catch-up signal replay.
   */
  private _pendingReplayOnReconnect = false;

  constructor(config: TopologyCoordinatorConfig) {
    this._config = config;
    this._sfu = config.sfuAdapter ?? new NoopSFUAdapter();
  }

  /** Current SFU adapter (for hooks that need direct access, e.g. mute). */
  get sfu(): ISFUAdapter {
    return this._sfu;
  }

  /** Current routing ('p2p' | 'mesh' | 'sfu' | 'hybrid'). */
  get routing(): CallRoutingMode {
    return this._routing;
  }

  // ── Public API ────────────────────────────────────────────────────────

  /** Attach to the shared Socket.IO client and start listening for events. */
  start(): void {
    if (this._destroyed) throw new Error('TopologyCoordinator destroyed');

    const offSwitch = socketManager.on(
      'topology_switch',
      (payload: TopologySwitchEvent) => {
        if (payload?.call_id !== this._config.callId) return;
        this._handleSwitch(payload).catch((err) => {
          console.error('[Topology] switch handler failed:', err);
          this._config.onError?.(String(err?.message ?? err));
        });
      },
    );
    this._unsubs.push(offSwitch);

    // ── Auto signal-replay on socket reconnect ──────────────────────────
    // Problem: while the socket was down, the server almost certainly
    // recorded new offer/answer/ice events (peers joining, topology shifts).
    // The stock `socket.io` client silently re-binds event listeners on
    // reconnect but cannot backfill missed messages — that's our job.
    //
    // We latch on `disconnect`, and then on the next `connect` trigger a
    // replay with `since_generation = current`. The TopologyCoordinator's
    // existing truncation-fallback path kicks in automatically if the
    // replay log has rolled over during the outage.
    const offDisconnect = socketManager.on('disconnect', (_reason: string) => {
      this._pendingReplayOnReconnect = true;
    });
    this._unsubs.push(offDisconnect);

    const offConnect = socketManager.on('connect', () => {
      if (!this._pendingReplayOnReconnect) return;
      this._pendingReplayOnReconnect = false;
      // Give the socket a beat to finish auth so the server has our
      // session mapped before we request replay.
      setTimeout(() => {
        if (this._destroyed) return;
        this.requestReplay(this._generation).catch((err) =>
          console.warn('[Topology] post-reconnect replay failed:', err),
        );
      }, 250);
    });
    this._unsubs.push(offConnect);

    this._startQualityLoop();
  }

  /**
   * Call this after a socket reconnection to pull any missed signaling.
   * The server returns the events via the Socket.IO ack — we apply them here.
   *
   * If the server reports `truncated=true`, replay alone is insufficient to
   * rebuild ICE state safely (missing offer/answer pairs would leave us in
   * an inconsistent mesh), so we fall back to a full renegotiation of every
   * peer connection.
   */
  async requestReplay(lastGeneration?: number): Promise<void> {
    try {
      const res = await socketManager.emit('call_signal_replay', {
        call_id: this._config.callId,
        since_generation: lastGeneration ?? this._generation,
      });
      if (res && res.ok) {
        if (typeof res.generation === 'number') {
          this._generation = Math.max(this._generation, res.generation);
        }
        if (res.routing) this._routing = res.routing;

        const truncated = res.truncated === true;
        if (truncated) {
          console.warn(
            '[Topology] replay truncated — falling back to full renegotiation of all peers',
          );
          try {
            await this._fullRenegotiateAll();
          } catch (err) {
            this._config.onError?.(
              `full-renegotiate after truncated replay failed: ${String((err as any)?.message ?? err)}`,
            );
          }
        } else {
          this._applyReplayedSignals({ events: res.signals ?? [] });
        }
      }
    } catch (err) {
      console.warn('[Topology] replay request failed', err);
    }
  }

  /**
   * Tear down every live peer connection and rebuild them from scratch.
   * Used as the safety net when the signal-replay log is truncated.
   */
  private async _fullRenegotiateAll(): Promise<void> {
    const gm: any = this._config.groupManager;
    const participants: GroupParticipant[] = gm.allParticipants ?? [];

    // First pass: destroy all existing PCs cleanly.
    for (const p of participants) {
      if (p.connection && !p.connection.destroyed) {
        try { (p.connection as PeerConnection).destroy(); } catch {/* ignore */}
      }
      p.connection = null;
      p.remoteStream = null;
    }

    // Second pass: re-add each participant so GroupCallManager recreates
    // PCs from scratch with the correct offerer/answerer polarity.
    // Lexicographic ordering matches the existing mesh convention.
    const localId: string = (gm.localUserId as string) ?? '';
    for (const p of participants) {
      try {
        const isInitiator = !!localId && localId < p.peerId;
        if (typeof gm.addParticipant === 'function') {
          gm.addParticipant(p.peerId, isInitiator);
        }
      } catch (err) {
        console.warn('[Topology] full-renegotiate re-add failed for', p.peerId, err);
      }
    }
  }

  currentRouting(): CallRoutingMode { return this._routing; }
  currentGeneration(): number { return this._generation; }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;
    for (const u of this._unsubs) {
      try { u(); } catch {/* ignore */}
    }
    this._unsubs.length = 0;
    if (this._qualityTimer) {
      clearInterval(this._qualityTimer);
      this._qualityTimer = null;
    }
    try { this._sfu.disconnect(); } catch {/* ignore */}
  }

  // ── Switch handler ────────────────────────────────────────────────────

  private async _handleSwitch(payload: TopologySwitchEvent): Promise<void> {
    if (payload.generation < this._generation) {
      // Stale message — ignore.
      return;
    }

    const prev = this._routing;
    const next = payload.new_routing;
    const gen = payload.generation;

    console.log(
      `[Topology] switch ${prev} → ${next} (gen ${this._generation} → ${gen}, reason=${payload.reason ?? 'n/a'})`,
    );

    // 1. Tear down everything tied to the older generation.
    if (next !== prev) {
      await this._tearDownCurrent(prev);
    }

    // 2. Wire up the new topology.
    if (next === 'sfu') {
      if (payload.sfu) {
        try {
          await this._sfu.connect(payload.sfu);
          // Publish our local stream so peers receive our audio/video via
          // the SFU. GroupCallManager owns the local MediaStream; prefer the
          // public getter, fall back to the legacy config pull if an older
          // build doesn't expose it.
          const gm = this._config.groupManager as any;
          const localStream: MediaStream | null =
            gm?.localStream ?? gm?.config?.localStream ?? null;
          if (localStream) {
            try {
              await this._sfu.publish(localStream);
            } catch (pubErr: any) {
              console.warn('[Topology] sfu publish failed:', pubErr?.message ?? pubErr);
              this._config.onError?.(`sfu publish failed: ${pubErr?.message ?? pubErr}`);
            }
          } else {
            console.warn('[Topology] no local stream available to publish on SFU');
          }
        } catch (err: any) {
          this._config.onError?.(`sfu connect failed: ${err?.message ?? err}`);
          // Fall back to mesh if SFU cannot connect.
        }
      }
    } else if (next === 'mesh' || next === 'p2p') {
      // Nothing to connect — mesh is implicit via GroupCallManager.
      // Renegotiate all live peers so their generation tags update.
      await this._renegotiateMeshAll();
    }

    this._generation = gen;
    this._routing = next;
    this._config.onRoutingChanged?.(next, gen);

    // 3. Ack the server with our new generation.
    socketManager.emitNoAck('call_topology_ack', {
      call_id: this._config.callId,
      user_id: this._config.localUserId,
      generation: gen,
      routing: next,
    });
  }

  private async _tearDownCurrent(mode: CallRoutingMode): Promise<void> {
    if (mode === 'sfu') {
      try { await this._sfu.disconnect(); } catch {/* ignore */}
    }
    if (mode === 'mesh' || mode === 'p2p') {
      // Drop all existing PCs — they will be rebuilt by the caller or by
      // the next `addParticipant` call.
      const participants: GroupParticipant[] = this._config.groupManager.allParticipants;
      for (const p of participants) {
        if (p.connection && !p.connection.destroyed) {
          try { (p.connection as PeerConnection).destroy(); } catch { /* ignore */ }
          p.connection = null;
          p.remoteStream = null;
        }
      }
    }
  }

  private async _renegotiateMeshAll(): Promise<void> {
    const participants: GroupParticipant[] = this._config.groupManager.allParticipants;
    const tasks: Promise<any>[] = [];
    for (const p of participants) {
      const pc = p.connection;
      if (!pc || pc.destroyed) continue;
      try {
        // Prefer explicit createOffer if the PC exposes it.
        const maybe = (pc as any).createOffer?.();
        if (maybe && typeof maybe.then === 'function') {
          tasks.push(maybe);
        }
      } catch (err) {
        console.warn('[Topology] renegotiate failed for', p.peerId, err);
      }
    }
    await Promise.allSettled(tasks);
  }

  // ── Quality reporting loop ────────────────────────────────────────────

  private _startQualityLoop(): void {
    const ms = this._config.qualitySampleIntervalMs ?? DEFAULT_QUALITY_INTERVAL_MS;
    if (this._qualityTimer) clearInterval(this._qualityTimer);
    this._qualityTimer = setInterval(() => {
      this._emitQualityReport().catch((e) =>
        console.warn('[Topology] quality report failed', e),
      );
    }, ms);
  }

  private async _emitQualityReport(): Promise<void> {
    if (this._destroyed) return;
    const participants = this._config.groupManager.allParticipants;

    const samples: Array<Omit<QualitySample, 'peer_id'>> = [];
    for (const p of participants) {
      const pc = p.connection;
      if (!pc || pc.destroyed) continue;
      try {
        const stats = await this._samplePC(pc as unknown as PeerConnection);
        if (stats) samples.push(stats);
      } catch { /* tolerate single-peer stats failures */ }
    }

    if (samples.length === 0) return;

    // Server expects a single aggregated sample per report.
    const n = samples.length;
    const agg = {
      packet_loss: samples.reduce((s, x) => s + x.packet_loss, 0) / n,
      rtt_ms:      samples.reduce((s, x) => s + x.rtt_ms,      0) / n,
      jitter_ms:   samples.reduce((s, x) => s + x.jitter_ms,   0) / n,
    };

    socketManager.emitNoAck('call_quality_report', {
      call_id: this._config.callId,
      packet_loss: agg.packet_loss,
      rtt_ms: agg.rtt_ms,
      jitter_ms: agg.jitter_ms,
    });
  }

  private async _samplePC(
    pc: PeerConnection,
  ): Promise<Omit<QualitySample, 'peer_id'> | null> {
    // First try the helper the existing PeerConnection already exposes.
    try {
      const q: any = await (pc as any).getQualityMetrics?.();
      if (q && typeof q === 'object') {
        return {
          packet_loss: Math.min(Math.max(Number(q.packetsLost ?? q.packet_loss ?? 0), 0), 1),
          rtt_ms: Math.max(0, Number(q.rtt ?? q.rtt_ms ?? 0)),
          jitter_ms: Math.max(0, Number(q.jitter ?? q.jitter_ms ?? 0)),
        };
      }
    } catch { /* fall through to raw getStats */ }

    // Fall back to raw RTCPeerConnection stats.
    const rawPc: any = (pc as any)._pc ?? (pc as any).pc ?? (pc as any).peerConnection;
    if (!rawPc || typeof rawPc.getStats !== 'function') return null;

    const report: RTCStatsReport = await rawPc.getStats();
    let pktsRecv = 0, pktsLost = 0, jitter = 0, rtt = 0;
    report.forEach((s: any) => {
      if (s.type === 'inbound-rtp' && (s.kind === 'video' || s.kind === 'audio')) {
        pktsRecv += Number(s.packetsReceived ?? 0);
        pktsLost += Number(s.packetsLost ?? 0);
        if (typeof s.jitter === 'number') jitter = Math.max(jitter, s.jitter * 1000);
      }
      if (s.type === 'candidate-pair' && s.state === 'succeeded' && s.nominated && typeof s.currentRoundTripTime === 'number') {
        rtt = s.currentRoundTripTime * 1000;
      }
    });
    const total = pktsRecv + pktsLost;
    const loss = total > 0 ? pktsLost / total : 0;
    return {
      packet_loss: Math.min(Math.max(loss, 0), 1),
      rtt_ms: rtt,
      jitter_ms: jitter,
    };
  }

  // ── Signal replay ─────────────────────────────────────────────────────

  private _applyReplayedSignals(payload: any): void {
    const events = Array.isArray(payload?.events) ? payload.events : [];
    if (events.length === 0) return;
    console.log(`[Topology] applying ${events.length} replayed signals`);
    for (const ev of events) {
      try {
        const gen = Number(ev.topology_generation ?? ev.generation ?? 0);
        if (gen && gen < this._generation) continue;
        const from = ev.from ?? ev.from_user;
        const kind = ev.kind;
        const payloadObj = ev.payload;
        if (!from || !payloadObj) continue;

        // Map server-side kinds to the SignalMessage shape the client expects.
        if (kind === 'offer' || kind === 'answer' || kind === 'ice' || kind === 'ice-candidate' || kind === 'renegotiate') {
          const signal = {
            type: kind === 'ice' ? 'ice-candidate' : kind,
            ...payloadObj,
          };
          this._config.groupManager.handleSignal(from, signal as any).catch(() => {/* ignore */});
        }
      } catch (err) {
        console.warn('[Topology] replay event failed', err);
      }
    }
  }
}

// Exported stub adapter so callers can compose their own SFU binding later.
export { NoopSFUAdapter };
