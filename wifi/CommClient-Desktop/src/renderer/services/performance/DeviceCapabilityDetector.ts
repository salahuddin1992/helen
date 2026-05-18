/**
 * DeviceCapabilityDetector — Profiles the current device to determine
 * what media quality the hardware can sustain.
 *
 * Probes:
 *   - Logical CPU cores (navigator.hardwareConcurrency)
 *   - Available memory (navigator.deviceMemory, JS heap)
 *   - GPU renderer string (via WebGL)
 *   - Canvas performance benchmark (synthetic encode cost proxy)
 *   - Screen resolution (to cap video at display capability)
 *
 * Outputs a DeviceTier:
 *   - 'high'   → Desktop i5+/Ryzen5+, 8+ GB, dedicated GPU. Full 1080p60.
 *   - 'medium' → Mid-range laptop, 4-8 GB, integrated GPU. 720p30.
 *   - 'low'    → Atom/Celeron, 2-4 GB, or tablet-class. 480p15-24.
 *   - 'minimal'→ Very weak. 360p15 max, audio-priority mode auto-enabled.
 *
 * The detector runs ONCE at app startup and caches the result.
 * Components and the QualityController read the cached tier to set initial
 * quality ceilings before the network feedback loop kicks in.
 */

// ── Types ──────────────────────────────────────────────

export type DeviceTier = 'high' | 'medium' | 'low' | 'minimal';

export interface DeviceProfile {
  tier: DeviceTier;
  score: number;              // 0-100 composite score

  // Raw hardware signals
  cpuCores: number;
  memoryGB: number;           // estimated
  gpuRenderer: string;
  gpuVendor: string;
  screenWidth: number;
  screenHeight: number;
  devicePixelRatio: number;

  // Synthetic benchmark
  canvasFps: number;          // frames rendered in 1s of canvas stress test
  encodeBenchmark: number;    // ms to encode a test frame

  // Derived capabilities
  maxVideoWidth: number;
  maxVideoHeight: number;
  maxFramerate: number;
  maxBitrateKbps: number;
  canHandleGroupVideo: boolean;   // can sustain 4+ video streams
  recommendAudioOnly: boolean;    // device is too weak for reliable video
  maxGroupParticipantsWithVideo: number;

  // Timestamp
  detectedAt: number;
}

// ── Tier Thresholds ────────────────────────────────────

interface TierCeiling {
  maxWidth: number;
  maxHeight: number;
  maxFps: number;
  maxBitrateKbps: number;
  maxGroupWithVideo: number;
}

const TIER_CEILINGS: Record<DeviceTier, TierCeiling> = {
  high: {
    maxWidth: 1920,
    maxHeight: 1080,
    maxFps: 60,
    maxBitrateKbps: 10_000,
    maxGroupWithVideo: 8,
  },
  medium: {
    maxWidth: 1280,
    maxHeight: 720,
    maxFps: 30,
    maxBitrateKbps: 5_000,
    maxGroupWithVideo: 5,
  },
  low: {
    maxWidth: 854,
    maxHeight: 480,
    maxFps: 24,
    maxBitrateKbps: 2_000,
    maxGroupWithVideo: 3,
  },
  minimal: {
    maxWidth: 640,
    maxHeight: 360,
    maxFps: 15,
    maxBitrateKbps: 500,
    maxGroupWithVideo: 1,
  },
};

// ── Known Weak GPUs ────────────────────────────────────

const WEAK_GPU_PATTERNS = [
  /intel.*hd.*[2-4]\d{3}/i,
  /intel.*uhd.*6[0-2]\d/i,
  /mesa.*llvmpipe/i,
  /swiftshader/i,
  /microsoft basic render/i,
  /virtualbox/i,
  /vmware/i,
  /parallels/i,
];

const STRONG_GPU_PATTERNS = [
  /nvidia.*rtx/i,
  /nvidia.*gtx\s*1[0-9]{3}/i,
  /nvidia.*gtx\s*[2-9]\d{3}/i,
  /radeon.*rx\s*[5-7]\d{2,3}/i,
  /radeon.*rx\s*6\d{3}/i,
  /intel.*iris.*xe/i,
  /intel.*arc/i,
  /apple.*m[1-4]/i,
];

// ── Singleton ──────────────────────────────────────────

let cachedProfile: DeviceProfile | null = null;

// ── GPU Detection ──────────────────────────────────────

function detectGPU(): { renderer: string; vendor: string } {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return { renderer: 'unknown', vendor: 'unknown' };

    const glAny = gl as WebGLRenderingContext;
    const ext = glAny.getExtension('WEBGL_debug_renderer_info');
    if (ext) {
      return {
        renderer: glAny.getParameter(ext.UNMASKED_RENDERER_WEBGL) || 'unknown',
        vendor: glAny.getParameter(ext.UNMASKED_VENDOR_WEBGL) || 'unknown',
      };
    }
    return {
      renderer: glAny.getParameter(glAny.RENDERER) || 'unknown',
      vendor: glAny.getParameter(glAny.VENDOR) || 'unknown',
    };
  } catch {
    return { renderer: 'unknown', vendor: 'unknown' };
  }
}

// ── Canvas Benchmark (synthetic CPU/GPU encode cost) ───

function runCanvasBenchmark(): { fps: number; encodeCostMs: number } {
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = 480;
    const ctx = canvas.getContext('2d');
    if (!ctx) return { fps: 0, encodeCostMs: 999 };

    // Draw noise-like pattern for 1 second and count frames
    const startTime = performance.now();
    let frames = 0;
    const deadline = startTime + 1000;

    while (performance.now() < deadline && frames < 120) {
      // Simulate a frame: fill gradient + circles (non-trivial draw)
      const gradient = ctx.createLinearGradient(0, 0, 640, 480);
      gradient.addColorStop(0, `hsl(${frames * 3}, 70%, 50%)`);
      gradient.addColorStop(1, `hsl(${frames * 3 + 120}, 70%, 50%)`);
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, 640, 480);

      for (let i = 0; i < 10; i++) {
        ctx.beginPath();
        ctx.arc(
          Math.random() * 640,
          Math.random() * 480,
          Math.random() * 30 + 5,
          0,
          Math.PI * 2,
        );
        ctx.fillStyle = `rgba(${Math.random() * 255},${Math.random() * 255},${Math.random() * 255},0.5)`;
        ctx.fill();
      }

      frames++;
    }

    const elapsed = performance.now() - startTime;
    const fps = Math.round((frames / elapsed) * 1000);

    // Single-frame encode cost
    const encodeStart = performance.now();
    canvas.toDataURL('image/jpeg', 0.7);
    const encodeCostMs = performance.now() - encodeStart;

    return { fps, encodeCostMs };
  } catch {
    return { fps: 30, encodeCostMs: 50 };
  }
}

// ── Score Computation ──────────────────────────────────

function computeScore(
  cpuCores: number,
  memoryGB: number,
  gpuRenderer: string,
  canvasFps: number,
  encodeCostMs: number,
): number {
  let score = 0;

  // CPU score (0-30 points)
  if (cpuCores >= 8) score += 30;
  else if (cpuCores >= 6) score += 25;
  else if (cpuCores >= 4) score += 18;
  else if (cpuCores >= 2) score += 10;
  else score += 5;

  // Memory score (0-20 points)
  if (memoryGB >= 16) score += 20;
  else if (memoryGB >= 8) score += 16;
  else if (memoryGB >= 4) score += 10;
  else if (memoryGB >= 2) score += 5;
  else score += 2;

  // GPU score (0-25 points)
  if (STRONG_GPU_PATTERNS.some(p => p.test(gpuRenderer))) {
    score += 25;
  } else if (WEAK_GPU_PATTERNS.some(p => p.test(gpuRenderer))) {
    score += 5;
  } else {
    score += 14; // Unknown GPU → assume mid-range integrated
  }

  // Canvas benchmark score (0-25 points)
  if (canvasFps >= 90) score += 25;
  else if (canvasFps >= 60) score += 20;
  else if (canvasFps >= 30) score += 12;
  else if (canvasFps >= 15) score += 6;
  else score += 2;

  // Encode cost penalty
  if (encodeCostMs > 100) score -= 10;
  else if (encodeCostMs > 50) score -= 5;

  return Math.max(0, Math.min(100, score));
}

function scoreToTier(score: number): DeviceTier {
  if (score >= 75) return 'high';
  if (score >= 50) return 'medium';
  if (score >= 25) return 'low';
  return 'minimal';
}

// ── Public API ─────────────────────────────────────────

/**
 * Detect device capabilities. Runs benchmark on first call, returns
 * cached result on subsequent calls.
 *
 * Safe to call from any context — benchmark completes in ~1s.
 */
export function detectDeviceCapabilities(): DeviceProfile {
  if (cachedProfile) return cachedProfile;

  const cpuCores = navigator.hardwareConcurrency || 2;
  const memoryGB = (navigator as any).deviceMemory || estimateMemoryFromHeap();
  const gpu = detectGPU();
  const benchmark = runCanvasBenchmark();
  const screenWidth = window.screen.width * (window.devicePixelRatio || 1);
  const screenHeight = window.screen.height * (window.devicePixelRatio || 1);

  const score = computeScore(cpuCores, memoryGB, gpu.renderer, benchmark.fps, benchmark.encodeCostMs);
  const tier = scoreToTier(score);
  const ceiling = TIER_CEILINGS[tier];

  cachedProfile = {
    tier,
    score,
    cpuCores,
    memoryGB,
    gpuRenderer: gpu.renderer,
    gpuVendor: gpu.vendor,
    screenWidth,
    screenHeight,
    devicePixelRatio: window.devicePixelRatio || 1,
    canvasFps: benchmark.fps,
    encodeBenchmark: benchmark.encodeCostMs,
    maxVideoWidth: Math.min(ceiling.maxWidth, screenWidth),
    maxVideoHeight: Math.min(ceiling.maxHeight, screenHeight),
    maxFramerate: ceiling.maxFps,
    maxBitrateKbps: ceiling.maxBitrateKbps,
    canHandleGroupVideo: tier !== 'minimal' && tier !== 'low',
    recommendAudioOnly: tier === 'minimal',
    maxGroupParticipantsWithVideo: ceiling.maxGroupWithVideo,
    detectedAt: Date.now(),
  };

  console.log(`[DeviceCapability] Tier: ${tier} (score ${score}) — ${cpuCores} cores, ${memoryGB}GB RAM, GPU: ${gpu.renderer.slice(0, 50)}`);

  return cachedProfile;
}

/**
 * Get the cached profile. Returns null if detection hasn't run yet.
 */
export function getCachedProfile(): DeviceProfile | null {
  return cachedProfile;
}

/**
 * Force re-detection (useful after hardware changes).
 */
export function resetDetection(): void {
  cachedProfile = null;
}

/**
 * Get the tier ceiling for a given tier.
 */
export function getTierCeiling(tier: DeviceTier): TierCeiling {
  return TIER_CEILINGS[tier];
}

// ── Internal Helpers ───────────────────────────────────

function estimateMemoryFromHeap(): number {
  try {
    const perf = (performance as any).memory;
    if (perf?.jsHeapSizeLimit) {
      // jsHeapSizeLimit is roughly ~50% of total available RAM in Chrome
      return Math.round((perf.jsHeapSizeLimit / 1024 / 1024 / 1024) * 2);
    }
  } catch {}
  return 4; // Conservative default
}
