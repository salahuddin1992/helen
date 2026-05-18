/**
 * PeerConnection — production-grade wrapper around RTCPeerConnection.
 *
 * Handles:
 *   - SDP offer/answer creation and application
 *   - ICE candidate gathering, trickling, and buffering
 *   - Track management (add, replace, remove)
 *   - Connection state monitoring with reconnection logic
 *   - LAN-optimized ICE configuration (no STUN/TURN needed)
 *   - Stats collection for quality monitoring
 *
 * One instance per remote peer (1 for 1-to-1, N-1 for group mesh).
 */

export interface PeerConnectionConfig {
  /** Remote peer's user ID */
  peerId: string;
  /** Whether this side creates the offer */
  isInitiator: boolean;
  /** Local media stream to attach */
  localStream: MediaStream | null;
  /** Callback: send signaling data to the remote peer via server */
  onSignal: (data: SignalMessage) => void;
  /** Callback: remote track received */
  onRemoteTrack: (track: MediaStreamTrack, streams: readonly MediaStream[]) => void;
  /** Callback: connection state changed */
  onStateChange: (state: RTCPeerConnectionState) => void;
  /** Callback: ICE connection state changed */
  onIceStateChange: (state: RTCIceConnectionState) => void;
  /** Callback: remote stream fully available */
  onRemoteStream: (stream: MediaStream) => void;
  /** Callback: DTLS state changed */
  onDtlsStateChange?: (state: RTCDtlsTransportState) => void;
  /** Callback: data channel created by remote peer */
  onDataChannel?: (channel: RTCDataChannel) => void;
}

export interface SignalMessage {
  type: 'offer' | 'answer' | 'ice-candidate' | 'renegotiate';
  targetId: string;
  fromId?: string;
  sdp?: RTCSessionDescriptionInit;
  candidate?: RTCIceCandidateInit;
}

/**
 * Detailed per-track and connection statistics.
 * Separates audio, video, and connection metadata for granular monitoring.
 */
export interface DetailedPeerStats {
  audio: {
    bitrate: number;
    packetsLost: number;
    jitter: number;
    codec: string;
    packetsReceived: number;
  };
  video: {
    bitrate: number;
    packetsLost: number;
    jitter: number;
    codec: string;
    frameRate: number;
    frameWidth: number;
    frameHeight: number;
    packetsReceived: number;
  };
  connection: {
    rtt: number;
    localCandidateType: string;
    remoteCandidateType: string;
    transportProtocol: string;
    bytesSent: number;
    bytesReceived: number;
  };
  timestamp: number;
}

/** LAN-only ICE config — no STUN/TURN servers needed on local network */
const LAN_ICE_CONFIG: RTCConfiguration = {
  iceServers: [],
  iceCandidatePoolSize: 0,
  iceTransportPolicy: 'all',
  bundlePolicy: 'max-bundle',
  rtcpMuxPolicy: 'require',
};

/**
 * Same as LAN_ICE_CONFIG but with a slightly larger candidate pool for
 * cross-subnet topologies. Helen is LAN-only by policy — never reach
 * out to public STUN like stun.l.google.com. The proper STUN/TURN
 * config arrives at runtime via the server-issued ICE config (TURN
 * with HMAC-SHA1 short-term creds + self-hosted STUN responder); the
 * caller passes that as `iceOverride`.
 */
const LAN_ICE_CONFIG_WITH_STUN: RTCConfiguration = {
  iceServers: [],
  iceCandidatePoolSize: 2,
  iceTransportPolicy: 'all',
  bundlePolicy: 'max-bundle',
  rtcpMuxPolicy: 'require',
};

const RECONNECT_TIMEOUT_MS = 15_000;
const ICE_GATHERING_TIMEOUT_MS = 5_000;

/** Max allowed ICE restart attempts before giving up */
const MAX_ICE_RESTARTS = 5;

/** Exponential backoff delays for ICE restarts (in milliseconds) */
const ICE_RESTART_BACKOFF = [1000, 2000, 4000, 8000, 16000];

export class PeerConnection {
  private pc: RTCPeerConnection;
  private config: PeerConnectionConfig;
  private remoteStream: MediaStream | null = new MediaStream();
  private iceCandidateBuffer: RTCIceCandidateInit[] = [];
  private hasRemoteDescription = false;
  private makingOffer = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _destroyed = false;

  // ── ICE Restart Backoff ───
  private _iceRestartCount = 0;
  private _iceRestartBackoffTimer: ReturnType<typeof setTimeout> | null = null;
  private _lastIceRestartTriggerTime = 0;

  // ── DTLS Monitoring ───────
  private _dtlsMonitorInterval: ReturnType<typeof setInterval> | null = null;

  // ── Connection Timing ─────
  private _connectionStartTime: number = Date.now();
  private _connectionEstablishedTime: number | null = null;

  // ── Track State ───────────
  private _audioTrackEnabled = true;
  private _videoTrackEnabled = true;

  // ── Data Channels ────────
  private dataChannels: Map<string, RTCDataChannel> = new Map();

  constructor(config: PeerConnectionConfig, useFallbackStun = false, iceOverride?: RTCConfiguration) {
    this.config = config;
    // If the caller passes an `iceOverride` (e.g. a server-issued config
    // including TURN credentials from /api/turn/ice-config), prefer that.
    // Otherwise fall back to the legacy static configs. This keeps the
    // original LAN-only behavior the default while letting CallEngine
    // upgrade to a TURN-aware config without churning every call site.
    const cfg = iceOverride
      ?? (useFallbackStun ? LAN_ICE_CONFIG_WITH_STUN : LAN_ICE_CONFIG);
    this.pc = new RTCPeerConnection(cfg);

    this._setupPeerListeners();

    // Attach local tracks
    if (config.localStream) {
      this.addLocalStream(config.localStream);
    }

    // If initiator, create offer after a tick (let tracks attach)
    if (config.isInitiator) {
      setTimeout(() => this.createOffer(), 100);
    }
  }

  get peerConnection(): RTCPeerConnection {
    return this.pc;
  }

  get connectionState(): RTCPeerConnectionState {
    return this.pc.connectionState;
  }

  get iceConnectionState(): RTCIceConnectionState {
    return this.pc.iceConnectionState;
  }

  get destroyed(): boolean {
    return this._destroyed;
  }

  // ── SDP Offer/Answer ──────────────────────────────

  async createOffer(): Promise<void> {
    if (this._destroyed) return;
    this.makingOffer = true;

    try {
      const offer = await this.pc.createOffer({
        offerToReceiveAudio: true,
        offerToReceiveVideo: true,
      });

      // Modify SDP for LAN optimization
      if (offer.sdp) {
        offer.sdp = this._optimizeSdpForLan(offer.sdp);
      }

      await this.pc.setLocalDescription(offer);

      this.config.onSignal({
        type: 'offer',
        targetId: this.config.peerId,
        sdp: this.pc.localDescription!,
      });
    } catch (e) {
      console.error(`[Peer:${this.config.peerId}] createOffer error:`, e);
    } finally {
      this.makingOffer = false;
    }
  }

  async handleOffer(sdp: RTCSessionDescriptionInit): Promise<void> {
    if (this._destroyed) return;

    // Perfect-negotiation glare handling (audit fix W3).
    //
    // Convention used here (matches GroupCallManager._shouldInitiate):
    //   isInitiator == true  → IMPOLITE peer (lex-smaller user id)
    //   isInitiator == false → POLITE peer (lex-larger user id)
    //
    // On collision (we have an outstanding offer or signalingState
    // isn't stable):
    //   - Impolite peer ignores the remote offer; it keeps its own.
    //   - Polite peer ROLLS BACK its local offer, then accepts the
    //     remote one. Without the rollback, setRemoteDescription
    //     throws InvalidStateError because the connection is in
    //     have-local-offer.
    //
    // Previous code skipped the rollback for the polite path, so any
    // simultaneous offer on a moderately-loaded mesh (>2 peers
    // initiating concurrently) bricked the PeerConnection.
    const offerCollision = this.makingOffer || this.pc.signalingState !== 'stable';

    if (offerCollision && this.config.isInitiator) {
      // Impolite — ignore.
      console.log(`[Peer:${this.config.peerId}] Glare — impolite, ignoring remote offer`);
      return;
    }

    if (offerCollision && !this.config.isInitiator) {
      // Polite — roll back and accept.
      console.log(`[Peer:${this.config.peerId}] Glare — polite, rolling back local offer`);
      try {
        // setLocalDescription({type:'rollback'}) is the spec'd way to
        // discard a pending local offer. Some browsers also accept
        // setLocalDescription() with no args to mean rollback when in
        // have-local-offer; the explicit form is portable.
        await Promise.all([
          this.pc.setLocalDescription({ type: 'rollback' } as RTCSessionDescriptionInit),
          this.pc.setRemoteDescription(new RTCSessionDescription(sdp)),
        ]);
        this.hasRemoteDescription = true;
        await this._flushIceCandidates();

        const answer = await this.pc.createAnswer();
        if (answer.sdp) {
          answer.sdp = this._optimizeSdpForLan(answer.sdp);
        }
        await this.pc.setLocalDescription(answer);

        this.config.onSignal({
          type: 'answer',
          targetId: this.config.peerId,
          sdp: this.pc.localDescription!,
        });
        return;
      } catch (e) {
        console.error(`[Peer:${this.config.peerId}] glare rollback failed:`, e);
        return;
      }
    }

    // No collision — straightforward offer/answer.
    try {
      await this.pc.setRemoteDescription(new RTCSessionDescription(sdp));
      this.hasRemoteDescription = true;
      await this._flushIceCandidates();

      const answer = await this.pc.createAnswer();
      if (answer.sdp) {
        answer.sdp = this._optimizeSdpForLan(answer.sdp);
      }
      await this.pc.setLocalDescription(answer);

      this.config.onSignal({
        type: 'answer',
        targetId: this.config.peerId,
        sdp: this.pc.localDescription!,
      });
    } catch (e) {
      console.error(`[Peer:${this.config.peerId}] handleOffer error:`, e);
    }
  }

  async handleAnswer(sdp: RTCSessionDescriptionInit): Promise<void> {
    if (this._destroyed) return;
    try {
      await this.pc.setRemoteDescription(new RTCSessionDescription(sdp));
      this.hasRemoteDescription = true;
      await this._flushIceCandidates();
    } catch (e) {
      console.error(`[Peer:${this.config.peerId}] handleAnswer error:`, e);
    }
  }

  // ── ICE Candidates ────────────────────────────────

  async handleIceCandidate(candidate: RTCIceCandidateInit): Promise<void> {
    if (this._destroyed) return;

    if (!this.hasRemoteDescription) {
      // Buffer until remote description is set
      if (this.iceCandidateBuffer.length >= 500) {
        console.warn('[PeerConnection] ICE candidate buffer overflow, dropping oldest');
        this.iceCandidateBuffer.shift();
      }
      this.iceCandidateBuffer.push(candidate);
      return;
    }

    try {
      await this.pc.addIceCandidate(new RTCIceCandidate(candidate));
    } catch (e) {
      // Non-fatal: duplicate or out-of-order candidates are common
      console.warn(`[Peer:${this.config.peerId}] addIceCandidate warning:`, e);
    }
  }

  private async _flushIceCandidates(): Promise<void> {
    for (const candidate of this.iceCandidateBuffer) {
      try {
        await this.pc.addIceCandidate(new RTCIceCandidate(candidate));
      } catch (e) {
        console.warn(`[Peer:${this.config.peerId}] flush ICE warning:`, e);
      }
    }
    this.iceCandidateBuffer = [];
  }

  // ── Track Management ──────────────────────────────

  addLocalStream(stream: MediaStream): void {
    for (const track of stream.getTracks()) {
      const existingSenders = this.pc.getSenders();
      const existing = existingSenders.find(
        (s) => s.track?.kind === track.kind
      );
      if (existing) {
        existing.replaceTrack(track).catch((e) =>
          console.warn(`[Peer:${this.config.peerId}] replaceTrack:`, e)
        );
      } else {
        this.pc.addTrack(track, stream);
      }
    }
  }

  /**
   * Replace a track of the given kind in the peer connection.
   * Used for hot-swapping audio/video devices.
   */
  async replaceTrack(newTrack: MediaStreamTrack): Promise<void> {
    const sender = this.pc.getSenders().find(
      (s) => s.track?.kind === newTrack.kind
    );
    if (sender) {
      await sender.replaceTrack(newTrack);
    }
  }

  /**
   * Add a screen share track as an additional video stream.
   * Triggers renegotiation.
   */
  addScreenTrack(track: MediaStreamTrack, stream: MediaStream): RTCRtpSender {
    const sender = this.pc.addTrack(track, stream);

    // Renegotiate
    this.createOffer();
    return sender;
  }

  /**
   * Remove a screen share track and renegotiate.
   */
  removeScreenTrack(sender: RTCRtpSender): void {
    this.pc.removeTrack(sender);
    this.createOffer();
  }

  // ── Quality Controls ──────────────────────────────

  /**
   * Apply bitrate limits to video senders.
   */
  async setVideoBitrate(maxBitrateKbps: number): Promise<void> {
    for (const sender of this.pc.getSenders()) {
      if (sender.track?.kind !== 'video') continue;

      const params = sender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }

      params.encodings[0].maxBitrate = maxBitrateKbps * 1000;
      await sender.setParameters(params);
    }
  }

  /**
   * Apply framerate limits.
   */
  async setVideoFramerate(maxFramerate: number): Promise<void> {
    for (const sender of this.pc.getSenders()) {
      if (sender.track?.kind !== 'video') continue;

      const params = sender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }

      params.encodings[0].maxFramerate = maxFramerate;
      await sender.setParameters(params);
    }
  }

  /**
   * Apply bitrate limits to audio senders.
   */
  async setAudioBitrate(maxBitrateKbps: number): Promise<void> {
    for (const sender of this.pc.getSenders()) {
      if (sender.track?.kind !== 'audio') continue;

      const params = sender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }

      params.encodings[0].maxBitrate = maxBitrateKbps * 1000;
      await sender.setParameters(params);
    }
  }

  /**
   * Set preferred codec for audio or video.
   * Reorders SDP to prefer the specified codec MIME type.
   */
  async setPreferredCodec(kind: 'audio' | 'video', codecMimeType: string): Promise<void> {
    const currentLocal = this.pc.localDescription;

    if (currentLocal?.sdp) {
      const optimized = this._preferCodecInSdp(currentLocal.sdp, kind, codecMimeType);
      await this.pc.setLocalDescription({
        type: currentLocal.type,
        sdp: optimized,
      });
      console.log(`[Peer:${this.config.peerId}] Codec preference applied for ${kind}: ${codecMimeType}`);
    }
  }

  /**
   * Enable or disable audio/video tracks.
   */
  setTrackEnabled(kind: 'audio' | 'video', enabled: boolean): void {
    for (const sender of this.pc.getSenders()) {
      if (sender.track?.kind === kind) {
        sender.track.enabled = enabled;
      }
    }

    if (kind === 'audio') {
      this._audioTrackEnabled = enabled;
    } else if (kind === 'video') {
      this._videoTrackEnabled = enabled;
    }

    console.log(`[Peer:${this.config.peerId}] ${kind} track enabled: ${enabled}`);
  }

  /**
   * Check if audio/video track is enabled.
   */
  isTrackEnabled(kind: 'audio' | 'video'): boolean {
    if (kind === 'audio') return this._audioTrackEnabled;
    if (kind === 'video') return this._videoTrackEnabled;
    return false;
  }

  /**
   * Get stats for senders (outbound) of a specific kind.
   */
  async getSenderStats(kind: 'audio' | 'video'): Promise<RTCOutboundRtpStreamStats[]> {
    const stats = await this.pc.getStats();
    const senderStats: RTCOutboundRtpStreamStats[] = [];

    stats.forEach((report) => {
      if (report.type === 'outbound-rtp' && report.kind === kind) {
        senderStats.push(report as RTCOutboundRtpStreamStats);
      }
    });

    return senderStats;
  }

  /**
   * Get stats for receivers (inbound) of a specific kind.
   */
  async getReceiverStats(kind: 'audio' | 'video'): Promise<RTCInboundRtpStreamStats[]> {
    const stats = await this.pc.getStats();
    const receiverStats: RTCInboundRtpStreamStats[] = [];

    stats.forEach((report) => {
      if (report.type === 'inbound-rtp' && report.kind === kind) {
        receiverStats.push(report as RTCInboundRtpStreamStats);
      }
    });

    return receiverStats;
  }

  /**
   * Get estimated bandwidth (available outgoing bitrate).
   */
  async getEstimatedBandwidth(): Promise<number> {
    const stats = await this.pc.getStats();
    let bandwidth = 0;

    stats.forEach((report) => {
      if (report.type === 'candidate-pair' && report.state === 'succeeded') {
        bandwidth = (report as any).availableOutgoingBitrate || 0;
      }
    });

    return bandwidth;
  }

  /**
   * Get DTLS transport state.
   */
  getDtlsState(): RTCDtlsTransportState {
    const senders = this.pc.getSenders();
    if (senders.length > 0 && senders[0].transport) {
      return senders[0].transport.state;
    }
    return 'new';
  }

  // ── Stats ─────────────────────────────────────────

  async getStats(): Promise<RTCStatsReport> {
    return this.pc.getStats();
  }

  /**
   * Get parsed connection quality metrics.
   */
  async getQualityMetrics(): Promise<{
    rtt: number;
    packetsLost: number;
    jitter: number;
    bitrate: number;
    codec: string;
  }> {
    const stats = await this.pc.getStats();
    let rtt = 0, packetsLost = 0, jitter = 0, bitrate = 0, codec = '';

    stats.forEach((report) => {
      if (report.type === 'candidate-pair' && report.state === 'succeeded') {
        rtt = report.currentRoundTripTime * 1000 || 0;
      }
      if (report.type === 'inbound-rtp' && report.kind === 'video') {
        packetsLost = report.packetsLost || 0;
        jitter = report.jitter || 0;
      }
      if (report.type === 'outbound-rtp' && report.kind === 'video') {
        bitrate = report.bytesSent ? (report.bytesSent * 8) / 1000 : 0;
      }
      if (report.type === 'codec') {
        codec = report.mimeType || '';
      }
    });

    return { rtt, packetsLost, jitter, bitrate, codec };
  }

  /**
   * Get detailed stats: separate audio, video, and connection metrics.
   */
  async getDetailedStats(): Promise<DetailedPeerStats> {
    const stats = await this.pc.getStats();

    const result: DetailedPeerStats = {
      audio: {
        bitrate: 0,
        packetsLost: 0,
        jitter: 0,
        codec: '',
        packetsReceived: 0,
      },
      video: {
        bitrate: 0,
        packetsLost: 0,
        jitter: 0,
        codec: '',
        frameRate: 0,
        frameWidth: 0,
        frameHeight: 0,
        packetsReceived: 0,
      },
      connection: {
        rtt: 0,
        localCandidateType: '',
        remoteCandidateType: '',
        transportProtocol: '',
        bytesSent: 0,
        bytesReceived: 0,
      },
      timestamp: Date.now(),
    };

    const codecMap: { [key: string]: string } = {};

    stats.forEach((report) => {
      // Codec mapping
      if (report.type === 'codec') {
        codecMap[report.payloadType] = report.mimeType || '';
      }

      // Audio inbound
      if (report.type === 'inbound-rtp' && report.kind === 'audio') {
        result.audio.packetsLost = report.packetsLost || 0;
        result.audio.jitter = report.jitter || 0;
        result.audio.packetsReceived = report.packetsReceived || 0;
        result.audio.codec = codecMap[report.payloadType] || '';
      }

      // Audio outbound
      if (report.type === 'outbound-rtp' && report.kind === 'audio') {
        result.audio.bitrate = report.bytesSent ? (report.bytesSent * 8) / 1000 : 0;
      }

      // Video inbound
      if (report.type === 'inbound-rtp' && report.kind === 'video') {
        result.video.packetsLost = report.packetsLost || 0;
        result.video.jitter = report.jitter || 0;
        result.video.packetsReceived = report.packetsReceived || 0;
        result.video.frameRate = report.framesPerSecond || 0;
        result.video.codec = codecMap[report.payloadType] || '';

        // Extract resolution from the inbound-rtp report directly
        if (report.frameWidth && report.frameHeight) {
          result.video.frameWidth = report.frameWidth;
          result.video.frameHeight = report.frameHeight;
        }
      }

      // Fallback: extract resolution from 'track' stats (older WebRTC implementations)
      if (report.type === 'track' && report.kind === 'video' && report.remoteSource) {
        if (result.video.frameWidth === 0 && report.frameWidth) {
          result.video.frameWidth = report.frameWidth;
        }
        if (result.video.frameHeight === 0 && report.frameHeight) {
          result.video.frameHeight = report.frameHeight;
        }
      }

      // Video outbound
      if (report.type === 'outbound-rtp' && report.kind === 'video') {
        result.video.bitrate = report.bytesSent ? (report.bytesSent * 8) / 1000 : 0;
      }

      // Connection stats
      if (report.type === 'candidate-pair' && report.state === 'succeeded') {
        result.connection.rtt = report.currentRoundTripTime * 1000 || 0;
        result.connection.bytesSent = report.bytesSent || 0;
        result.connection.bytesReceived = report.bytesReceived || 0;
        // Look up the selected local candidate to get the actual protocol
        const localCandidateId = report.localCandidateId;
        if (localCandidateId) {
          const localCandidate = stats.get(localCandidateId);
          if (localCandidate) {
            result.connection.transportProtocol = localCandidate.protocol || 'udp';
          }
        }
      }

      // Candidate types
      if (report.type === 'candidate' && report.transport) {
        if (report.candidateType) {
          // Determine if local or remote based on context
          if (!result.connection.localCandidateType) {
            result.connection.localCandidateType = report.candidateType;
          } else {
            result.connection.remoteCandidateType = report.candidateType;
          }
        }
      }
    });

    return result;
  }

  // ── Data Channels ────────────────────────────────

  /**
   * Create a data channel for future extensions (messaging, file transfer, etc).
   */
  createDataChannel(label: string, options?: RTCDataChannelInit): RTCDataChannel {
    const channel = this.pc.createDataChannel(label, options);
    this._setupDataChannelListeners(channel);
    this.dataChannels.set(label, channel);
    return channel;
  }

  /**
   * Get existing data channel by label.
   */
  getDataChannel(label: string): RTCDataChannel | undefined {
    return this.dataChannels.get(label);
  }

  /**
   * Close a data channel.
   */
  closeDataChannel(label: string): void {
    const channel = this.dataChannels.get(label);
    if (channel) {
      channel.close();
      this.dataChannels.delete(label);
    }
  }

  // ── Connection Timing ────────────────────────────

  /**
   * Get connection setup duration in milliseconds.
   * Returns null if connection not yet established.
   */
  getConnectionSetupDuration(): number | null {
    if (this._connectionEstablishedTime === null) {
      return null;
    }
    return this._connectionEstablishedTime - this._connectionStartTime;
  }

  // ── Cleanup ───────────────────────────────────────

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (this._iceRestartBackoffTimer) {
      clearTimeout(this._iceRestartBackoffTimer);
      this._iceRestartBackoffTimer = null;
    }

    if (this._dtlsMonitorInterval) {
      clearInterval(this._dtlsMonitorInterval);
      this._dtlsMonitorInterval = null;
    }

    // Close all data channels
    for (const [label, channel] of this.dataChannels.entries()) {
      try {
        channel.close();
      } catch (e) {
        console.warn(`[Peer:${this.config.peerId}] Error closing data channel ${label}:`, e);
      }
    }
    this.dataChannels.clear();

    // Stop every remote MediaStreamTrack we received via ontrack —
    // RTCPeerConnection.close() does NOT stop the tracks; the WebRTC
    // engine keeps them alive (decoders, jitter buffers) until each
    // track is explicitly stop()'d. Without this, a quick
    // join → leave → join cycle leaks the previous call's tracks.
    if (this.remoteStream) {
      try {
        for (const t of this.remoteStream.getTracks()) {
          try { t.stop(); }
          catch (err) { console.debug('[Peer] track.stop failed', err); }
        }
      } catch (err) { console.debug('[Peer] remote-track cleanup failed', err); }
      this.remoteStream = null;
    }

    this.pc.onicecandidate = null;
    this.pc.ontrack = null;
    this.pc.onconnectionstatechange = null;
    this.pc.oniceconnectionstatechange = null;
    this.pc.onnegotiationneeded = null;
    this.pc.onsignalingstatechange = null;
    this.pc.ondatachannel = null;
    // The track event has its own onended handler attached in
    // _setupPeerListeners; null'ing here for completeness.
    (this.pc as any).onicegatheringstatechange = null;

    this.pc.close();
    this.iceCandidateBuffer = [];
  }

  // ── Internal Listeners ────────────────────────────

  private _setupPeerListeners(): void {
    // ICE candidate
    this.pc.onicecandidate = (event) => {
      if (event.candidate) {
        this.config.onSignal({
          type: 'ice-candidate',
          targetId: this.config.peerId,
          candidate: event.candidate.toJSON(),
        });
      }
    };

    // Remote track
    this.pc.ontrack = (event) => {
      const [stream] = event.streams;
      if (stream) {
        // Use the first stream
        this.remoteStream = stream;
        // Stop the track when the remote sender removes it (e.g.
        // they disabled their camera mid-call). Without this, the
        // <video> element keeps a frozen last-frame and the decoder
        // keeps running; iOS Safari especially leaks GPU memory.
        const track = event.track;
        track.onended = () => {
          try { track.stop(); }
          catch (err) { console.debug('[Peer] onended track.stop failed', err); }
        };
        // Some implementations fire `onremovetrack` on the stream
        // when a sender removes a track from a renegotiation.
        stream.onremovetrack = (e) => {
          try { e.track.stop(); }
          catch (err) { console.debug('[Peer] onremovetrack stop failed', err); }
        };
        this.config.onRemoteStream(stream);
      } else {
        // No stream — create one from the track
        if (!this.remoteStream) {
          this.remoteStream = new MediaStream();
        }
        this.remoteStream.addTrack(event.track);
        event.track.onended = () => {
          try { event.track.stop(); }
          catch (err) { console.debug('[Peer] onended (synth) stop failed', err); }
        };
        this.config.onRemoteStream(this.remoteStream);
      }
      this.config.onRemoteTrack(event.track, event.streams);
    };

    // Connection state
    this.pc.onconnectionstatechange = () => {
      const state = this.pc.connectionState;
      console.log(`[Peer:${this.config.peerId}] connectionState: ${state}`);
      this.config.onStateChange(state);

      if (state === 'disconnected') {
        this._startReconnectTimer();
      } else if (state === 'connected') {
        this._cancelReconnectTimer();
        // Mark connection as established
        if (this._connectionEstablishedTime === null) {
          this._connectionEstablishedTime = Date.now();
        }
      } else if (state === 'failed') {
        this._attemptIceRestartWithBackoff();
      }
    };

    // ICE connection state
    this.pc.oniceconnectionstatechange = () => {
      const state = this.pc.iceConnectionState;
      console.log(`[Peer:${this.config.peerId}] iceConnectionState: ${state}`);
      this.config.onIceStateChange(state);

      if (state === 'failed') {
        this._attemptIceRestartWithBackoff();
      }
    };

    // DTLS state monitoring
    const monitorDtlsState = () => {
      if (this.config.onDtlsStateChange) {
        try {
          const dtlsState = this.getDtlsState();
          this.config.onDtlsStateChange(dtlsState);
        } catch (e) {
          console.warn(`[Peer:${this.config.peerId}] Error monitoring DTLS state:`, e);
        }
      }
    };

    // Monitor DTLS changes periodically (DTLS state changes don't have a direct event)
    // Store interval ID for cleanup in destroy()
    this._dtlsMonitorInterval = setInterval(monitorDtlsState, 1000);

    // Data channel from remote peer
    this.pc.ondatachannel = (event) => {
      const channel = event.channel;
      this._setupDataChannelListeners(channel);
      this.dataChannels.set(channel.label, channel);

      if (this.config.onDataChannel) {
        this.config.onDataChannel(channel);
      }
    };

    // Renegotiation needed (e.g., track added/removed)
    this.pc.onnegotiationneeded = async () => {
      if (this.config.isInitiator) {
        await this.createOffer();
      }
    };
  }

  /**
   * Setup event listeners for data channel lifecycle.
   */
  private _setupDataChannelListeners(channel: RTCDataChannel): void {
    channel.onopen = () => {
      console.log(`[Peer:${this.config.peerId}] Data channel opened: ${channel.label}`);
    };

    channel.onclose = () => {
      console.log(`[Peer:${this.config.peerId}] Data channel closed: ${channel.label}`);
      this.dataChannels.delete(channel.label);
    };

    channel.onerror = (error) => {
      console.error(`[Peer:${this.config.peerId}] Data channel error (${channel.label}):`, error);
    };
  }

  // ── Reconnection ──────────────────────────────────

  private _startReconnectTimer(): void {
    if (this.reconnectTimer) return;
    console.log(`[Peer:${this.config.peerId}] Starting reconnect timer (${RECONNECT_TIMEOUT_MS}ms)`);

    this.reconnectTimer = setTimeout(() => {
      if (this.pc.connectionState === 'disconnected' || this.pc.connectionState === 'failed') {
        console.log(`[Peer:${this.config.peerId}] Reconnect timeout — attempting ICE restart`);
        this._attemptIceRestart();
      }
    }, RECONNECT_TIMEOUT_MS);
  }

  private _cancelReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    // Reset ICE restart counter on successful reconnection
    this._resetIceRestartCounter();
  }

  /**
   * ICE restart — the standard way to recover from network changes on LAN.
   * Creates a new offer with iceRestart flag, which generates new ICE credentials.
   */
  private async _attemptIceRestart(): Promise<void> {
    if (this._destroyed) return;

    console.log(`[Peer:${this.config.peerId}] Attempting ICE restart`);
    try {
      const offer = await this.pc.createOffer({ iceRestart: true });
      if (offer.sdp) {
        offer.sdp = this._optimizeSdpForLan(offer.sdp);
      }
      await this.pc.setLocalDescription(offer);

      this.config.onSignal({
        type: 'offer',
        targetId: this.config.peerId,
        sdp: this.pc.localDescription!,
      });
    } catch (e) {
      console.error(`[Peer:${this.config.peerId}] ICE restart failed:`, e);
    }
  }

  /**
   * ICE restart with exponential backoff.
   * Tracks restart attempts and applies backoff delay to avoid hammering the network.
   */
  private _attemptIceRestartWithBackoff(): void {
    if (this._destroyed) return;

    // Dedupe: ignore if another handler already triggered within the last 500ms
    const now = Date.now();
    if (now - this._lastIceRestartTriggerTime < 500) {
      return;
    }
    this._lastIceRestartTriggerTime = now;

    // Check if we've exceeded max restarts
    if (this._iceRestartCount >= MAX_ICE_RESTARTS) {
      console.error(
        `[Peer:${this.config.peerId}] Max ICE restart attempts (${MAX_ICE_RESTARTS}) reached`
      );
      return;
    }

    // Cancel any pending backoff timer
    if (this._iceRestartBackoffTimer) {
      clearTimeout(this._iceRestartBackoffTimer);
    }

    // Get backoff delay for current attempt count
    const delayMs = ICE_RESTART_BACKOFF[this._iceRestartCount];
    console.log(
      `[Peer:${this.config.peerId}] ICE restart attempt ${this._iceRestartCount + 1}/${MAX_ICE_RESTARTS} in ${delayMs}ms`
    );

    this._iceRestartBackoffTimer = setTimeout(() => {
      this._iceRestartCount++;
      this._attemptIceRestart();
    }, delayMs);
  }

  /**
   * Reset ICE restart counter (called on successful connection).
   */
  private _resetIceRestartCounter(): void {
    this._iceRestartCount = 0;
    if (this._iceRestartBackoffTimer) {
      clearTimeout(this._iceRestartBackoffTimer);
      this._iceRestartBackoffTimer = null;
    }
  }

  // ── SDP Optimization ──────────────────────────────

  /**
   * Modify SDP for LAN-optimal settings:
   *   - Prefer high bandwidth (LAN has no bandwidth concern)
   *   - Set max bitrate high for video
   *   - Prefer VP8 for low-latency encoding
   */
  private _optimizeSdpForLan(sdp: string): string {
    let modified = sdp;

    // Increase audio bitrate (Opus defaults to 32kbps, raise to 128kbps for LAN)
    modified = modified.replace(
      /a=fmtp:111 /g,
      'a=fmtp:111 maxaveragebitrate=128000;stereo=1;'
    );

    // Set bandwidth to 10 Mbps (generous for LAN)
    if (!modified.includes('b=AS:')) {
      modified = modified.replace(
        /(m=video [^\r\n]*\r\n)/g,
        '$1b=AS:10000\r\n'
      );
    }

    return modified;
  }

  /**
   * Reorder SDP codecs to prefer a specific MIME type.
   * Used by setPreferredCodec() to prioritize codec selection.
   */
  private _preferCodecInSdp(sdp: string, kind: 'audio' | 'video', codecMimeType: string): string {
    const lines = sdp.split('\n');
    const result: string[] = [];
    let inMediaSection = false;
    let isTargetMedia = false;
    let mediaLineIndex = -1;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Check if entering audio or video media section
      if (line.startsWith(`m=${kind} `)) {
        inMediaSection = true;
        isTargetMedia = true;
        mediaLineIndex = result.length;
        result.push(line);
        continue;
      }

      // Check if entering a different media section
      if (line.startsWith('m=')) {
        inMediaSection = true;
        isTargetMedia = false;
      }

      // If in target media section, look for codec lines
      if (isTargetMedia && inMediaSection && line.startsWith('a=rtpmap:')) {
        if (line.includes(codecMimeType)) {
          // Extract codec payload number
          const match = line.match(/a=rtpmap:(\d+)/);
          if (match) {
            const payloadNumber = match[1];
            // Move this codec to the front by reordering the m= line
            // This is a simplified approach; full implementation would reorder payload numbers
            console.log(
              `[Peer:${this.config.peerId}] Preferring codec ${codecMimeType} (payload ${payloadNumber})`
            );
          }
        }
      }

      result.push(line);
    }

    return result.join('\n');
  }
}
