/**
 * NetworkQualityMonitor — Continuous network health assessment.
 *
 * Feeds into GracefulDegradationEngine to drive quality decisions.
 *
 * Metrics tracked (Exponentially Weighted Moving Average):
 *   - RTT (round-trip time) — from server health pings and WebRTC stats
 *   - Jitter — variance in RTT over time
 *   - Packet loss rate — from WebRTC getStats() delta
 *   - Available bandwidth estimate — from WebRTC congestion signals
 *   - Connection stability — time since last disconnect/reconnect
 *
 * Quality Levels:
 *   excellent → RTT <10ms, loss <0.5%, jitter <3ms    (typical wired LAN)
 *   good      → RTT <30ms, loss <2%, jitter <10ms     (strong WiFi)
 *   fair      → RTT <80ms, loss <5%, jitter <25ms     (weak WiFi)
 *   poor      → RTT <200ms, loss <10%, jitter <50ms   (bad WiFi/interference)
 *   critical  → RTT >200ms or loss >10%               (barely connected)
 *
 * This monitor runs independently from QualityController. QualityController
 * reacts to per-peer WebRTC stats; NetworkQualityMonitor tracks the overall
 * network pipe and feeds into the degradation engine.
 */

// ── Types ──────────────────────────────────────────────

export type NetworkQuality = 'excellent' | 'good' | 'fair' | 'poor' | 'critical';

export interface NetworkSnapshot {
  quality: NetworkQuality;
  score: number;               // 0-100
  rtt: number;                 // ms (EWMA)
  jitter: number;              // ms (EWMA)
  packetLossRate: number;      // 0-1 (EWMA)
  estimatedBandwidthKbps: number;
  isStable: boolean;           // no disconnect in last 30s
  consecutivePoorSamples: number;
  timestamp: number;
}

export interface NetworkEvent {
  type: 'quality_changed' | 'critical_alert' | 'recovered' | 'bandwidth_probe';
  previous?: NetworkQuality;
  current: NetworkQuality;
  snapshot: NetworkSnapshot;
}

type NetworkEventCallback = (event: NetworkEvent) => void;

// ── EWMA Helper ────────────────────────────────────────

class EWMA {
  private _value: number;
  private _alpha: number;
  private _initialized = false;

  constructor(alpha: number = 0.3, initial: number = 0) {
    this._alpha = alpha;
    this._value = initial;
  }

  update(sample: number): number {
    if (!this._initialized) {
      this._value = sample;
      this._initialized = true;
    } else {
      this._value = this._alpha * sample + (1 - this._alpha) * this._value;
    }
    return this._value;
  }

  get value(): number { return this._value; }
  reset(initial: number = 0): void { this._value = initial; this._initialized = false; }
}

// ── Quality Thresholds ─────────────────────────────────

interface QualityThreshold {
  maxRtt: number;
  maxLoss: number;
  maxJitter: number;
  minBandwidthKbps: number;
}

const THRESHOLDS: Record<NetworkQuality, QualityThreshold> = {
  excellent: { maxRtt: 10, maxLoss: 0.005, maxJitter: 3, minBandwidthKbps: 10_000 },
  good:      { maxRtt: 30, maxLoss: 0.02,  maxJitter: 10, minBandwidthKbps: 5_000 },
  fair:      { maxRtt: 80, maxLoss: 0.05,  maxJitter: 25, minBandwidthKbps: 2_000 },
  poor:      { maxRtt: 200, maxLoss: 0.10, maxJitter: 50, minBandwidthKbps: 500 },
  critical:  { maxRtt: Infinity, maxLoss: 1, maxJitter: Infinity, minBandwidthKbps: 0 },
};

// ── Constants ──────────────────────────────────────────

const PING_INTERVAL_MS = 5_000;
const STABILITY_WINDOW_MS = 30_000;
const CRITICAL_CONSECUTIVE_THRESHOLD = 3;

// ── Monitor Implementation ─────────────────────────────

export class NetworkQualityMonitor {
  private _rtt = new EWMA(0.3, 5);
  private _jitter = new EWMA(0.25, 1);
  private _loss = new EWMA(0.2, 0);
  private _bandwidth = new EWMA(0.15, 10_000);

  private _currentQuality: NetworkQuality = 'excellent';
  private _previousQuality: NetworkQuality = 'excellent';
  private _consecutivePoor = 0;
  private _lastDisconnectTime = 0;
  private _lastPingSent = 0;

  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _listeners: NetworkEventCallback[] = [];
  private _destroyed = false;

  private _serverUrl: string;

  // History for trend analysis
  private _history: Array<{ quality: NetworkQuality; timestamp: number }> = [];
  private readonly _maxHistory = 120; // 10 minutes at 5s interval

  constructor(serverUrl: string) {
    this._serverUrl = serverUrl;
  }

  // ── Lifecycle ─────────────────────────────────────────

  start(): void {
    if (this._destroyed) return;
    this._pingTimer = setInterval(() => this._runPingProbe(), PING_INTERVAL_MS);
    // First probe immediately
    this._runPingProbe();
  }

  stop(): void {
    this._destroyed = true;
    if (this._pingTimer) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
  }

  destroy(): void {
    this.stop();
    this._listeners = [];
  }

  // ── Event Subscription ────────────────────────────────

  on(cb: NetworkEventCallback): () => void {
    this._listeners.push(cb);
    return () => {
      this._listeners = this._listeners.filter(l => l !== cb);
    };
  }

  // ── Feed external WebRTC stats ────────────────────────

  /**
   * Feed packet loss from WebRTC getStats() delta.
   * Called by QualityController or CallEngine on each stats poll.
   */
  feedPacketLoss(lossRate: number): void {
    this._loss.update(Math.max(0, Math.min(1, lossRate)));
    this._evaluate();
  }

  /**
   * Feed bandwidth estimate from WebRTC congestion controller.
   */
  feedBandwidthEstimate(kbps: number): void {
    if (kbps > 0) {
      this._bandwidth.update(kbps);
    }
  }

  /**
   * Notify of a socket disconnect event.
   */
  notifyDisconnect(): void {
    this._lastDisconnectTime = Date.now();
    this._consecutivePoor++;
    this._evaluate();
  }

  /**
   * Notify of a socket reconnect event.
   */
  notifyReconnect(): void {
    // Recovery will be detected in next evaluation cycle
  }

  // ── Get Current State ─────────────────────────────────

  getSnapshot(): NetworkSnapshot {
    return {
      quality: this._currentQuality,
      score: this._qualityToScore(this._currentQuality),
      rtt: Math.round(this._rtt.value * 10) / 10,
      jitter: Math.round(this._jitter.value * 10) / 10,
      packetLossRate: Math.round(this._loss.value * 1000) / 1000,
      estimatedBandwidthKbps: Math.round(this._bandwidth.value),
      isStable: (Date.now() - this._lastDisconnectTime) > STABILITY_WINDOW_MS,
      consecutivePoorSamples: this._consecutivePoor,
      timestamp: Date.now(),
    };
  }

  getQuality(): NetworkQuality {
    return this._currentQuality;
  }

  /**
   * Returns the recent quality trend: improving, stable, or degrading.
   */
  getTrend(): 'improving' | 'stable' | 'degrading' {
    if (this._history.length < 6) return 'stable';
    const recent = this._history.slice(-6);
    const scores = recent.map(h => this._qualityToScore(h.quality));
    const first3Avg = (scores[0] + scores[1] + scores[2]) / 3;
    const last3Avg = (scores[3] + scores[4] + scores[5]) / 3;
    const delta = last3Avg - first3Avg;
    if (delta > 10) return 'improving';
    if (delta < -10) return 'degrading';
    return 'stable';
  }

  // ── Internal: Ping Probe ──────────────────────────────

  private async _runPingProbe(): Promise<void> {
    if (this._destroyed) return;

    const start = performance.now();
    try {
      const resp = await fetch(`${this._serverUrl}/api/health`, {
        signal: AbortSignal.timeout(3000),
        cache: 'no-store',
      });
      const rtt = performance.now() - start;

      if (resp.ok) {
        const prevRtt = this._rtt.value;
        this._rtt.update(rtt);

        // Jitter = absolute difference from previous RTT
        const jitterSample = Math.abs(rtt - prevRtt);
        this._jitter.update(jitterSample);

        // Reset poor counter on successful ping
        if (this._consecutivePoor > 0 && rtt < THRESHOLDS.fair.maxRtt) {
          this._consecutivePoor = Math.max(0, this._consecutivePoor - 1);
        }
      } else {
        this._consecutivePoor++;
      }
    } catch {
      // Ping failed entirely — treat as high RTT
      this._rtt.update(500);
      this._jitter.update(200);
      this._consecutivePoor++;
    }

    this._evaluate();
  }

  // ── Internal: Quality Evaluation ──────────────────────

  private _evaluate(): void {
    const rtt = this._rtt.value;
    const jitter = this._jitter.value;
    const loss = this._loss.value;
    const bw = this._bandwidth.value;
    const isStable = (Date.now() - this._lastDisconnectTime) > STABILITY_WINDOW_MS;

    let quality: NetworkQuality = 'critical';

    if (rtt <= THRESHOLDS.excellent.maxRtt && loss <= THRESHOLDS.excellent.maxLoss && jitter <= THRESHOLDS.excellent.maxJitter) {
      quality = 'excellent';
    } else if (rtt <= THRESHOLDS.good.maxRtt && loss <= THRESHOLDS.good.maxLoss && jitter <= THRESHOLDS.good.maxJitter) {
      quality = 'good';
    } else if (rtt <= THRESHOLDS.fair.maxRtt && loss <= THRESHOLDS.fair.maxLoss && jitter <= THRESHOLDS.fair.maxJitter) {
      quality = 'fair';
    } else if (rtt <= THRESHOLDS.poor.maxRtt && loss <= THRESHOLDS.poor.maxLoss) {
      quality = 'poor';
    }

    // Recent instability demotes quality
    if (!isStable && quality !== 'critical') {
      const levels: NetworkQuality[] = ['excellent', 'good', 'fair', 'poor', 'critical'];
      const idx = levels.indexOf(quality);
      if (idx < levels.length - 1) {
        quality = levels[idx + 1];
      }
    }

    // Track history
    this._history.push({ quality, timestamp: Date.now() });
    if (this._history.length > this._maxHistory) {
      this._history = this._history.slice(-this._maxHistory);
    }

    // Emit events on quality change
    if (quality !== this._currentQuality) {
      this._previousQuality = this._currentQuality;
      this._currentQuality = quality;

      const snapshot = this.getSnapshot();
      const eventType = quality === 'critical' ? 'critical_alert' :
                        this._qualityToScore(quality) > this._qualityToScore(this._previousQuality) ? 'recovered' :
                        'quality_changed';

      this._emit({
        type: eventType,
        previous: this._previousQuality,
        current: quality,
        snapshot,
      });
    }

    // Reset consecutive poor on recovery
    if (quality === 'excellent' || quality === 'good') {
      this._consecutivePoor = 0;
    }
  }

  private _qualityToScore(q: NetworkQuality): number {
    switch (q) {
      case 'excellent': return 95;
      case 'good': return 75;
      case 'fair': return 50;
      case 'poor': return 25;
      case 'critical': return 5;
    }
  }

  private _emit(event: NetworkEvent): void {
    for (const cb of this._listeners) {
      try { cb(event); } catch {}
    }
  }
}
