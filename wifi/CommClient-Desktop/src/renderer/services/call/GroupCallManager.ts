/**
 * GroupCallManager — mesh topology manager for multi-party LAN calls.
 *
 * On a local network, mesh is viable for small groups (2–8 participants)
 * because LAN bandwidth is effectively unlimited (100Mbps–1Gbps+).
 *
 * Each participant maintains N-1 PeerConnections (one per remote peer).
 * This manager handles:
 *   - Adding/removing participants dynamically
 *   - Routing signaling messages to the correct PeerConnection
 *   - Propagating local stream changes (mute, device swap) to all peers
 *   - Collecting aggregate quality metrics across all connections
 *   - Graceful teardown of individual or all connections
 *
 * For groups larger than MAX_MESH_SIZE, the system should fall back to
 * an SFU (mediasoup). This manager emits a warning when approaching the limit.
 */

import {
  PeerConnection,
  PeerConnectionConfig,
  SignalMessage,
} from './PeerConnection';

export interface GroupParticipant {
  peerId: string;
  displayName?: string;
  isAudioMuted: boolean;
  isVideoOff: boolean;
  isSharingScreen: boolean;
  /** Webinar feature — hand-raise indicator for the host. */
  isHandRaised?: boolean;
  /** ISO timestamp when hand was raised (FIFO ordering for the queue). */
  handRaisedAt?: string | null;
  connection: PeerConnection | null;
  remoteStream: MediaStream | null;
  joinedAt: number;
}

export interface GroupCallConfig {
  /** Local user ID */
  localUserId: string;
  /** Room/channel ID for the group call */
  roomId: string;
  /** Local media stream */
  localStream: MediaStream | null;
  /** Send signaling data to the server for relay */
  onSignal: (data: SignalMessage) => void;
  /** Remote participant's stream is available */
  onRemoteStream: (peerId: string, stream: MediaStream) => void;
  /** Remote participant's stream removed */
  onRemoteStreamRemoved: (peerId: string) => void;
  /** Participant joined */
  onParticipantJoined: (participant: GroupParticipant) => void;
  /** Participant left */
  onParticipantLeft: (peerId: string) => void;
  /** Connection state changed for a specific peer */
  onPeerStateChange: (peerId: string, state: RTCPeerConnectionState) => void;
  /** Mesh capacity warning */
  onMeshWarning: (message: string) => void;
  /** Server-issued ICE config (includes TURN credentials). When present,
   *  every PeerConnection in the group uses this instead of the LAN-only
   *  default. Without it, cross-network peers fail to establish.
   *  Audit fix #8d. */
  iceOverride?: RTCConfiguration;
}

/**
 * SFU configuration for potential upgrade from mesh topology.
 * Stored when canUpgradeToSFU() triggers, ready for transition.
 */
export interface SFUConfig {
  serverUrl: string;
  roomId: string;
  routerRtpCapabilities?: any;
}

/**
 * Participant metadata enriching the base participant info.
 * Includes connection quality assessment and role hierarchy.
 */
export interface ParticipantMetadata {
  displayName: string;
  avatarUrl?: string;
  role: 'host' | 'participant' | 'viewer';
  joinedAt: number;
  connectionQuality: 'excellent' | 'good' | 'fair' | 'poor';
}

/**
 * Mesh topology statistics for monitoring network health and performance.
 */
export interface MeshTopologyStats {
  totalConnections: number;
  averageRtt: number;
  worstConnectionRtt: number;
  bandwidthUtilization: {
    totalUploadKbps: number;
    totalDownloadKbps: number;
    peakUploadKbps: number;
    peakDownloadKbps: number;
  };
  healthyPeerCount: number;
  unhealthyPeerCount: number;
  averagePacketLoss: number;
}

/** Max peers in mesh before recommending SFU fallback */
const MAX_MESH_SIZE = 8;

/** Warn when approaching mesh limit */
const MESH_WARN_THRESHOLD = 6;

export class GroupCallManager {
  private config: GroupCallConfig;
  private participants: Map<string, GroupParticipant> = new Map();
  private _destroyed = false;

  // SFU upgrade infrastructure
  private _sfuConfig: SFUConfig | null = null;

  // Active speaker tracking
  private _activeSpeakers: Map<string, { level: number; lastActive: number }> = new Map();

  // Participant metadata storage
  private _metadata: Map<string, ParticipantMetadata> = new Map();

  // Bandwidth estimation cache with TTL
  private _bandwidthCache: {
    totalUpload: number;
    totalDownload: number;
    perPeer: Map<string, number>;
    cachedAt: number;
  } | null = null;
  private readonly BANDWIDTH_CACHE_TTL = 5000; // 5 seconds

  // Peer health check cache
  private _healthCheckCache: Map<
    string,
    { healthy: boolean; rtt: number; packetLoss: number; checkedAt: number }
  > = new Map();
  private readonly HEALTH_CHECK_CACHE_TTL = 10000; // 10 seconds

  // Pending operations tracking to prevent concurrent add/remove races
  private _pendingOperations: Set<string> = new Set();

  constructor(config: GroupCallConfig) {
    this.config = config;
  }

  get destroyed(): boolean {
    return this._destroyed;
  }

  get participantCount(): number {
    return this.participants.size;
  }

  get allParticipants(): GroupParticipant[] {
    return Array.from(this.participants.values());
  }

  /** Local user id (used by TopologyCoordinator / SFU wiring). */
  get localUserId(): string {
    return this.config.localUserId;
  }

  /** Public read-only accessor for the local MediaStream, used by SFU publish. */
  get localStream(): MediaStream | null {
    return this.config.localStream;
  }

  getParticipant(peerId: string): GroupParticipant | undefined {
    return this.participants.get(peerId);
  }

  // ── Participant Management ────────────────────────

  /**
   * Add a new participant to the mesh.
   * @param peerId Remote user ID
   * @param isInitiator Whether local side should create the offer
   *   (typically: the peer with the lexicographically smaller ID initiates)
   */
  addParticipant(peerId: string, isInitiator: boolean): PeerConnection {
    if (this._destroyed) throw new Error('GroupCallManager is destroyed');
    if (this._pendingOperations.has(peerId)) {
      const existing = this.participants.get(peerId);
      if (existing?.connection && !existing.connection.destroyed) {
        return existing.connection;
      }
      throw new Error(`Concurrent operation in progress for peerId ${peerId}`);
    }

    this._pendingOperations.add(peerId);

    // Check mesh capacity
    if (this.participants.size >= MAX_MESH_SIZE) {
      this.config.onMeshWarning(
        `Mesh limit reached (${MAX_MESH_SIZE}). Audio/video quality may degrade. Consider SFU for larger groups.`
      );
    } else if (this.participants.size >= MESH_WARN_THRESHOLD) {
      this.config.onMeshWarning(
        `${this.participants.size + 1} participants in mesh. Performance may degrade above ${MAX_MESH_SIZE}.`
      );
    }

    // Don't add duplicate
    const existing = this.participants.get(peerId);
    if (existing?.connection && !existing.connection.destroyed) {
      console.warn(`[GroupCall] Participant ${peerId} already exists, returning existing connection`);
      return existing.connection;
    }

    const pcConfig: PeerConnectionConfig = {
      peerId,
      isInitiator,
      localStream: this.config.localStream,
      onSignal: (data: SignalMessage) => {
        // Tag with fromId for server routing
        this.config.onSignal({
          ...data,
          fromId: this.config.localUserId,
        });
      },
      onRemoteTrack: (_track, _streams) => {
        // Handled via onRemoteStream
      },
      onStateChange: (state) => {
        this.config.onPeerStateChange(peerId, state);

        if (state === 'failed' || state === 'closed') {
          // Mark participant as disconnected but don't remove yet
          // The server will send an explicit leave event
          const p = this.participants.get(peerId);
          if (p) {
            p.remoteStream = null;
          }
        }
      },
      onIceStateChange: (_state) => {
        // Logged internally by PeerConnection
      },
      onRemoteStream: (stream) => {
        const p = this.participants.get(peerId);
        if (p) {
          p.remoteStream = stream;
        }
        this.config.onRemoteStream(peerId, stream);
      },
    };

    // Audit fix #8d: hand the server-issued ICE config (with TURN
    // credentials) down to every PeerConnection in the mesh. Without
    // this override, group calls between peers on different networks
    // (or behind symmetric NAT) silently fail at ICE because the
    // LAN-only default has no STUN/TURN.
    const pc = new PeerConnection(
      pcConfig,
      false,
      this.config.iceOverride,
    );

    const participant: GroupParticipant = {
      peerId,
      isAudioMuted: false,
      isVideoOff: false,
      isSharingScreen: false,
      connection: pc,
      remoteStream: null,
      joinedAt: Date.now(),
    };

    this.participants.set(peerId, participant);
    this.config.onParticipantJoined(participant);

    console.log(
      `[GroupCall] Added participant ${peerId} (initiator=${isInitiator}). Total: ${this.participants.size}`
    );

    this._pendingOperations.delete(peerId);
    return pc;
  }

  /**
   * Remove a participant from the mesh and tear down their PeerConnection.
   */
  removeParticipant(peerId: string): void {
    if (this._pendingOperations.has(peerId)) {
      console.warn(`[GroupCall] Concurrent operation in progress for peerId ${peerId}, skipping remove`);
      return;
    }

    this._pendingOperations.add(peerId);

    const participant = this.participants.get(peerId);
    if (!participant) {
      this._pendingOperations.delete(peerId);
      return;
    }

    if (participant.connection && !participant.connection.destroyed) {
      participant.connection.destroy();
    }

    this.participants.delete(peerId);
    this.config.onRemoteStreamRemoved(peerId);
    this.config.onParticipantLeft(peerId);

    console.log(
      `[GroupCall] Removed participant ${peerId}. Remaining: ${this.participants.size}`
    );

    this._pendingOperations.delete(peerId);
  }

  // ── Signaling Routing ─────────────────────────────

  /**
   * Route an incoming signaling message to the correct PeerConnection.
   * If the sender is unknown, auto-add them as a non-initiator.
   */
  async handleSignal(fromId: string, signal: SignalMessage): Promise<void> {
    if (this._destroyed) return;

    let participant = this.participants.get(fromId);

    // Auto-add unknown peer (they initiated, so we are NOT the initiator)
    if (!participant || !participant.connection) {
      console.log(`[GroupCall] Auto-adding unknown peer ${fromId} from signal`);
      this.addParticipant(fromId, false);
      participant = this.participants.get(fromId)!;
    }

    const pc = participant.connection!;

    switch (signal.type) {
      case 'offer':
        if (signal.sdp) {
          await pc.handleOffer(signal.sdp);
        }
        break;

      case 'answer':
        if (signal.sdp) {
          await pc.handleAnswer(signal.sdp);
        }
        break;

      case 'ice-candidate':
        if (signal.candidate) {
          await pc.handleIceCandidate(signal.candidate);
        }
        break;

      case 'renegotiate':
        // Remote side requests renegotiation
        if (this._shouldInitiate(fromId)) {
          await pc.createOffer();
        }
        break;
    }
  }

  /**
   * Determine if local side should initiate (create offer) based on ID comparison.
   * Consistent tiebreaker: lexicographically smaller ID initiates.
   */
  private _shouldInitiate(remoteId: string): boolean {
    return this.config.localUserId < remoteId;
  }

  // ── Local Stream Operations ───────────────────────

  /**
   * Update the local stream across all peer connections.
   * Called when the user changes their media (e.g., switches device).
   */
  updateLocalStream(stream: MediaStream): void {
    this.config.localStream = stream;

    for (const [, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        participant.connection.addLocalStream(stream);
      }
    }
  }

  /**
   * Replace a specific track (audio or video) across all peer connections.
   * Used for hot-swapping devices mid-call.
   */
  async replaceTrackAll(newTrack: MediaStreamTrack): Promise<void> {
    const promises: Promise<void>[] = [];

    for (const [, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        promises.push(participant.connection.replaceTrack(newTrack));
      }
    }

    await Promise.allSettled(promises);
  }

  /**
   * Add a screen share track to all peer connections.
   * Returns a Map of peerId → RTCRtpSender for later removal.
   */
  addScreenTrackAll(
    track: MediaStreamTrack,
    stream: MediaStream
  ): Map<string, RTCRtpSender> {
    const senders = new Map<string, RTCRtpSender>();

    for (const [peerId, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        const sender = participant.connection.addScreenTrack(track, stream);
        senders.set(peerId, sender);
      }
    }

    return senders;
  }

  /**
   * Remove screen share track from all peer connections.
   */
  removeScreenTrackAll(senders: Map<string, RTCRtpSender>): void {
    for (const [peerId, sender] of senders) {
      const participant = this.participants.get(peerId);
      if (participant?.connection && !participant.connection.destroyed) {
        participant.connection.removeScreenTrack(sender);
      }
    }
  }

  // ── Participant State Updates ─────────────────────

  /**
   * Update a remote participant's media state (received from server).
   */
  updateParticipantState(
    peerId: string,
    state: Partial<Pick<
      GroupParticipant,
      'isAudioMuted' | 'isVideoOff' | 'isSharingScreen' | 'displayName'
        | 'isHandRaised' | 'handRaisedAt'
    >>
  ): void {
    const participant = this.participants.get(peerId);
    if (!participant) return;

    if (state.isAudioMuted !== undefined) participant.isAudioMuted = state.isAudioMuted;
    if (state.isVideoOff !== undefined) participant.isVideoOff = state.isVideoOff;
    if (state.isSharingScreen !== undefined) participant.isSharingScreen = state.isSharingScreen;
    if (state.displayName !== undefined) participant.displayName = state.displayName;
    if (state.isHandRaised !== undefined) participant.isHandRaised = state.isHandRaised;
    if (state.handRaisedAt !== undefined) participant.handRaisedAt = state.handRaisedAt;
  }

  // ── Quality / Bitrate Controls ────────────────────

  /**
   * Set video bitrate limit across all peer connections.
   */
  async setVideoBitrateAll(maxBitrateKbps: number): Promise<void> {
    const promises: Promise<void>[] = [];

    for (const [, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        promises.push(participant.connection.setVideoBitrate(maxBitrateKbps));
      }
    }

    await Promise.allSettled(promises);
  }

  /**
   * Set video framerate limit across all peer connections.
   */
  async setVideoFramerateAll(maxFramerate: number): Promise<void> {
    const promises: Promise<void>[] = [];

    for (const [, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        promises.push(participant.connection.setVideoFramerate(maxFramerate));
      }
    }

    await Promise.allSettled(promises);
  }

  /**
   * Set audio bitrate limit across all peer connections.
   * Lets QualityController throttle Opus so a noisy mesh doesn't
   * burst the audio channel above the server-side cap.
   */
  async setAudioBitrateAll(maxBitrateKbps: number): Promise<void> {
    const promises: Promise<void>[] = [];

    for (const [, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        promises.push(participant.connection.setAudioBitrate(maxBitrateKbps));
      }
    }

    await Promise.allSettled(promises);
  }

  /**
   * Collect aggregate quality metrics from all peer connections.
   */
  async getAggregateMetrics(): Promise<
    Map<
      string,
      { rtt: number; packetsLost: number; jitter: number; bitrate: number; codec: string }
    >
  > {
    const metrics = new Map<
      string,
      { rtt: number; packetsLost: number; jitter: number; bitrate: number; codec: string }
    >();

    for (const [peerId, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        try {
          const m = await participant.connection.getQualityMetrics();
          metrics.set(peerId, m);
        } catch {
          // Skip failed stats collection
        }
      }
    }

    return metrics;
  }

  // ── SFU-Ready Architecture ────────────────────────

  /**
   * Check if mesh topology should upgrade to SFU (mediasoup).
   * Returns true when participant count exceeds practical mesh limits (>8 or approaching).
   */
  canUpgradeToSFU(): boolean {
    return this.participants.size > MAX_MESH_SIZE;
  }

  /**
   * Prepare for SFU upgrade by storing configuration.
   * Called preemptively when canUpgradeToSFU() returns true.
   * Allows graceful transition from mesh to centralized architecture.
   */
  async prepareSFUUpgrade(config: SFUConfig): Promise<void> {
    if (this._destroyed) throw new Error('GroupCallManager is destroyed');

    this._sfuConfig = config;
    console.log('[GroupCall] SFU upgrade prepared:', {
      serverUrl: config.serverUrl,
      roomId: config.roomId,
      capabilities: config.routerRtpCapabilities ? 'provided' : 'not provided',
    });
  }

  /**
   * Get stored SFU configuration (if any).
   */
  getSFUConfig(): SFUConfig | null {
    return this._sfuConfig;
  }

  // ── Dynamic Mesh Quality Scaling ─────────────────

  /**
   * Calculate optimal per-peer bitrate based on participant count.
   * As mesh topology scales, per-peer bandwidth decreases to maintain aggregate quality.
   * Formula: base bitrate / sqrt(participant count) to balance fairness and quality.
   */
  getOptimalBitrateForMesh(): number {
    const participantCount = this.participants.size;

    // Base bitrate for video in LAN (high, since LAN has plenty of bandwidth)
    const baseBitrateKbps = 2500;

    if (participantCount <= 1) return baseBitrateKbps;

    // Scale down with sqrt(n) to maintain reasonable aggregate bandwidth
    // 2 peers: 2500 / 1.41 = ~1768 kbps each
    // 4 peers: 2500 / 2 = 1250 kbps each
    // 8 peers: 2500 / 2.83 = ~883 kbps each
    const scaleFactor = Math.sqrt(participantCount);
    const perPeerBitrate = Math.max(
      Math.floor(baseBitrateKbps / scaleFactor),
      500 // Floor at 500 kbps minimum to maintain video quality
    );

    return perPeerBitrate;
  }

  /**
   * Apply dynamic bitrate and framerate scaling across all peer connections.
   * Reduces quality gracefully as participant count increases to prevent network saturation.
   */
  async applyMeshQualityScaling(): Promise<void> {
    if (this._destroyed) return;

    const optimalBitrate = this.getOptimalBitrateForMesh();

    // Scale framerate: 30 fps baseline, down to 15 fps at high participant counts
    let maxFramerate = 30;
    if (this.participants.size >= 6) maxFramerate = 24;
    if (this.participants.size >= 8) maxFramerate = 15;

    const promises: Promise<void>[] = [];

    try {
      promises.push(this.setVideoBitrateAll(optimalBitrate));
      promises.push(this.setVideoFramerateAll(maxFramerate));

      await Promise.allSettled(promises);

      console.log('[GroupCall] Applied mesh quality scaling:', {
        participantCount: this.participants.size,
        bitrateKbps: optimalBitrate,
        framerateFps: maxFramerate,
      });
    } catch (err) {
      console.error('[GroupCall] Error applying mesh quality scaling:', err);
    }
  }

  // ── Active Speaker Detection ──────────────────────

  /**
   * Update active speaker status for a peer.
   * Called by audio level monitor (typically from WebAudio AnalyserNode).
   * @param peerId Remote peer ID
   * @param audioLevel Audio level (0–1 or 0–100 depending on source, will be normalized)
   */
  updateActiveSpeaker(peerId: string, audioLevel: number): void {
    if (!this.participants.has(peerId)) return;

    // Normalize to 0–1 range if level appears to be percentage (>1)
    const normalizedLevel = audioLevel > 1 ? audioLevel / 100 : audioLevel;

    // Threshold: only mark as active if level > 0.1 (avoiding noise floor)
    if (normalizedLevel > 0.1) {
      this._activeSpeakers.set(peerId, {
        level: normalizedLevel,
        lastActive: Date.now(),
      });
    } else {
      // Remove from active list if below threshold, but keep for 5 seconds for smoothness
      const current = this._activeSpeakers.get(peerId);
      if (current && Date.now() - current.lastActive > 5000) {
        this._activeSpeakers.delete(peerId);
      }
    }
  }

  /**
   * Get top N most recently active speakers, sorted by audio level descending.
   * Useful for spotlight layout or participant highlight order.
   */
  getActiveSpeakers(maxCount: number = 3): string[] {
    const now = Date.now();
    const speakers = Array.from(this._activeSpeakers.entries())
      .filter(([_, data]) => now - data.lastActive < 5000) // Still active in last 5 seconds
      .sort(([, a], [, b]) => b.level - a.level) // Sort by level descending
      .slice(0, maxCount)
      .map(([peerId]) => peerId);

    return speakers;
  }

  /**
   * Get the single dominant (loudest recently-active) speaker.
   * Returns null if no speaker activity detected.
   */
  getDominantSpeaker(): string | null {
    const speakers = this.getActiveSpeakers(1);
    return speakers.length > 0 ? speakers[0] : null;
  }

  // ── Participant Metadata ──────────────────────────

  /**
   * Set or update participant metadata (display name, avatar, role, connection quality).
   */
  setParticipantMetadata(peerId: string, metadata: ParticipantMetadata): void {
    if (!this.participants.has(peerId)) {
      console.warn(`[GroupCall] Cannot set metadata for unknown peer: ${peerId}`);
      return;
    }

    this._metadata.set(peerId, metadata);

    // Also update displayName in the participant object for backward compatibility
    const participant = this.participants.get(peerId);
    if (participant) {
      participant.displayName = metadata.displayName;
    }

    console.log(`[GroupCall] Updated metadata for ${peerId}:`, {
      displayName: metadata.displayName,
      role: metadata.role,
      quality: metadata.connectionQuality,
    });
  }

  /**
   * Get participant metadata.
   */
  getParticipantMetadata(peerId: string): ParticipantMetadata | undefined {
    return this._metadata.get(peerId);
  }

  /**
   * Get all participant metadata.
   */
  getAllMetadata(): Map<string, ParticipantMetadata> {
    return new Map(this._metadata);
  }

  /**
   * Remove participant metadata when they leave.
   */
  private _clearParticipantMetadata(peerId: string): void {
    this._metadata.delete(peerId);
    this._activeSpeakers.delete(peerId);
  }

  // ── Bandwidth Estimation for Mesh ─────────────────

  /**
   * Estimate aggregate bandwidth usage across all peer connections.
   * Returns total upload/download plus per-peer breakdown.
   * Caches results for 5 seconds to avoid repeated stat queries.
   */
  async getAggregateBandwidth(): Promise<{
    totalUpload: number;
    totalDownload: number;
    perPeer: Map<string, number>;
  }> {
    const now = Date.now();

    // Return cached result if still valid
    if (this._bandwidthCache && now - this._bandwidthCache.cachedAt < this.BANDWIDTH_CACHE_TTL) {
      return {
        totalUpload: this._bandwidthCache.totalUpload,
        totalDownload: this._bandwidthCache.totalDownload,
        perPeer: new Map(this._bandwidthCache.perPeer),
      };
    }

    let totalUpload = 0;
    let totalDownload = 0;
    const perPeer = new Map<string, number>();

    for (const [peerId, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        try {
          const metrics = await participant.connection.getQualityMetrics();
          // Bitrate is typically in kbps; sum for aggregate
          perPeer.set(peerId, metrics.bitrate);
          totalUpload += metrics.bitrate;
          totalDownload += metrics.bitrate;
        } catch {
          // Skip failed metrics
        }
      }
    }

    // Cache result
    this._bandwidthCache = {
      totalUpload,
      totalDownload,
      perPeer,
      cachedAt: now,
    };

    return { totalUpload, totalDownload, perPeer };
  }

  // ── Peer Connection Health Check ──────────────────

  /**
   * Check the health of all peer connections.
   * Returns map of peerId → { healthy, rtt, packetLoss }.
   * Health is "good" if RTT < 50ms and packet loss < 1%.
   * Caches results for 10 seconds.
   */
  async checkAllPeerHealth(): Promise<
    Map<string, { healthy: boolean; rtt: number; packetLoss: number }>
  > {
    const now = Date.now();
    const result = new Map<string, { healthy: boolean; rtt: number; packetLoss: number }>();

    for (const [peerId, participant] of this.participants) {
      // Check cache first
      const cached = this._healthCheckCache.get(peerId);
      if (cached && now - cached.checkedAt < this.HEALTH_CHECK_CACHE_TTL) {
        result.set(peerId, {
          healthy: cached.healthy,
          rtt: cached.rtt,
          packetLoss: cached.packetLoss,
        });
        continue;
      }

      if (participant.connection && !participant.connection.destroyed) {
        try {
          const metrics = await participant.connection.getQualityMetrics();

          // Health thresholds: RTT < 50ms AND packet loss < 1%
          const healthy = metrics.rtt < 50 && (metrics.packetsLost ?? 0) < 0.01;

          const health = {
            healthy,
            rtt: metrics.rtt,
            packetLoss: metrics.packetsLost ?? 0,
          };

          result.set(peerId, health);

          // Cache result
          this._healthCheckCache.set(peerId, {
            ...health,
            checkedAt: now,
          });
        } catch {
          // Treat failed checks as unhealthy
          const health = { healthy: false, rtt: 9999, packetLoss: 1.0 };
          result.set(peerId, health);
        }
      }
    }

    return result;
  }

  /**
   * Get list of peers with poor health (RTT > 50ms or packet loss > 1%).
   */
  async getUnhealthyPeers(): Promise<string[]> {
    const healthMap = await this.checkAllPeerHealth();
    return Array.from(healthMap.entries())
      .filter(([_, health]) => !health.healthy)
      .map(([peerId]) => peerId);
  }

  // ── Layout Recommendation for UI ──────────────────

  /**
   * Recommend UI layout based on participant count and active speaker state.
   * - 'grid': balanced for 2-6 participants
   * - 'spotlight': focus on dominant speaker (6-8+ participants)
   * - 'sidebar': host + main participant + list (default fallback)
   */
  getRecommendedLayout(): 'grid' | 'spotlight' | 'sidebar' {
    const count = this.participants.size;

    if (count <= 6) {
      return 'grid'; // Grid works well up to 6 participants
    } else if (count <= 8 && this.getDominantSpeaker()) {
      return 'spotlight'; // Emphasize active speaker for larger groups
    } else {
      return 'sidebar'; // Fallback: show participant list with controls
    }
  }

  // ── Mesh Topology Statistics ──────────────────────

  /**
   * Collect comprehensive mesh topology statistics.
   * Includes connection count, RTT distribution, bandwidth, and health overview.
   */
  async getMeshStats(): Promise<MeshTopologyStats> {
    const healthMap = await this.checkAllPeerHealth();
    const bandwidthData = await this.getAggregateBandwidth();

    const rttValues: number[] = [];
    let healthyCount = 0;
    let unhealthyCount = 0;

    for (const [_, health] of healthMap) {
      rttValues.push(health.rtt);
      if (health.healthy) healthyCount++;
      else unhealthyCount++;
    }

    const avgRtt =
      rttValues.length > 0
        ? Math.round(rttValues.reduce((a, b) => a + b, 0) / rttValues.length)
        : 0;
    const maxRtt = rttValues.length > 0 ? Math.max(...rttValues) : 0;
    const avgPacketLoss =
      Array.from(healthMap.values()).reduce((sum, h) => sum + h.packetLoss, 0) /
      Math.max(healthMap.size, 1);

    return {
      totalConnections: this.participants.size,
      averageRtt: avgRtt,
      worstConnectionRtt: maxRtt,
      bandwidthUtilization: {
        totalUploadKbps: bandwidthData.totalUpload,
        totalDownloadKbps: bandwidthData.totalDownload,
        peakUploadKbps: Math.max(...Array.from(bandwidthData.perPeer.values())),
        peakDownloadKbps: Math.max(...Array.from(bandwidthData.perPeer.values())),
      },
      healthyPeerCount: healthyCount,
      unhealthyPeerCount: unhealthyCount,
      averagePacketLoss: avgPacketLoss,
    };
  }

  // ── Cleanup ───────────────────────────────────────

  /**
   * Destroy all peer connections and clean up.
   */
  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    for (const [peerId, participant] of this.participants) {
      if (participant.connection && !participant.connection.destroyed) {
        participant.connection.destroy();
      }
      this.config.onRemoteStreamRemoved(peerId);
      this._clearParticipantMetadata(peerId);
    }

    this.participants.clear();
    this._activeSpeakers.clear();
    this._metadata.clear();
    this._bandwidthCache = null;
    this._healthCheckCache.clear();
    this._sfuConfig = null;

    console.log('[GroupCall] Destroyed — all connections closed');
  }
}
