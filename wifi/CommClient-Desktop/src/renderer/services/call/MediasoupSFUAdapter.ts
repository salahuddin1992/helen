/**
 * MediasoupSFUAdapter — full bridge to the Python SFU proxy + mediasoup worker.
 *
 * ── Wire protocol ──────────────────────────────────────────────────────────
 * Client ←→ Server (socket.io):
 *   emit  call_sfu_create_transport   { call_id, direction: "send"|"recv" }
 *     → ack { ok, id, ice_parameters, ice_candidates, dtls_parameters,
 *             sctp_parameters?, direction }
 *   emit  call_sfu_connect_transport  { call_id, transport_id, dtls_parameters }
 *     → ack { ok }
 *   emit  call_sfu_produce            { call_id, transport_id, kind,
 *                                       rtp_parameters, app_data? }
 *     → ack { ok, id, kind, peer_id }
 *   emit  call_sfu_consume            { call_id, transport_id, producer_id,
 *                                       rtp_capabilities }
 *     → ack { ok, id, producer_id, producer_peer_id, kind, rtp_parameters,
 *             type, producer_paused }
 *   emit  call_sfu_resume             { call_id, consumer_id }  → ack { ok }
 *   emit  call_sfu_pause              { call_id, consumer_id }  → ack { ok }
 *
 *   on    call_sfu_new_producer       { call_id, producer_id, peer_id, kind }
 *     → automatically consume on the recvTransport
 *
 * Lazy dependency: `mediasoup-client` is imported dynamically so builds do
 * not blow up if the dev workstation hasn't run `npm install` yet. Once
 * installed, the adapter is a real, fully-operational SFU client.
 */

import { socketManager } from '../socket.manager';
import type { ISFUAdapter, SFUInfo } from './TopologyCoordinator';

// ── Structural typings for mediasoup-client (optional dep) ──────────────

interface MediasoupDeviceLike {
  loaded: boolean;
  rtpCapabilities: any;
  load(opts: { routerRtpCapabilities: any }): Promise<void>;
  createSendTransport(opts: any): MediasoupTransportLike;
  createRecvTransport(opts: any): MediasoupTransportLike;
  canProduce?(kind: 'audio' | 'video'): boolean;
}

interface MediasoupTransportLike {
  id: string;
  closed: boolean;
  on(event: string, handler: (...args: any[]) => any): void;
  produce(opts: { track: MediaStreamTrack; appData?: any }): Promise<any>;
  consume(opts: any): Promise<any>;
  close(): void;
}

type ConsumerHandler = (peerId: string, stream: MediaStream) => void;
/**
 * Single-stream variant used by ``consume()``: the peer id is already the
 * map key, so the callback only needs the stream.
 */
type StreamHandler = (stream: MediaStream) => void;

// ── Adapter ──────────────────────────────────────────────────────────────

export interface MediasoupAdapterContext {
  callId: string;
  localUserId: string;
  /** Optional fan-out hook so the caller can forward remote streams to UI. */
  onRemoteStream?: ConsumerHandler;
  /** Optional hook fired when a peer's producer closes. */
  onRemoteStreamEnded?: (peerId: string, kind: 'audio' | 'video') => void;
}

export class MediasoupSFUAdapter implements ISFUAdapter {
  private _ctx: MediasoupAdapterContext;
  private _device: MediasoupDeviceLike | null = null;
  private _sendTransport: MediasoupTransportLike | null = null;
  private _recvTransport: MediasoupTransportLike | null = null;
  private _producers: Map<string, any> = new Map();        // producerId → producer
  private _consumers: Map<string, any> = new Map();        // consumerId → consumer
  /** peerId → MediaStream — one aggregated remote stream per producing peer. */
  private _remoteStreams: Map<string, MediaStream> = new Map();
  /** producerId → consumerId mapping (for close fan-out). */
  private _producerToConsumer: Map<string, string> = new Map();
  private _unsubs: Array<() => void> = [];
  private _publishedStream: MediaStream | null = null;
  /** producerIds that belong to the local screen-share publication. */
  private _screenProducerIds: Set<string> = new Set();
  private _screenStream: MediaStream | null = null;
  private _destroyed = false;
  /** Pending consume() callbacks keyed by peerId, awaiting a producer. */
  private _pendingConsumeCallbacks: Map<string, StreamHandler> = new Map();

  constructor(ctxOrLegacy?: MediasoupAdapterContext | undefined) {
    // Backward-compat: the old call site did `new MediasoupSFUAdapter()` with
    // no args. If no context is supplied, fall back to a stub ctx; connect()
    // will refuse until attach(ctx) is invoked.
    this._ctx = ctxOrLegacy ?? { callId: '', localUserId: '' };
  }

  /**
   * Attach call context after construction. Must be called before connect()
   * if the adapter was built without a context.
   */
  attach(ctx: MediasoupAdapterContext): void {
    this._ctx = ctx;
  }

  // ── ISFUAdapter surface ────────────────────────────────────────────────

  async connect(info: SFUInfo): Promise<void> {
    if (!this._ctx.callId || !this._ctx.localUserId) {
      throw new Error('MediasoupSFUAdapter.connect called without call context — use attach() first');
    }

    const Device = await this._resolveDeviceClass();

    this._device = new Device();
    if (!info.rtp_capabilities) {
      throw new Error('topology_switch payload missing rtp_capabilities');
    }
    await this._device!.load({ routerRtpCapabilities: info.rtp_capabilities });

    // Create send + recv transports. We ignore `info.transport_options` as
    // metadata only — the real transport params come from the server's
    // createWebRtcTransport response via call_sfu_create_transport.
    this._sendTransport = await this._buildTransport('send');
    this._recvTransport = await this._buildTransport('recv');

    // Listen for new-producer broadcasts from other peers.
    const offNew = socketManager.on('call_sfu_new_producer', (evt: any) => {
      if (!evt || evt.call_id !== this._ctx.callId) return;
      this._handleRemoteProducer(evt).catch((err) =>
        console.warn('[SFU] new_producer handling failed:', err),
      );
    });
    this._unsubs.push(offNew);
  }

  async publish(stream: MediaStream): Promise<void> {
    if (this._destroyed) return;
    if (!this._sendTransport) {
      throw new Error('SFU not connected');
    }
    this._publishedStream = stream;
    const canProduce = this._device?.canProduce?.bind(this._device);

    for (const track of stream.getTracks()) {
      if (canProduce && !canProduce(track.kind as 'audio' | 'video')) {
        console.warn(`[SFU] device cannot produce ${track.kind}, skipping`);
        continue;
      }
      try {
        const producer = await this._sendTransport.produce({
          track,
          appData: { user_id: this._ctx.localUserId, kind: track.kind },
        });
        this._producers.set(producer.id, producer);
        producer.on('transportclose', () => this._producers.delete(producer.id));
        producer.on('trackended', () => {
          try { producer.close(); } catch { /* ignore */ }
          this._producers.delete(producer.id);
        });
      } catch (err) {
        console.warn(`[SFU] produce(${track.kind}) failed:`, err);
      }
    }
  }

  /**
   * Publish a screen-share stream as a separate producer (tagged
   * ``appData.mediaType === 'screen'``) so remote peers can render it in a
   * dedicated tile alongside the camera. Idempotent: calling twice without
   * ``unpublishScreenShare()`` replaces the prior stream.
   *
   * Downstream consumers read ``appData.mediaType`` to distinguish screen
   * from camera; the Python SFU proxy forwards ``app_data`` verbatim.
   */
  async publishScreenShare(stream: MediaStream): Promise<void> {
    if (this._destroyed) return;
    if (!this._sendTransport) throw new Error('SFU not connected');

    // If a screen share is already live, tear it down first — callers may
    // re-invoke when swapping source (entire screen → window, etc.).
    if (this._screenProducerIds.size > 0) {
      await this.unpublishScreenShare();
    }

    this._screenStream = stream;
    const canProduce = this._device?.canProduce?.bind(this._device);

    for (const track of stream.getTracks()) {
      if (canProduce && !canProduce(track.kind as 'audio' | 'video')) {
        console.warn(`[SFU] device cannot produce screen ${track.kind}, skipping`);
        continue;
      }
      try {
        const producer = await this._sendTransport.produce({
          track,
          appData: {
            user_id: this._ctx.localUserId,
            kind: track.kind,
            mediaType: 'screen',
          },
        });
        this._producers.set(producer.id, producer);
        this._screenProducerIds.add(producer.id);
        producer.on('transportclose', () => {
          this._producers.delete(producer.id);
          this._screenProducerIds.delete(producer.id);
        });
        producer.on('trackended', () => {
          try { producer.close(); } catch { /* ignore */ }
          this._producers.delete(producer.id);
          this._screenProducerIds.delete(producer.id);
        });
      } catch (err) {
        console.warn(`[SFU] produce(screen ${track.kind}) failed:`, err);
      }
    }
  }

  /** Stop and close all local screen-share producers. */
  async unpublishScreenShare(): Promise<void> {
    if (this._screenProducerIds.size === 0) return;
    for (const producerId of Array.from(this._screenProducerIds)) {
      const producer = this._producers.get(producerId);
      try { producer?.close(); } catch { /* ignore */ }
      this._producers.delete(producerId);
      this._screenProducerIds.delete(producerId);
      // Best-effort server notification so consumers can tear down their tile.
      socketManager
        .emit('call_sfu_producer_close', {
          call_id: this._ctx.callId,
          producer_id: producerId,
        })
        .catch(() => { /* server cleans up on transport close anyway */ });
    }
    this._screenStream = null;
  }

  /**
   * Best-effort: surface remote stream for ``peerId`` through ``onStream``.
   * If the producer already exists, we fire synchronously; otherwise we
   * stash the callback and fire when a ``call_sfu_new_producer`` arrives
   * (or pull existing streams from our cache).
   */
  async consume(peerId: string, onStream: (s: MediaStream) => void): Promise<void> {
    if (this._destroyed) return;

    const cached = this._remoteStreams.get(peerId);
    if (cached && cached.getTracks().length > 0) {
      try { onStream(cached); } catch { /* ignore */ }
      return;
    }

    // Otherwise stash the callback — the `call_sfu_new_producer` listener
    // will invoke it once the first producer for this peer arrives.
    this._pendingConsumeCallbacks.set(peerId, onStream);
  }

  async disconnect(): Promise<void> {
    this._destroyed = true;

    for (const u of this._unsubs) {
      try { u(); } catch { /* ignore */ }
    }
    this._unsubs.length = 0;

    for (const [, p] of this._producers) {
      try { p.close(); } catch { /* ignore */ }
    }
    this._producers.clear();

    for (const [, c] of this._consumers) {
      try { c.close(); } catch { /* ignore */ }
    }
    this._consumers.clear();

    try { this._sendTransport?.close(); } catch { /* ignore */ }
    try { this._recvTransport?.close(); } catch { /* ignore */ }
    this._sendTransport = null;
    this._recvTransport = null;
    this._device = null;

    this._remoteStreams.clear();
    this._producerToConsumer.clear();
    this._pendingConsumeCallbacks.clear();
    this._screenProducerIds.clear();
    this._screenStream = null;

    // Best-effort notify the server so it can free transports. The Python
    // side also cleans up on socket disconnect and on call leave, so this
    // is idempotent.
    try {
      // Leave endpoint doesn't exist at the socket layer yet — rely on
      // call_leave_group / call_hangup to trigger peer_leave on the worker
      // via the Python bridge. No-op here.
    } catch { /* ignore */ }
  }

  /**
   * Pause or resume every local producer of a given kind. Fires both the
   * local mediasoup-client call (stops emitting RTP) AND the server's
   * call_sfu_producer_(pause|resume) handler so consumers get notified to
   * grey out the tile and save downlink bandwidth.
   *
   * Returns the number of producers affected (0 if we never produced that
   * kind — e.g. audio-only call pausing video).
   */
  async setProducerPaused(
    kind: 'audio' | 'video',
    paused: boolean,
  ): Promise<number> {
    if (this._destroyed) return 0;
    let affected = 0;
    for (const [producerId, producer] of this._producers) {
      if (producer?.kind !== kind) continue;
      try {
        if (paused) {
          producer.pause?.();
        } else {
          producer.resume?.();
        }
      } catch (err) {
        console.warn(`[SFU] local producer ${paused ? 'pause' : 'resume'} failed:`, err);
      }
      // Fire-and-forget server ack: the UI doesn't need to block on this.
      const event = paused ? 'call_sfu_producer_pause' : 'call_sfu_producer_resume';
      socketManager
        .emit(event, { call_id: this._ctx.callId, producer_id: producerId })
        .catch((err) =>
          console.warn(`[SFU] server ${event} failed:`, err),
        );
      affected++;
    }
    return affected;
  }

  /**
   * Swap the underlying track of the non-screen producer matching ``kind``.
   * This uses mediasoup-client's ``producer.replaceTrack`` so there is no
   * SDP renegotiation — remote peers simply start decoding frames from the
   * new source. Used by CallEngine when the user picks a different camera
   * or mic mid-call (e.g. switching to the paired phone as the source).
   * Returns true on success, false if no matching producer exists or the
   * swap failed.
   */
  async replaceTrack(
    kind: 'audio' | 'video',
    newTrack: MediaStreamTrack,
  ): Promise<boolean> {
    if (this._destroyed) return false;
    for (const [producerId, producer] of this._producers) {
      if (producer?.kind !== kind) continue;
      // Don't clobber a live screen share — those producers are tagged and
      // should be swapped via their own publishScreenShare() code path.
      if (this._screenProducerIds.has(producerId)) continue;
      try {
        await producer.replaceTrack({ track: newTrack });
        return true;
      } catch (err) {
        console.warn(`[SFU] replaceTrack(${kind}) failed:`, err);
      }
    }
    return false;
  }

  /** Runtime diagnostics snapshot. */
  getDiagnostics(): Record<string, any> {
    return {
      deviceLoaded: !!this._device?.loaded,
      sendTransport: this._sendTransport?.id ?? null,
      recvTransport: this._recvTransport?.id ?? null,
      producers: [...this._producers.keys()],
      consumers: [...this._consumers.keys()],
      remotePeers: [...this._remoteStreams.keys()],
    };
  }

  // ── Internals ──────────────────────────────────────────────────────────

  private async _resolveDeviceClass(): Promise<new () => MediasoupDeviceLike> {
    try {
      // Obfuscate the specifier so Vite's dev-server import-analysis can't
      // statically resolve the path — the package is an optional runtime
      // peer and may not be installed. Rollup's `external` config only
      // takes effect at build time, so the dev server needs this trick.
      // @ts-ignore — optional peer
      const specifier = ['mediasoup', 'client'].join('-');
      const lib: any = await import(/* @vite-ignore */ specifier);
      const Device = lib.Device ?? lib.default?.Device;
      if (!Device) throw new Error('mediasoup-client: Device class not found on module');
      return Device;
    } catch (err: any) {
      console.warn('[SFU] mediasoup-client unavailable — cannot enter SFU mode:', err?.message ?? err);
      throw err;
    }
  }

  private async _buildTransport(direction: 'send' | 'recv'): Promise<MediasoupTransportLike> {
    const params = await socketManager.emit('call_sfu_create_transport', {
      call_id: this._ctx.callId,
      direction,
    });
    if (!params || params.ok === false) {
      throw new Error(`create_transport(${direction}) failed: ${params?.error ?? 'unknown'}`);
    }

    const transportOpts = {
      id: params.id,
      iceParameters: params.ice_parameters,
      iceCandidates: params.ice_candidates,
      dtlsParameters: params.dtls_parameters,
      sctpParameters: params.sctp_parameters,
      iceServers: [],
    };

    const transport =
      direction === 'send'
        ? this._device!.createSendTransport(transportOpts)
        : this._device!.createRecvTransport(transportOpts);

    // ── DTLS connect handler ────────────────────────────────────────────
    transport.on(
      'connect',
      ({ dtlsParameters }: any, callback: () => void, errback: (e: Error) => void) => {
        socketManager
          .emit('call_sfu_connect_transport', {
            call_id: this._ctx.callId,
            transport_id: transport.id,
            dtls_parameters: dtlsParameters,
          })
          .then((res) => {
            if (res?.ok === false) throw new Error(res.error ?? 'connect failed');
            callback();
          })
          .catch((err) => errback(err instanceof Error ? err : new Error(String(err))));
      },
    );

    // ── Produce handler (send transport only) ───────────────────────────
    if (direction === 'send') {
      transport.on(
        'produce',
        (
          { kind, rtpParameters, appData }: any,
          callback: (arg: { id: string }) => void,
          errback: (e: Error) => void,
        ) => {
          socketManager
            .emit('call_sfu_produce', {
              call_id: this._ctx.callId,
              transport_id: transport.id,
              kind,
              rtp_parameters: rtpParameters,
              app_data: appData,
            })
            .then((res) => {
              if (!res || res.ok === false) throw new Error(res?.error ?? 'produce failed');
              callback({ id: res.id });
            })
            .catch((err) => errback(err instanceof Error ? err : new Error(String(err))));
        },
      );
    }

    transport.on('connectionstatechange', (state: string) => {
      console.log(`[SFU] transport(${direction}:${transport.id}) state=${state}`);
      if (state === 'failed' || state === 'disconnected') {
        // Optional: drive reconnection logic via TopologyCoordinator.
      }
    });

    return transport;
  }

  private async _handleRemoteProducer(evt: {
    call_id: string;
    producer_id: string;
    peer_id: string;
    kind: 'audio' | 'video';
  }): Promise<void> {
    if (this._destroyed) return;
    if (!this._recvTransport || !this._device) return;
    if (evt.peer_id === this._ctx.localUserId) return; // ignore self-echo

    const res = await socketManager.emit('call_sfu_consume', {
      call_id: this._ctx.callId,
      transport_id: this._recvTransport.id,
      producer_id: evt.producer_id,
      rtp_capabilities: this._device.rtpCapabilities,
    });
    if (!res || res.ok === false) {
      console.warn('[SFU] consume() failed:', res?.error ?? 'unknown');
      return;
    }

    const consumer = await this._recvTransport.consume({
      id: res.id,
      producerId: evt.producer_id,
      kind: res.kind,
      rtpParameters: res.rtp_parameters,
      appData: { peer_id: evt.peer_id, producer_id: evt.producer_id },
    });

    this._consumers.set(consumer.id, consumer);
    this._producerToConsumer.set(evt.producer_id, consumer.id);

    consumer.on('transportclose', () => {
      this._teardownConsumer(consumer.id, evt.peer_id, res.kind);
    });
    consumer.on('producerclose', () => {
      this._teardownConsumer(consumer.id, evt.peer_id, res.kind);
    });

    // Attach track to per-peer aggregated stream.
    let stream = this._remoteStreams.get(evt.peer_id);
    if (!stream) {
      stream = new MediaStream();
      this._remoteStreams.set(evt.peer_id, stream);
    }
    stream.addTrack(consumer.track);

    // Resume on the server (created paused for race safety).
    try {
      await socketManager.emit('call_sfu_resume', {
        call_id: this._ctx.callId,
        consumer_id: consumer.id,
      });
    } catch (err) {
      console.warn('[SFU] resume consumer failed:', err);
    }

    // Fan-out to callers waiting on this peer + the ctx-level hook.
    const pending = this._pendingConsumeCallbacks.get(evt.peer_id);
    if (pending) {
      try { pending(stream); } catch { /* ignore */ }
      this._pendingConsumeCallbacks.delete(evt.peer_id);
    }
    try { this._ctx.onRemoteStream?.(evt.peer_id, stream); } catch { /* ignore */ }
  }

  private _teardownConsumer(consumerId: string, peerId: string, kind: 'audio' | 'video'): void {
    const c = this._consumers.get(consumerId);
    if (c) {
      try { c.close(); } catch { /* ignore */ }
      this._consumers.delete(consumerId);
    }
    // Remove reverse mapping
    for (const [pid, cid] of this._producerToConsumer) {
      if (cid === consumerId) {
        this._producerToConsumer.delete(pid);
        break;
      }
    }
    // Drop the track from the per-peer stream; leave the stream around in
    // case another track (e.g. audio ↔ video) keeps the call alive.
    const stream = this._remoteStreams.get(peerId);
    if (stream && c?.track) {
      try { stream.removeTrack(c.track); } catch { /* ignore */ }
      if (stream.getTracks().length === 0) {
        this._remoteStreams.delete(peerId);
      }
    }
    try { this._ctx.onRemoteStreamEnded?.(peerId, kind); } catch { /* ignore */ }
  }
}
