/**
 * PreJoinScreen — pre-flight camera/mic test before joining a call.
 *
 * Why
 * ---
 * Joining a 200-person call only to discover your mic was muted at
 * the OS level, your camera was claimed by another app, or that
 * the wrong device is selected wastes everyone's time. This screen
 * runs once before the actual ``acceptCall`` / ``initiateCall``
 * action and lets the user:
 *   - See themselves on camera (or pick a different one).
 *   - Watch a live mic level meter.
 *   - Toggle mute/video for the join.
 *   - Pick devices.
 *   - Cancel without burning a call slot.
 *
 * The screen is a lightweight modal that sits above the rest of
 * the app. It owns its own preview MediaStream — we tear it down
 * before calling the real engine method, which then re-acquires
 * fresh constraints. Without that handoff a single camera handle
 * would be claimed by both the preview and the call, which fails
 * on Windows.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Mic, MicOff, Camera, CameraOff, Phone, X, Activity } from 'lucide-react';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

export type PreJoinIntent =
  | { kind: 'accept' }                              // accept incoming
  | { kind: 'initiate-1to1'; targetId: string; type: 'audio' | 'video' }
  | { kind: 'initiate-group'; channelId: string; type: 'audio' | 'video' };

interface Props {
  intent: PreJoinIntent;
  onCancel: () => void;
  onJoined: () => void;
  /** Display name for the call subject — shown in the screen header. */
  title?: string;
}

const PreJoinScreen: React.FC<Props> = ({ intent, onCancel, onJoined, title }) => {
  const acceptCall = useCallStore((s) => s.acceptCall);
  const initiateCall = useCallStore((s) => s.initiateCall);
  const initiateGroupCall = useCallStore((s) => s.initiateGroupCall);
  const refreshDevices = useCallStore((s) => s.refreshDevices);
  const devices = useCallStore((s) => s.devices);

  const [mutedAudio, setMutedAudio] = useState(false);
  const [mutedVideo, setMutedVideo] = useState(false);
  const [audioDevice, setAudioDevice] = useState<string>('');
  const [videoDevice, setVideoDevice] = useState<string>('');
  const [audioLevel, setAudioLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [joining, setJoining] = useState(false);

  // Network pre-flight result.
  const [netTest, setNetTest] = useState<{
    rtt_ms: number;
    grade: 'excellent' | 'good' | 'fair' | 'poor';
  } | null>(null);
  const [testing, setTesting] = useState(false);

  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);

  // Determine if the intent involves video so we don't try to
  // acquire a camera for an audio-only call. Audio-only intents
  // still get a mic-meter preview.
  const wantsVideo = intent.kind === 'initiate-1to1' || intent.kind === 'initiate-group'
    ? intent.type === 'video'
    : true;

  // Acquire preview stream once devices are known.
  useEffect(() => {
    let cancelled = false;
    void refreshDevices();

    const acquire = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: audioDevice ? { deviceId: { exact: audioDevice } } : true,
          video: wantsVideo
            ? (videoDevice ? { deviceId: { exact: videoDevice } } : true)
            : false,
        });
        if (cancelled) {
          for (const t of stream.getTracks()) t.stop();
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          (videoRef.current as any).srcObject = stream;
        }
        // Apply current mute toggles to the fresh stream.
        for (const t of stream.getAudioTracks()) t.enabled = !mutedAudio;
        for (const t of stream.getVideoTracks()) t.enabled = !mutedVideo;
        setError(null);
        _attachMicMeter(stream);
      } catch (err: any) {
        setError(err?.message || 'لم نتمكن من الوصول للكاميرا/الميك');
      }
    };

    void acquire();

    return () => {
      cancelled = true;
      _teardown();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioDevice, videoDevice, wantsVideo]);

  // Apply mute changes to existing tracks (no re-acquire needed).
  useEffect(() => {
    const s = streamRef.current;
    if (!s) return;
    for (const t of s.getAudioTracks()) t.enabled = !mutedAudio;
  }, [mutedAudio]);
  useEffect(() => {
    const s = streamRef.current;
    if (!s) return;
    for (const t of s.getVideoTracks()) t.enabled = !mutedVideo;
  }, [mutedVideo]);

  const _attachMicMeter = (stream: MediaStream) => {
    try {
      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteFrequencyData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i];
        setAudioLevel(sum / (data.length * 255));
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
    } catch {
      // No audio context — the meter just won't update. Not fatal.
    }
  };

  /**
   * Run a quick HTTP-level latency probe against the call server.
   * 5 sequential GETs to ``/api/health`` — the server's load-balancer
   * probe — averaging the round trip. Bandwidth probing is skipped
   * because PreJoin is supposed to take seconds, not minutes.
   *
   * Grade buckets are tuned for LAN where <5ms is the norm:
   *   excellent  < 20 ms
   *   good       < 60 ms
   *   fair       < 200 ms
   *   poor       >= 200 ms
   */
  const runNetworkTest = async () => {
    if (testing) return;
    setTesting(true);
    setNetTest(null);
    const tokens = (useAuthStore.getState() as any).tokens;
    const serverUrl =
      (useAuthStore.getState() as any).serverUrl ||
      'http://127.0.0.1:3000';
    const url = `${serverUrl}/api/health`;
    const samples: number[] = [];
    for (let i = 0; i < 5; i++) {
      const t0 = performance.now();
      try {
        const r = await fetch(url, {
          method: 'GET',
          headers: tokens?.access_token
            ? { Authorization: `Bearer ${tokens.access_token}` }
            : {},
          cache: 'no-store',
        });
        // Drain response body so the connection is fully released
        // — without this, keep-alive can mask the next request's
        // real latency.
        await r.text();
        samples.push(performance.now() - t0);
      } catch {
        samples.push(2000);  // treat failure as 2s
      }
    }
    samples.sort((a, b) => a - b);
    // Drop best + worst to reduce outlier impact.
    const trimmed = samples.length > 2 ? samples.slice(1, -1) : samples;
    const avg = trimmed.reduce((s, n) => s + n, 0) / trimmed.length;
    const grade =
      avg < 20 ? 'excellent' :
      avg < 60 ? 'good' :
      avg < 200 ? 'fair' : 'poor';
    setNetTest({ rtt_ms: Math.round(avg), grade });
    setTesting(false);
  };

  // Auto-run the test once when the screen mounts.
  useEffect(() => {
    void runNetworkTest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const _teardown = () => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => { /* ignore */ });
      audioCtxRef.current = null;
    }
    if (streamRef.current) {
      for (const t of streamRef.current.getTracks()) {
        try { t.stop(); } catch { /* ignore */ }
      }
      streamRef.current = null;
    }
  };

  const handleJoin = async () => {
    if (joining) return;
    setJoining(true);
    // Tear down preview FIRST so the camera handle is free for the
    // engine's getUserMedia call; on Windows a held handle errors
    // out the second acquisition.
    _teardown();
    try {
      switch (intent.kind) {
        case 'accept':
          await acceptCall();
          break;
        case 'initiate-1to1':
          await initiateCall(intent.targetId, intent.type);
          break;
        case 'initiate-group':
          await initiateGroupCall(intent.channelId, intent.type);
          break;
      }
      // Apply pre-join mute/video preferences on the now-active call.
      // The store sets isMuted/isVideoOff false by default; flip
      // them via the engine if the user toggled here.
      if (mutedAudio) {
        try { useCallStore.getState().toggleMute(); } catch { /* ignore */ }
      }
      if (mutedVideo && wantsVideo) {
        try { useCallStore.getState().toggleVideo(); } catch { /* ignore */ }
      }
      onJoined();
    } catch (err: any) {
      setError(err?.message || 'فشل الانضمام للمكالمة');
      setJoining(false);
    }
  };

  const audioInputs = devices.filter((d) => d.kind === 'audioinput');
  const videoInputs = devices.filter((d) => d.kind === 'videoinput');

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur
                    flex items-center justify-center p-4">
      <div className="bg-surface-900 rounded-xl border border-surface-700
                      w-full max-w-2xl shadow-2xl overflow-hidden">
        <div className="px-5 py-3 border-b border-surface-700 flex items-center justify-between">
          <h2 className="text-base font-semibold text-text-100">
            {title || 'الاستعداد للمكالمة'}
          </h2>
          <button
            onClick={onCancel}
            className="text-text-400 hover:text-text-100"
            title="إلغاء"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-5 grid md:grid-cols-2 gap-5">
          {/* Camera preview tile */}
          <div className="relative rounded-lg overflow-hidden bg-surface-950 aspect-video">
            {wantsVideo && !mutedVideo ? (
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex flex-col items-center justify-center text-text-500 text-sm">
                <CameraOff size={36} className="mb-2 opacity-50" />
                {wantsVideo ? 'الكاميرا مغلقة' : 'مكالمة صوتية فقط'}
              </div>
            )}

            {/* Mic level meter overlay (bottom of preview). */}
            <div className="absolute bottom-2 left-2 right-2 flex items-center gap-2 bg-black/40 rounded-full px-2 py-1">
              {mutedAudio ? (
                <MicOff size={12} className="text-red-400" />
              ) : (
                <Mic size={12} className="text-white" />
              )}
              <div className="flex-1 h-1 bg-white/20 rounded-full overflow-hidden">
                <div
                  className="h-full bg-green-400 transition-all duration-100"
                  style={{ width: `${Math.min(100, audioLevel * 200)}%` }}
                />
              </div>
            </div>
          </div>

          {/* Controls + device pickers */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <button
                onClick={() => setMutedAudio((v) => !v)}
                className={`flex-1 px-3 py-2 rounded-lg flex items-center justify-center gap-2 ${
                  mutedAudio
                    ? 'bg-red-600/80 text-white'
                    : 'bg-surface-800 text-text-100 hover:bg-surface-700'
                }`}
              >
                {mutedAudio ? <MicOff size={16} /> : <Mic size={16} />}
                {mutedAudio ? 'الميك مغلق' : 'الميك مفتوح'}
              </button>
              {wantsVideo && (
                <button
                  onClick={() => setMutedVideo((v) => !v)}
                  className={`flex-1 px-3 py-2 rounded-lg flex items-center justify-center gap-2 ${
                    mutedVideo
                      ? 'bg-red-600/80 text-white'
                      : 'bg-surface-800 text-text-100 hover:bg-surface-700'
                  }`}
                >
                  {mutedVideo ? <CameraOff size={16} /> : <Camera size={16} />}
                  {mutedVideo ? 'الكاميرا مغلقة' : 'الكاميرا مفتوحة'}
                </button>
              )}
            </div>

            {audioInputs.length > 1 && (
              <div>
                <label className="block text-xs text-text-400 mb-1">الميكروفون</label>
                <select
                  value={audioDevice}
                  onChange={(e) => setAudioDevice(e.target.value)}
                  className="w-full bg-surface-800 border border-surface-700
                             rounded-lg px-2 py-1.5 text-sm text-text-100"
                >
                  <option value="">افتراضي</option>
                  {audioInputs.map((d) => (
                    <option key={d.deviceId} value={d.deviceId}>
                      {d.label || d.deviceId.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {wantsVideo && videoInputs.length > 1 && (
              <div>
                <label className="block text-xs text-text-400 mb-1">الكاميرا</label>
                <select
                  value={videoDevice}
                  onChange={(e) => setVideoDevice(e.target.value)}
                  className="w-full bg-surface-800 border border-surface-700
                             rounded-lg px-2 py-1.5 text-sm text-text-100"
                >
                  <option value="">افتراضي</option>
                  {videoInputs.map((d) => (
                    <option key={d.deviceId} value={d.deviceId}>
                      {d.label || d.deviceId.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {error && (
              <div className="px-3 py-2 rounded bg-red-500/20 border border-red-500/40 text-xs text-red-200">
                {error}
              </div>
            )}

            {/* Network pre-flight banner */}
            <div className={`px-3 py-2 rounded text-xs flex items-center gap-2 ${
              !netTest
                ? 'bg-surface-800 text-text-400'
                : netTest.grade === 'excellent' || netTest.grade === 'good'
                  ? 'bg-green-500/20 text-green-200'
                  : netTest.grade === 'fair'
                    ? 'bg-yellow-500/20 text-yellow-200'
                    : 'bg-red-500/20 text-red-200'
            }`}>
              <Activity size={14} className="flex-shrink-0" />
              {testing ? (
                <span>قياس جودة الشبكة...</span>
              ) : netTest ? (
                <>
                  <span className="flex-1">
                    الشبكة:{' '}
                    {netTest.grade === 'excellent' ? 'ممتازة' :
                     netTest.grade === 'good' ? 'جيدة' :
                     netTest.grade === 'fair' ? 'متوسطة' : 'ضعيفة'}
                    {' '}({netTest.rtt_ms}ms)
                  </span>
                  <button
                    onClick={() => void runNetworkTest()}
                    className="text-[10px] px-2 py-0.5 rounded bg-black/30 hover:bg-black/50"
                  >
                    إعادة
                  </button>
                </>
              ) : (
                <span>لم يتم القياس</span>
              )}
            </div>
          </div>
        </div>

        <div className="px-5 py-3 border-t border-surface-700 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg text-sm text-text-300 hover:text-text-100"
            disabled={joining}
          >
            إلغاء
          </button>
          <button
            onClick={handleJoin}
            disabled={joining}
            className="px-5 py-2 rounded-lg bg-blue-600 hover:bg-blue-500
                       text-white text-sm font-medium flex items-center gap-2
                       disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <Phone size={14} />
            {joining ? 'جاري الانضمام...' : 'انضمام للمكالمة'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default PreJoinScreen;
