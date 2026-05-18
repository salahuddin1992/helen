/**
 * mediaConstraintsBuilder — single source of truth for the MediaTrackConstraints
 * used both by the Settings preview AND by the real call pipeline.
 *
 * Reading from useSettingsStore here means a user's resolution / FPS / sample-rate
 * / EC / NS / AGC / autoMaxQuality choices are applied to every call, not just
 * the Settings preview. When `autoMaxQuality` is on (locally or forced by the
 * server policy) we probe each device's getCapabilities() to discover its
 * actual ceiling, then capture there — still clamped by the server's hard cap.
 *
 * Server policy is cached for 60 seconds so a burst of calls doesn't stampede
 * /media-policy/me. Callers that already know the policy (e.g. SettingsView
 * after its first fetch) can push it in via setServerMediaPolicy().
 */
import { useSettingsStore } from '@/stores/settings.store';
import { api } from '@/services/api.client';
import { isVirtualDeviceId } from './MediaDeviceManager';
import type { VideoResolution } from '@/types';

const RESOLUTION_MAP: Record<Exclude<VideoResolution, 'custom'>, { width: number; height: number }> = {
  '360p':  { width: 640,  height: 360 },
  '480p':  { width: 854,  height: 480 },
  '720p':  { width: 1280, height: 720 },
  '1080p': { width: 1920, height: 1080 },
  '1440p': { width: 2560, height: 1440 },
  '4k':    { width: 3840, height: 2160 },
  '5k':    { width: 5120, height: 2880 },
  '8k':    { width: 7680, height: 4320 },
};

export interface ServerMediaCap {
  max_width: number;
  max_height: number;
  max_framerate: number;
  auto_max_quality: boolean;
  enforce_hard_cap: boolean;
}

interface PolicyCache {
  policy: ServerMediaCap | null;
  fetchedAt: number;
}

const POLICY_TTL_MS = 60_000;
let _policyCache: PolicyCache = { policy: null, fetchedAt: 0 };

export function setServerMediaPolicy(policy: ServerMediaCap | null): void {
  _policyCache = { policy, fetchedAt: Date.now() };
}

async function getServerMediaPolicy(): Promise<ServerMediaCap | null> {
  const now = Date.now();
  if (_policyCache.policy && now - _policyCache.fetchedAt < POLICY_TTL_MS) {
    return _policyCache.policy;
  }
  try {
    const res = await api.getMyMediaCap();
    const cap = res.cap;
    const policy: ServerMediaCap = {
      max_width:        cap.max_width,
      max_height:       cap.max_height,
      max_framerate:    cap.max_framerate,
      auto_max_quality: !!cap.auto_max_quality,
      enforce_hard_cap: !!cap.enforce_hard_cap,
    };
    _policyCache = { policy, fetchedAt: now };
    return policy;
  } catch {
    // Fall back to the last good cache, or null — call path must still work
    // even if the media-policy endpoint is transiently down.
    return _policyCache.policy;
  }
}

function resolveManualResolution(): { width: number; height: number } {
  const { videoResolution, customVideoWidth, customVideoHeight } = useSettingsStore.getState().settings;
  if (videoResolution === 'custom') {
    return {
      width:  Math.max(16, customVideoWidth | 0),
      height: Math.max(16, customVideoHeight | 0),
    };
  }
  return RESOLUTION_MAP[videoResolution];
}

function resolveManualFrameRate(): number {
  const { useCustomFrameRate, customVideoFrameRate, videoFrameRate } = useSettingsStore.getState().settings;
  return useCustomFrameRate
    ? Math.max(1, customVideoFrameRate | 0)
    : videoFrameRate;
}

async function probeCameraMax(deviceId: string | undefined): Promise<{
  width: number; height: number; frameRate: number;
} | null> {
  try {
    const constraints: MediaTrackConstraints = deviceId ? { deviceId: { exact: deviceId } } : {};
    const stream = await navigator.mediaDevices.getUserMedia({ video: constraints });
    const track = stream.getVideoTracks()[0];
    const caps = (track as any).getCapabilities?.() ?? {};
    track.stop();
    return {
      width:     Number(caps.width?.max ?? 0) || 0,
      height:    Number(caps.height?.max ?? 0) || 0,
      frameRate: Number(caps.frameRate?.max ?? 0) || 0,
    };
  } catch {
    return null;
  }
}

async function probeMicMax(deviceId: string | undefined): Promise<{
  sampleRate: number; channelCount: number;
} | null> {
  try {
    const constraints: MediaTrackConstraints = deviceId ? { deviceId: { exact: deviceId } } : {};
    const stream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
    const track = stream.getAudioTracks()[0];
    const caps = (track as any).getCapabilities?.() ?? {};
    track.stop();
    return {
      sampleRate:   Number(caps.sampleRate?.max ?? 0) || 0,
      channelCount: Number(caps.channelCount?.max ?? 0) || 0,
    };
  } catch {
    return null;
  }
}

export interface BuildConstraintsOpts {
  audio: boolean;
  video: boolean;
  audioDeviceId?: string;   // pass 'default' or falsy to let the OS pick
  videoDeviceId?: string;
}

export interface BuiltConstraints {
  audio: MediaTrackConstraints | false;
  video: MediaTrackConstraints | false;
  // Meta the caller can log / display. Handy for debugging mismatches
  // between "what I asked for" vs "what the camera actually gave back".
  meta: {
    autoMax: boolean;
    probedCamera: { width: number; height: number; frameRate: number } | null;
    probedMic:    { sampleRate: number; channelCount: number } | null;
    serverPolicy: ServerMediaCap | null;
  };
}

/**
 * Build constraints for both audio and video based on the user's Settings
 * preferences and the server's media policy. Use this everywhere a call
 * path currently calls `getUserMedia` directly.
 */
export async function buildCallConstraints(opts: BuildConstraintsOpts): Promise<BuiltConstraints> {
  const settings = useSettingsStore.getState().settings;
  const policy = await getServerMediaPolicy();

  const autoMax =
    settings.autoMaxQuality ||
    Boolean(policy?.auto_max_quality && policy?.enforce_hard_cap);

  // ── Audio ──
  let audioOut: MediaTrackConstraints | false = false;
  let probedMic: { sampleRate: number; channelCount: number } | null = null;
  if (opts.audio) {
    const audioId = opts.audioDeviceId && opts.audioDeviceId !== 'default'
      ? opts.audioDeviceId
      : undefined;

    if (autoMax) {
      probedMic = await probeMicMax(audioId);
    }

    const ac: MediaTrackConstraints = {
      echoCancellation: settings.echoCancellation,
      noiseSuppression: settings.noiseSuppression,
      autoGainControl:  settings.autoGainControl,
      sampleRate: (autoMax && probedMic?.sampleRate)
        ? probedMic.sampleRate
        : settings.audioSampleRate,
    };
    // Virtual audio inputs (paired phone mic, iPhone-over-USB) are served
    // by our own MediaStream — they don't exist in navigator.mediaDevices,
    // so passing ``{exact: id}`` to getUserMedia throws OverconstrainedError
    // and nukes the whole capture. MediaDeviceManager already has a
    // separate code path for virtual sources; here we just drop the
    // constraint so real mic selection still works.
    if (audioId && !isVirtualDeviceId(audioId)) ac.deviceId = { exact: audioId };
    audioOut = ac;
  }

  // ── Video ──
  let videoOut: MediaTrackConstraints | false = false;
  let probedCamera: { width: number; height: number; frameRate: number } | null = null;
  if (opts.video) {
    const videoId = opts.videoDeviceId || settings.videoInputDevice || undefined;

    let width:  number;
    let height: number;
    let fps:    number;

    if (autoMax) {
      probedCamera = await probeCameraMax(videoId);
      const manual = resolveManualResolution();
      width  = probedCamera?.width     || manual.width;
      height = probedCamera?.height    || manual.height;
      fps    = probedCamera?.frameRate || resolveManualFrameRate();
    } else {
      const manual = resolveManualResolution();
      width  = manual.width;
      height = manual.height;
      fps    = resolveManualFrameRate();
    }

    // Clamp to the server's hard cap when it's enforced, otherwise the
    // server-side media-policy service clamps on receive anyway — capture
    // above the cap is legal but wasteful.
    if (policy?.enforce_hard_cap) {
      width  = Math.min(width,  policy.max_width);
      height = Math.min(height, policy.max_height);
      fps    = Math.min(fps,    policy.max_framerate);
    }

    const vc: MediaTrackConstraints = {
      width:     { ideal: width },
      height:    { ideal: height },
      frameRate: { ideal: fps },
    };
    // Same guard as audio: virtual video sources live outside the browser's
    // device list; pinning ``{exact: id}`` to one would fail with
    // OverconstrainedError and prevent the fallback to the default camera.
    if (videoId && !isVirtualDeviceId(videoId)) vc.deviceId = { exact: videoId };
    videoOut = vc;
  }

  return {
    audio: audioOut,
    video: videoOut,
    meta: { autoMax, probedCamera, probedMic, serverPolicy: policy },
  };
}
