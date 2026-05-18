/**
 * CapacityModel.ts — Hardware tier definitions with empirically-derived limits.
 *
 * Defines three concrete PC classes (Weak / Normal / Strong) mapped to
 * the existing 4-tier DeviceCapabilityDetector system (minimal/low/medium/high).
 *
 * Each class specifies:
 *   - Representative hardware specs (CPU, RAM, GPU)
 *   - Measured resource ceilings (CPU%, heap, bandwidth)
 *   - Maximum simultaneous operations per category
 *   - Real-world examples of common Windows PCs in each class
 *
 * Basis for all capacity numbers:
 *   - Electron renderer: Chromium single-process, ~60% of a core for UI
 *   - WebRTC: VP8 SW encode ~15-25% per 720p stream, ~5-8% per 480p
 *   - WebRTC: VP8 SW decode ~8-12% per 720p stream, ~3-5% per 480p
 *   - AudioContext: ~1-2% per peer (AEC + AGC + noise suppression)
 *   - Socket.IO: negligible CPU, ~200-500 bytes per event
 *   - SQLite backend: ~2-5% CPU during bulk query, negligible at idle
 *
 * CPU budget breakdown (100% = all logical cores):
 *   - Electron renderer UI thread: 8-15%
 *   - React reconciliation/layout: 3-8%
 *   - Socket.IO + event processing: 1-3%
 *   - OS + Electron main process: 5-10%
 *   - Available for media: remaining 64-83%
 *
 * These numbers are conservative — real headroom may be higher, but we
 * design for worst-case (background Windows updates, antivirus scans, etc).
 */

import type { DeviceTier } from '../performance/DeviceCapabilityDetector';

// ── Types ───────────────────────────────────────────────────

export type PCClass = 'weak' | 'normal' | 'strong';

export interface HardwareReference {
  /** Class name */
  class: PCClass;
  /** Human-readable label */
  label: string;
  /** Description */
  description: string;
  /** Representative CPU examples */
  cpuExamples: string[];
  /** Logical core count range */
  coreRange: [number, number];
  /** Approximate clock speed range (GHz) */
  clockRange: [number, number];
  /** RAM range (GB) */
  ramRange: [number, number];
  /** GPU description */
  gpuDescription: string;
  /** Whether discrete GPU is typical */
  discreteGPU: boolean;
  /** Real-world PC examples */
  pcExamples: string[];
  /** Maps to DeviceCapabilityDetector tiers */
  deviceTiers: DeviceTier[];
}

export interface ResourceCeiling {
  /** Maximum total CPU budget for CommClient (% of all cores) */
  maxCpuPercent: number;
  /** Maximum JS heap memory (MB) */
  maxHeapMB: number;
  /** Maximum WebRTC peer connections */
  maxPeerConnections: number;
  /** Maximum simultaneous video encode streams */
  maxVideoEncodes: number;
  /** Maximum simultaneous video decode streams */
  maxVideoDecodes: number;
  /** Maximum total media bandwidth (kbps) */
  maxBandwidthKbps: number;
  /** CPU budget available for media after UI overhead (%) */
  mediaCpuBudgetPercent: number;
  /** GPU decode capability (concurrent HW decodes) */
  gpuDecodeSlots: number;
  /** Estimated per-720p-encode CPU cost (%) */
  encode720pCpuPercent: number;
  /** Estimated per-480p-encode CPU cost (%) */
  encode480pCpuPercent: number;
  /** Estimated per-720p-decode CPU cost (%) */
  decode720pCpuPercent: number;
  /** Estimated per-480p-decode CPU cost (%) */
  decode480pCpuPercent: number;
}

export interface OperationLimits {
  /** Maximum 1:1 audio calls simultaneously */
  max1to1Audio: number;
  /** Maximum 1:1 video calls simultaneously (always 1 in practice) */
  max1to1Video: number;
  /** Maximum participants in a group audio call */
  maxGroupAudioParticipants: number;
  /** Maximum participants in a group video call */
  maxGroupVideoParticipants: number;
  /** Maximum screen share senders active simultaneously */
  maxScreenShareSenders: number;
  /** Maximum screen share receivers simultaneously */
  maxScreenShareReceivers: number;
  /** Maximum simultaneous file transfers */
  maxFileTransfers: number;
  /** Maximum open chat channels with active listeners */
  maxActiveChatChannels: number;
}

export interface PCClassProfile {
  hardware: HardwareReference;
  resources: ResourceCeiling;
  limits: OperationLimits;
}

// ── Weak PC (Celeron, Pentium, old i3, 2-4GB RAM) ──────────

const WEAK_HARDWARE: HardwareReference = {
  class: 'weak',
  label: 'Weak',
  description: 'Budget or aging PCs with dual-core CPUs and limited RAM. Common in schools, offices with older equipment, and budget home use.',
  cpuExamples: [
    'Intel Celeron N4020/N4120',
    'Intel Pentium Silver N5030',
    'Intel Core i3-4xxx (4th gen)',
    'AMD Athlon 3000G',
    'AMD A6/A8 series',
  ],
  coreRange: [2, 4],
  clockRange: [1.1, 2.4],
  ramRange: [2, 4],
  gpuDescription: 'Intel UHD 600 / Intel HD 4000-5000 / AMD Radeon R4-R5 (integrated only)',
  discreteGPU: false,
  pcExamples: [
    'HP Stream 14', 'Lenovo IdeaPad 1', 'Acer Aspire 1',
    'Dell Inspiron 3000 (budget)', 'Any PC from 2014-2017',
    'School/lab Chromebook-class hardware',
  ],
  deviceTiers: ['minimal', 'low'],
};

const WEAK_RESOURCES: ResourceCeiling = {
  maxCpuPercent: 30,         // 2 cores → ~60% total, keep 30% for OS
  maxHeapMB: 256,
  maxPeerConnections: 3,
  maxVideoEncodes: 1,        // 1 SW encode maxes out a weak CPU
  maxVideoDecodes: 2,
  maxBandwidthKbps: 2_000,
  mediaCpuBudgetPercent: 15, // Only 15% left after UI + OS on 2 cores
  gpuDecodeSlots: 1,
  encode720pCpuPercent: 25,  // VP8 720p SW encode on Celeron
  encode480pCpuPercent: 12,
  decode720pCpuPercent: 12,
  decode480pCpuPercent: 5,
};

const WEAK_LIMITS: OperationLimits = {
  max1to1Audio: 1,
  max1to1Video: 1,
  maxGroupAudioParticipants: 6,
  maxGroupVideoParticipants: 2,    // Self + 1 other with video
  maxScreenShareSenders: 0,        // Too expensive to encode screen
  maxScreenShareReceivers: 1,      // Can receive at low quality
  maxFileTransfers: 1,
  maxActiveChatChannels: 10,
};

// ── Normal PC (i5, Ryzen 5, 8GB RAM) ───────────────────────

const NORMAL_HARDWARE: HardwareReference = {
  class: 'normal',
  label: 'Normal',
  description: 'Standard mid-range PCs. Most common in homes and offices. The majority of Windows users fall here.',
  cpuExamples: [
    'Intel Core i5-8xxx to i5-12xxx',
    'Intel Core i3-10xxx to i3-13xxx',
    'AMD Ryzen 5 3600/5600',
    'AMD Ryzen 3 3300X/4300',
    'Intel Core i7-6xxx to i7-8xxx (older high-end)',
  ],
  coreRange: [4, 8],
  clockRange: [2.4, 4.0],
  ramRange: [8, 16],
  gpuDescription: 'Intel UHD 620-770 / AMD Radeon Vega 7-8 / entry discrete (GTX 1650, RX 6500)',
  discreteGPU: false,  // Some have discrete, most don't
  pcExamples: [
    'Dell Inspiron 15 5000', 'HP Pavilion 15', 'Lenovo IdeaPad 5',
    'Acer Aspire 5', 'Any mid-range office desktop',
    'ThinkPad E/T series', 'Surface Laptop Go',
  ],
  deviceTiers: ['medium'],
};

const NORMAL_RESOURCES: ResourceCeiling = {
  maxCpuPercent: 50,
  maxHeapMB: 512,
  maxPeerConnections: 6,
  maxVideoEncodes: 2,        // Can encode own video + screen share
  maxVideoDecodes: 4,
  maxBandwidthKbps: 8_000,
  mediaCpuBudgetPercent: 35, // 4-6 cores, decent headroom
  gpuDecodeSlots: 3,
  encode720pCpuPercent: 18,
  encode480pCpuPercent: 8,
  decode720pCpuPercent: 10,
  decode480pCpuPercent: 4,
};

const NORMAL_LIMITS: OperationLimits = {
  max1to1Audio: 1,
  max1to1Video: 1,
  maxGroupAudioParticipants: 12,
  maxGroupVideoParticipants: 5,    // Self + 4 others with video
  maxScreenShareSenders: 1,
  maxScreenShareReceivers: 1,      // Can receive while in video call
  maxFileTransfers: 3,
  maxActiveChatChannels: 30,
};

// ── Strong PC (i7/i9, Ryzen 7/9, 16+GB RAM) ───────────────

const STRONG_HARDWARE: HardwareReference = {
  class: 'strong',
  label: 'Strong',
  description: 'High-end workstations, gaming PCs, and recent flagships. Abundant CPU, RAM, and often a discrete GPU.',
  cpuExamples: [
    'Intel Core i7-10xxx to i7-14xxx',
    'Intel Core i9-12xxx to i9-14xxx',
    'AMD Ryzen 7 5800X/7800X',
    'AMD Ryzen 9 5900X/7900X',
    'Intel Core i5-13xxx+ (newer i5 = old i7 performance)',
  ],
  coreRange: [8, 24],
  clockRange: [3.0, 5.5],
  ramRange: [16, 64],
  gpuDescription: 'Discrete GPU (GTX 1660+, RTX 2060+, RX 6600+) or strong integrated (Iris Xe, Radeon 780M)',
  discreteGPU: true,
  pcExamples: [
    'Dell XPS 15/17', 'HP Spectre x360', 'Lenovo ThinkPad X1 Carbon',
    'Custom gaming desktops', 'Mac Pro / Surface Pro (high-end)',
    'Any workstation-class device',
  ],
  deviceTiers: ['high'],
};

const STRONG_RESOURCES: ResourceCeiling = {
  maxCpuPercent: 80,
  maxHeapMB: 1024,
  maxPeerConnections: 10,
  maxVideoEncodes: 3,        // Own video + screen share + potential relay
  maxVideoDecodes: 8,
  maxBandwidthKbps: 20_000,
  mediaCpuBudgetPercent: 60, // 8+ cores, lots of headroom
  gpuDecodeSlots: 8,         // Discrete GPU handles many decodes
  encode720pCpuPercent: 12,  // Modern CPU is more efficient
  encode480pCpuPercent: 5,
  decode720pCpuPercent: 6,
  decode480pCpuPercent: 2,
};

const STRONG_LIMITS: OperationLimits = {
  max1to1Audio: 1,
  max1to1Video: 1,
  maxGroupAudioParticipants: 20,
  maxGroupVideoParticipants: 8,    // Self + 7 others with video
  maxScreenShareSenders: 1,
  maxScreenShareReceivers: 2,      // Can view 2 screen shares simultaneously
  maxFileTransfers: 5,
  maxActiveChatChannels: 50,
};

// ── Profile Registry ────────────────────────────────────────

const PROFILES: Record<PCClass, PCClassProfile> = {
  weak: { hardware: WEAK_HARDWARE, resources: WEAK_RESOURCES, limits: WEAK_LIMITS },
  normal: { hardware: NORMAL_HARDWARE, resources: NORMAL_RESOURCES, limits: NORMAL_LIMITS },
  strong: { hardware: STRONG_HARDWARE, resources: STRONG_RESOURCES, limits: STRONG_LIMITS },
};

/**
 * Get the full capacity profile for a PC class.
 */
export function getCapacityProfile(pcClass: PCClass): PCClassProfile {
  return PROFILES[pcClass];
}

/**
 * Get all profiles.
 */
export function getAllCapacityProfiles(): PCClassProfile[] {
  return [PROFILES.weak, PROFILES.normal, PROFILES.strong];
}

/**
 * Map a DeviceTier to a PCClass.
 */
export function tierToPCClass(tier: DeviceTier): PCClass {
  switch (tier) {
    case 'minimal':
    case 'low':
      return 'weak';
    case 'medium':
      return 'normal';
    case 'high':
      return 'strong';
  }
}

/**
 * Get the operation limits for a given device tier.
 */
export function getLimitsForTier(tier: DeviceTier): OperationLimits {
  return PROFILES[tierToPCClass(tier)].limits;
}

/**
 * Get the resource ceiling for a given device tier.
 */
export function getResourcesForTier(tier: DeviceTier): ResourceCeiling {
  return PROFILES[tierToPCClass(tier)].resources;
}

/**
 * Estimate CPU cost of a hypothetical call scenario.
 * Returns { totalCpuPercent, withinBudget, headroomPercent }.
 */
export function estimateCallCpuCost(
  tier: DeviceTier,
  scenario: {
    videoEncodeCount: number;
    videoEncodeResolution: '720p' | '480p';
    videoDecodeCount: number;
    videoDecodeResolution: '720p' | '480p';
    audioStreamCount: number;
    screenShareEncode: boolean;
    screenShareDecode: boolean;
  },
): { totalCpuPercent: number; withinBudget: boolean; headroomPercent: number } {
  const res = getResourcesForTier(tier);

  const encodeCpu = scenario.videoEncodeResolution === '720p'
    ? res.encode720pCpuPercent
    : res.encode480pCpuPercent;

  const decodeCpu = scenario.videoDecodeResolution === '720p'
    ? res.decode720pCpuPercent
    : res.decode480pCpuPercent;

  let totalCpu = 0;

  // Video encode
  totalCpu += scenario.videoEncodeCount * encodeCpu;

  // Video decode
  totalCpu += scenario.videoDecodeCount * decodeCpu;

  // Audio processing (~2% per peer)
  totalCpu += scenario.audioStreamCount * 2;

  // Screen share encode (~equivalent to 720p video encode)
  if (scenario.screenShareEncode) totalCpu += res.encode720pCpuPercent;

  // Screen share decode (~equivalent to 720p video decode)
  if (scenario.screenShareDecode) totalCpu += res.decode720pCpuPercent;

  // UI overhead (fixed)
  totalCpu += 15;

  const withinBudget = totalCpu <= res.maxCpuPercent;
  const headroomPercent = res.maxCpuPercent - totalCpu;

  return {
    totalCpuPercent: Math.round(totalCpu),
    withinBudget,
    headroomPercent: Math.round(headroomPercent),
  };
}

/**
 * Compute the maximum number of video participants for a given tier
 * based on actual CPU cost estimation.
 */
export function computeMaxVideoParticipants(
  tier: DeviceTier,
  includeScreenShare: boolean = false,
): number {
  const res = getResourcesForTier(tier);
  let availableCpu = res.mediaCpuBudgetPercent;

  // Reserve CPU for own video encode (480p for most, 720p for strong)
  const ownEncodeRes = tier === 'high' ? '720p' : '480p' as const;
  const ownEncodeCost = ownEncodeRes === '720p' ? res.encode720pCpuPercent : res.encode480pCpuPercent;
  availableCpu -= ownEncodeCost;

  // Reserve for screen share decode if applicable
  if (includeScreenShare) {
    availableCpu -= res.decode720pCpuPercent;
  }

  // Each remote participant costs: decode + audio
  const decodeRes = tier === 'high' ? '720p' : '480p' as const;
  const perParticipantCost = (decodeRes === '720p' ? res.decode720pCpuPercent : res.decode480pCpuPercent) + 2;

  const maxByGPU = Math.min(res.maxVideoDecodes, res.gpuDecodeSlots);
  const maxByCPU = Math.floor(availableCpu / perParticipantCost);
  const maxByPeers = res.maxPeerConnections;

  // Take the minimum of all constraints, add 1 for self
  return Math.min(maxByCPU, maxByGPU, maxByPeers) + 1;
}
