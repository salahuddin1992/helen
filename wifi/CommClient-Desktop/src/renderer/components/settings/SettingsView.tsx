import React, { useEffect, useRef, useState } from 'react';
import { useSettingsStore } from '@/stores/settings.store';
import { useAuthStore } from '@/stores/auth.store';
import { api } from '@/services/api.client';
import { socketManager } from '@/services/socket.manager';
import AuthorizedImage from '@/components/common/AuthorizedImage';
import type { ProfilePhoto, ProfilePhotoVisibility } from '@/types';
import {
  Mic,
  MicOff,
  Volume2,
  Camera,
  Moon,
  Sun,
  LogOut,
  Server,
  Edit,
  Check,
  X,
  Globe,
  Upload,
  Star,
  Trash2,
  Lock,
  Users as UsersIcon,
  Globe as PublicGlobe,
  Sparkles,
  Phone,
  Monitor,
  Bell,
} from 'lucide-react';
import { t, setLanguage } from '@/i18n';
import type { VideoResolution, VideoFrameRate, AudioSampleRate } from '@/types';
import { Handle } from '@/components/common/Handle';
import { PairPhoneDialog } from './PairPhoneDialog';
import { PairedSessionsList } from './PairedSessionsList';
import { ServerPicker } from './ServerPicker';
import { HealthCheck } from './HealthCheck';
import { useVirtualSourcesStore } from '@/stores/virtualSources.store';
import { NotificationSoundsPanel } from '@/components/settings/NotificationSoundsPanel';
import { QuickReactionsSettings } from '@/components/settings/QuickReactionsSettings';
import { PrivacyPanel } from '@/components/settings/PrivacyPanel';
import { KeyboardShortcutsPanel } from '@/components/settings/KeyboardShortcutsPanel';
import { CustomEmojiAdminPanel } from '@/components/admin/CustomEmojiAdminPanel';
import { isVirtualDeviceId } from '@/services/call/MediaDeviceManager';

// Ideal width/height per named profile. Browsers negotiate down to the
// closest supported mode — we read the real values via getSettings()
// after acquiring the stream so the UI shows truth, not wishful thinking.
// 'custom' falls back to user-entered dimensions at request time.
const RESOLUTION_MAP: Record<Exclude<VideoResolution, 'custom'>, { width: number; height: number }> = {
  '360p':  { width: 640,   height: 360 },
  '480p':  { width: 854,   height: 480 },
  '720p':  { width: 1280,  height: 720 },
  '1080p': { width: 1920,  height: 1080 },
  '1440p': { width: 2560,  height: 1440 },
  '4k':    { width: 3840,  height: 2160 },
  '5k':    { width: 5120,  height: 2880 },
  '8k':    { width: 7680,  height: 4320 },
};

const RESOLUTION_ORDER: VideoResolution[] = [
  '360p', '480p', '720p', '1080p', '1440p', '4k', '5k', '8k', 'custom',
];
const FRAME_RATE_ORDER: VideoFrameRate[] = [15, 24, 30, 60, 90, 120];
const SAMPLE_RATE_ORDER: AudioSampleRate[] = [8000, 16000, 24000, 32000, 44100, 48000, 96000];

const RESOLUTION_LABELS: Record<VideoResolution, string> = {
  '360p':  '360p (SD)',
  '480p':  '480p (SD)',
  '720p':  '720p (HD)',
  '1080p': '1080p (Full HD)',
  '1440p': '1440p (2K)',
  '4k':    '4K UHD',
  '5k':    '5K',
  '8k':    '8K UHD',
  'custom': '—',
};

interface AudioDevice {
  deviceId: string;
  label: string;
  kind: 'audioinput' | 'audiooutput';
}

interface VideoDevice {
  deviceId: string;
  label: string;
}

interface SessionRow {
  id: string;
  device_name: string | null;
  last_activity: string;
  is_active: boolean;
}

const SettingsView: React.FC = () => {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const serverUrl = useAuthStore((s) => s.serverUrl);
  const updateUser = useAuthStore((s) => s.updateUser);
  const settings = useSettingsStore((s) => s.settings);
  const updateSettings = useSettingsStore((s) => s.update);

  const [pairDialogOpen, setPairDialogOpen] = useState(false);
  const virtualSources = useVirtualSourcesStore((s) => s.sources);
  const [isEditingProfile, setIsEditingProfile] = useState(false);
  const [displayName, setDisplayName] = useState(user?.display_name || '');
  const [bio, setBio] = useState(user?.bio || '');
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);

  const [audioDevices, setAudioDevices] = useState<AudioDevice[]>([]);
  const [videoDevices, setVideoDevices] = useState<VideoDevice[]>([]);
  const [needsMicPermission, setNeedsMicPermission] = useState(false);

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  const [cameraPreview, setCameraPreview] = useState(false);
  const [testingAudio, setTestingAudio] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const previewStreamRef = useRef<MediaStream | null>(null);
  // Actual negotiated camera mode — populated after the stream is live.
  const [actualCamera, setActualCamera] = useState<{
    width: number; height: number; frameRate: number;
  } | null>(null);

  // Live microphone level meter state. 0..1, updated ~60fps via rAF.
  const [micTesting, setMicTesting] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const micStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);

  // Server media policy — loaded once on mount. When auto_max_quality is
  // true the server recommends (or forces, when combined with
  // enforce_hard_cap) that every client capture at the device's max.
  const [serverPolicy, setServerPolicy] = useState<{
    max_width: number;
    max_height: number;
    max_framerate: number;
    auto_max_quality: boolean;
    enforce_hard_cap: boolean;
  } | null>(null);
  // Probed device ceilings from MediaTrack getCapabilities().
  const [cameraCaps, setCameraCaps] = useState<{ width: number; height: number; frameRate: number } | null>(null);
  const [micCaps, setMicCaps] = useState<{ sampleRate: number; channelCount: number } | null>(null);
  const [probing, setProbing] = useState(false);
  // Auto-max is on when the user enabled it locally OR the server policy asks
  // for it. Server can't silently override a user who explicitly opted out —
  // enforce_hard_cap still clamps the actual resolution at the media-policy
  // service level, so the worst case is a slightly redundant capture. We
  // treat the server flag as a recommendation unless enforce_hard_cap is on.
  const autoMaxEffective =
    settings.autoMaxQuality ||
    Boolean(serverPolicy?.auto_max_quality && serverPolicy?.enforce_hard_cap);
  const autoMaxForced = Boolean(
    serverPolicy?.auto_max_quality && serverPolicy?.enforce_hard_cap,
  );

  const [socketConnected, setSocketConnected] = useState(socketManager.isConnected());
  const [appVersion, setAppVersion] = useState<string>('');

  // Keep profile fields in sync with the user object
  useEffect(() => {
    setDisplayName(user?.display_name || '');
    setBio(user?.bio || '');
  }, [user?.display_name, user?.bio]);

  // App version from Electron main process
  useEffect(() => {
    window.electronAPI?.getVersion?.().then(setAppVersion).catch(() => {});
  }, []);

  // Track socket connection for the Server Info section
  useEffect(() => {
    setSocketConnected(socketManager.isConnected());
    const offConnect = socketManager.on('connect', () => setSocketConnected(true));
    const offDisconnect = socketManager.on('disconnect', () => setSocketConnected(false));
    return () => {
      offConnect();
      offDisconnect();
    };
  }, []);

  // Enumerate devices. Labels are empty until the user has granted mic permission,
  // so try a silent probe first and keep the UI in sync on device changes.
  useEffect(() => {
    let cancelled = false;

    const enumerate = async () => {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        if (cancelled) return;
        const audio = devices
          .filter((d) => d.kind === 'audioinput' || d.kind === 'audiooutput')
          .map((d) => ({
            deviceId: d.deviceId,
            label: d.label,
            kind: d.kind as 'audioinput' | 'audiooutput',
          }));
        const video = devices
          .filter((d) => d.kind === 'videoinput')
          .map((d, idx) => ({
            deviceId: d.deviceId,
            label: d.label || `Camera ${idx + 1}`,
          }));
        // Append virtual sources (paired phone, etc.) so the dropdowns can pick them.
        const virtuals = Object.values(useVirtualSourcesStore.getState().sources);
        const virtualAudio = virtuals
          .filter((v) => v.kind === 'audioinput')
          .map((v) => ({ deviceId: v.deviceId, label: v.label, kind: 'audioinput' as const }));
        const virtualVideo = virtuals
          .filter((v) => v.kind === 'videoinput')
          .map((v) => ({ deviceId: v.deviceId, label: v.label }));
        setAudioDevices([...audio, ...virtualAudio]);
        setVideoDevices([...video, ...virtualVideo]);
        // If every audio input label is empty, we lack permission
        const allEmpty = audio.filter((a) => a.kind === 'audioinput').every((a) => !a.label);
        setNeedsMicPermission(allEmpty && audio.some((a) => a.kind === 'audioinput'));
      } catch (error) {
        console.error('[Settings] enumerateDevices failed', error);
      }
    };

    enumerate();
    navigator.mediaDevices.addEventListener('devicechange', enumerate);
    return () => {
      cancelled = true;
      navigator.mediaDevices.removeEventListener('devicechange', enumerate);
    };
  }, [virtualSources]);

  // Load active sessions
  useEffect(() => {
    let cancelled = false;
    setSessionsLoading(true);
    api
      .listSessions()
      .then((data: any) => {
        if (cancelled) return;
        setSessions(data?.sessions || []);
      })
      .catch(() => {
        if (cancelled) return;
        setSessions([]);
      })
      .finally(() => {
        if (!cancelled) setSessionsLoading(false);
      });
    return () => { cancelled = true; };
  }, [user?.id]);

  // Stop any live preview stream when the component unmounts
  useEffect(() => () => {
    stopPreviewStream();
    stopMicTest();
  }, []);

  // Fetch server media policy once so we know whether to force auto-max.
  useEffect(() => {
    let cancelled = false;
    api.getMyMediaCap()
      .then((res) => {
        if (cancelled) return;
        const cap = res.cap;
        setServerPolicy({
          max_width: cap.max_width,
          max_height: cap.max_height,
          max_framerate: cap.max_framerate,
          auto_max_quality: !!cap.auto_max_quality,
          enforce_hard_cap: !!cap.enforce_hard_cap,
        });
      })
      .catch((e) => {
        // Non-fatal — client keeps working with local-only settings.
        console.warn('[Settings] media policy fetch failed', e);
      });
    return () => { cancelled = true; };
  }, []);

  // Probe each device's capabilities via getUserMedia → getCapabilities().
  // We only probe when auto-max is effective so we don't surprise the user
  // with a permission prompt they didn't ask for. The probe stops its own
  // track immediately — the numbers persist in state, not a live stream.
  useEffect(() => {
    if (!autoMaxEffective) {
      setCameraCaps(null);
      setMicCaps(null);
      return;
    }
    let cancelled = false;
    const run = async () => {
      setProbing(true);
      try {
        // Camera probe
        try {
          // Virtual deviceIds (paired phone, USB iPhone) don't exist in
          // navigator.mediaDevices — passing them to getUserMedia throws
          // OverconstrainedError. Probe the default camera instead.
          const vConstraints: MediaTrackConstraints =
            settings.videoInputDevice && !isVirtualDeviceId(settings.videoInputDevice)
              ? { deviceId: { exact: settings.videoInputDevice } }
              : {};
          const vStream = await navigator.mediaDevices.getUserMedia({ video: vConstraints });
          const vTrack = vStream.getVideoTracks()[0];
          const vCaps = (vTrack as any).getCapabilities?.() ?? {};
          vTrack.stop();
          if (!cancelled) {
            setCameraCaps({
              width:     Number(vCaps.width?.max ?? 0) || 0,
              height:    Number(vCaps.height?.max ?? 0) || 0,
              frameRate: Number(vCaps.frameRate?.max ?? 0) || 0,
            });
          }
        } catch (e) {
          if (!cancelled) setCameraCaps(null);
        }
        // Mic probe
        try {
          const aConstraints: MediaTrackConstraints =
            settings.audioInputDevice && !isVirtualDeviceId(settings.audioInputDevice)
              ? { deviceId: { exact: settings.audioInputDevice } }
              : {};
          const aStream = await navigator.mediaDevices.getUserMedia({ audio: aConstraints });
          const aTrack = aStream.getAudioTracks()[0];
          const aCaps = (aTrack as any).getCapabilities?.() ?? {};
          aTrack.stop();
          if (!cancelled) {
            setMicCaps({
              sampleRate:   Number(aCaps.sampleRate?.max ?? 0) || 0,
              channelCount: Number(aCaps.channelCount?.max ?? 0) || 0,
            });
          }
        } catch (e) {
          if (!cancelled) setMicCaps(null);
        }
      } finally {
        if (!cancelled) setProbing(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, [autoMaxEffective, settings.videoInputDevice, settings.audioInputDevice]);

  const stopPreviewStream = () => {
    previewStreamRef.current?.getTracks().forEach((trk) => trk.stop());
    previewStreamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setActualCamera(null);
  };

  const stopMicTest = () => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    try { analyserRef.current?.disconnect(); } catch { /* already disconnected */ }
    analyserRef.current = null;
    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    micStreamRef.current?.getTracks().forEach((t) => t.stop());
    micStreamRef.current = null;
    setMicLevel(0);
    setMicTesting(false);
  };

  const startMicTest = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: buildAudioConstraints(),
      });
      micStreamRef.current = stream;

      const Ctx = window.AudioContext || (window as any).webkitAudioContext;
      const ctx = new Ctx();
      audioCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0.8;
      analyserRef.current = analyser;
      source.connect(analyser);

      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        if (!analyserRef.current) return;
        analyserRef.current.getByteTimeDomainData(buf);
        // RMS over the waveform — centered at 128, range 0..128.
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / buf.length);
        // Apply mic gain as a soft multiplier for display only.
        const scaled = Math.min(1, rms * 2 * (settings.microphoneGain / 100));
        setMicLevel(scaled);
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
      setMicTesting(true);
      setNeedsMicPermission(false);
    } catch (error) {
      console.error('[Settings] mic test failed', error);
      stopMicTest();
    }
  };

  const buildAudioConstraints = (): MediaTrackConstraints => ({
    deviceId: settings.audioInputDevice
      ? { exact: settings.audioInputDevice } : undefined,
    echoCancellation: settings.echoCancellation,
    noiseSuppression: settings.noiseSuppression,
    autoGainControl: settings.autoGainControl,
    // Use the mic's detected max rate when auto-max is on — falls back to
    // the manually chosen preset if the probe failed or returned nothing.
    sampleRate: (autoMaxEffective && micCaps?.sampleRate)
      ? micCaps.sampleRate
      : settings.audioSampleRate,
  });

  // Clamp helper: respects server's hard cap when enforce_hard_cap is on so
  // the client doesn't waste cycles capturing above what the server accepts.
  const clampToServer = (w: number, h: number, fps: number) => {
    if (!serverPolicy || !serverPolicy.enforce_hard_cap) return { w, h, fps };
    return {
      w:   Math.min(w,   serverPolicy.max_width),
      h:   Math.min(h,   serverPolicy.max_height),
      fps: Math.min(fps, serverPolicy.max_framerate),
    };
  };

  const resolvedResolution = (): { width: number; height: number } => {
    if (autoMaxEffective && cameraCaps && cameraCaps.width && cameraCaps.height) {
      const { w, h } = clampToServer(cameraCaps.width, cameraCaps.height, 0);
      return { width: w, height: h };
    }
    if (settings.videoResolution === 'custom') {
      return {
        width:  Math.max(16, settings.customVideoWidth | 0),
        height: Math.max(16, settings.customVideoHeight | 0),
      };
    }
    return RESOLUTION_MAP[settings.videoResolution];
  };

  const resolvedFrameRate = (): number => {
    if (autoMaxEffective && cameraCaps && cameraCaps.frameRate) {
      const { fps } = clampToServer(0, 0, cameraCaps.frameRate);
      return fps || cameraCaps.frameRate;
    }
    return settings.useCustomFrameRate
      ? Math.max(1, settings.customVideoFrameRate | 0)
      : settings.videoFrameRate;
  };

  const handleGrantMicPermission = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((trk) => trk.stop());
      // Re-enumerate so labels show up
      const devices = await navigator.mediaDevices.enumerateDevices();
      setAudioDevices(
        devices
          .filter((d) => d.kind === 'audioinput' || d.kind === 'audiooutput')
          .map((d) => ({
            deviceId: d.deviceId,
            label: d.label,
            kind: d.kind as 'audioinput' | 'audiooutput',
          })),
      );
      setNeedsMicPermission(false);
    } catch (error) {
      console.error('[Settings] mic permission denied', error);
    }
  };

  const handleTestAudio = async () => {
    setTestingAudio(true);
    try {
      const audioContext = new (window.AudioContext ||
        (window as any).webkitAudioContext)();
      const oscillator = audioContext.createOscillator();
      const gainNode = audioContext.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(audioContext.destination);

      oscillator.frequency.value = 440;
      // Scale test tone by user's speaker-volume preference so they
      // can calibrate against the actual output level they'll hear.
      const peak = 0.3 * (settings.speakerVolume / 100);
      gainNode.gain.setValueAtTime(peak, audioContext.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(
        0.01,
        audioContext.currentTime + 0.5,
      );

      oscillator.start(audioContext.currentTime);
      oscillator.stop(audioContext.currentTime + 0.5);

      setTimeout(() => {
        setTestingAudio(false);
        audioContext.close().catch(() => {});
      }, 600);
    } catch (error) {
      console.error('[Settings] test audio failed', error);
      setTestingAudio(false);
    }
  };

  const handleCameraPreview = async () => {
    if (cameraPreview) {
      stopPreviewStream();
      setCameraPreview(false);
      return;
    }

    const { width, height } = resolvedResolution();
    const frameRate = resolvedFrameRate();

    const videoConstraints: MediaTrackConstraints = {
      width:  { ideal: width },
      height: { ideal: height },
      frameRate: { ideal: frameRate },
    };
    if (settings.videoInputDevice) {
      videoConstraints.deviceId = { exact: settings.videoInputDevice };
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints,
      });
      previewStreamRef.current = stream;
      setCameraPreview(true);

      // Read back what the browser actually negotiated. On unsupported
      // modes the camera will have dropped to its nearest supported
      // mode — surface that to the user so the "Actual" line is truth.
      const track = stream.getVideoTracks()[0];
      if (track) {
        const s = track.getSettings();
        setActualCamera({
          width: s.width || 0,
          height: s.height || 0,
          frameRate: Math.round(s.frameRate || 0),
        });
      }

      // The <video> element is rendered when cameraPreview flips true,
      // so attach the stream on the next tick.
      queueMicrotask(() => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
        }
      });
    } catch (error) {
      console.error('[Settings] camera preview failed', error);
      stopPreviewStream();
      setCameraPreview(false);
    }
  };

  const handleSaveProfile = async () => {
    setSavingProfile(true);
    setProfileError(null);
    try {
      const payload: Record<string, any> = {};
      if (displayName !== user?.display_name) payload.display_name = displayName;
      if (bio !== (user?.bio || '')) payload.bio = bio;

      if (Object.keys(payload).length > 0) {
        const updated = await api.updateMe(payload);
        updateUser({
          display_name: updated.display_name ?? displayName,
          bio: updated.bio ?? bio,
        });
      }
      setIsEditingProfile(false);
    } catch (error: any) {
      setProfileError(error?.message || t('settings.save_error'));
    } finally {
      setSavingProfile(false);
    }
  };

  const handleRevokeSession = async (sessionId: string) => {
    try {
      await api.revokeSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
    } catch (error) {
      console.error('[Settings] revoke session failed', error);
    }
  };

  const handleLogout = () => {
    if (window.confirm(t('settings.confirm_logout'))) {
      logout();
    }
  };

  return (
    <div className="w-full h-full bg-surface-950 overflow-y-auto">
      <div className="max-w-2xl mx-auto py-6 px-4">
        <h1 className="text-3xl font-bold text-text-100 mb-8">
          {t('settings.title')}
        </h1>

        {/* Profile Section */}
        <Section title={t('settings.profile')} icon={<Edit size={20} />}>
          <ProfilePhotosGallery userId={user?.id} onPrimaryChange={(url) => updateUser({ avatar_url: url })} />
          <div className="h-5" />
          <div className="space-y-4">
            {isEditingProfile ? (
              <>
                <div>
                  <label className="block text-sm font-medium text-text-200 mb-2">
                    {t('settings.display_name')}
                  </label>
                  <input
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-text-200 mb-2">
                    {t('settings.bio')}
                  </label>
                  <textarea
                    value={bio}
                    onChange={(e) => setBio(e.target.value)}
                    rows={3}
                    className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50 resize-none"
                  />
                </div>

                {profileError && (
                  <p className="text-sm text-red-400">{profileError}</p>
                )}

                <div className="flex gap-3">
                  <button
                    onClick={handleSaveProfile}
                    disabled={savingProfile}
                    className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 text-white rounded-lg font-medium transition-colors"
                  >
                    <Check size={16} />
                    {savingProfile ? t('common.loading') : t('common.save')}
                  </button>
                  <button
                    onClick={() => {
                      setDisplayName(user?.display_name || '');
                      setBio(user?.bio || '');
                      setProfileError(null);
                      setIsEditingProfile(false);
                    }}
                    disabled={savingProfile}
                    className="flex items-center gap-2 px-4 py-2 bg-surface-800 hover:bg-surface-700 disabled:opacity-60 text-text-100 rounded-lg font-medium transition-colors"
                  >
                    <X size={16} />
                    {t('common.cancel')}
                  </button>
                </div>
              </>
            ) : (
              <div className="flex items-start gap-4">
                <div className="w-20 h-20 rounded-full overflow-hidden bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-2xl font-bold text-white flex-shrink-0">
                  {user?.avatar_url ? (
                    <AuthorizedImage
                      path={user.avatar_url}
                      alt=""
                      className="w-full h-full object-cover"
                      fallback={
                        <span>{user?.display_name?.charAt(0)?.toUpperCase() || '?'}</span>
                      }
                    />
                  ) : (
                    <span>{user?.display_name?.charAt(0)?.toUpperCase() || '?'}</span>
                  )}
                </div>
                <div className="flex-1">
                  <p className="text-lg font-semibold text-text-100">
                    {user?.display_name || ''}
                  </p>
                  <Handle user={user as any} className="text-sm text-text-500 block" />
                  {bio && <p className="text-sm text-text-400 mt-2">{bio}</p>}
                </div>
                <button
                  onClick={() => setIsEditingProfile(true)}
                  className="p-2 rounded-lg hover:bg-surface-800 text-blue-400 transition-colors"
                  aria-label={t('common.save')}
                >
                  <Edit size={18} />
                </button>
              </div>
            )}

            {/* Change password — separate row so it never collides with
                the inline profile-edit form above. The whole flow lives
                in <ChangePasswordRow/> for testability and to keep this
                component readable. */}
            <ChangePasswordRow />
          </div>
        </Section>

        {/* Audio Section */}
        <Section title={t('settings.audio')} icon={<Mic size={20} />}>
          <div className="space-y-4">
            {needsMicPermission && (
              <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30 text-sm text-yellow-200 flex items-center justify-between gap-3">
                <span>{t('settings.grant_mic_for_labels')}</span>
                <button
                  onClick={handleGrantMicPermission}
                  className="px-3 py-1 bg-yellow-600/80 hover:bg-yellow-600 text-white rounded text-xs font-medium transition-colors"
                >
                  {t('common.confirm')}
                </button>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.microphone')}
              </label>
              <select
                value={settings.audioInputDevice || ''}
                onChange={(e) => updateSettings({ audioInputDevice: e.target.value })}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                {audioDevices
                  .filter((d) => d.kind === 'audioinput')
                  .map((device, idx) => (
                    <option key={device.deviceId || `mic-${idx}`} value={device.deviceId}>
                      {device.label || `Microphone ${idx + 1}`}
                    </option>
                  ))}
              </select>
            </div>

            {/* Live mic level meter + test toggle */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm font-medium text-text-200">
                  {t('settings.mic_level')}
                </label>
                <button
                  onClick={() => (micTesting ? stopMicTest() : startMicTest())}
                  className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                    micTesting
                      ? 'bg-red-600 hover:bg-red-700 text-white'
                      : 'bg-blue-600 hover:bg-blue-700 text-white'
                  }`}
                >
                  {micTesting ? <MicOff size={14} /> : <Mic size={14} />}
                  {micTesting ? t('common.cancel') : t('settings.mic_level')}
                </button>
              </div>
              <MicLevelBar level={micLevel} active={micTesting} />
              <p className="mt-1 text-xs text-text-500">
                {micTesting ? t('settings.mic_level_hint') : t('settings.no_signal')}
              </p>
            </div>

            {/* Mic gain slider */}
            <SliderRow
              label={t('settings.microphone_gain')}
              value={settings.microphoneGain}
              onChange={(v) => updateSettings({ microphoneGain: v })}
            />

            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.speaker')}
              </label>
              <select
                value={settings.audioOutputDevice || ''}
                onChange={(e) => updateSettings({ audioOutputDevice: e.target.value })}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                {audioDevices
                  .filter((d) => d.kind === 'audiooutput')
                  .map((device, idx) => (
                    <option key={device.deviceId || `out-${idx}`} value={device.deviceId}>
                      {device.label || `Speaker ${idx + 1}`}
                    </option>
                  ))}
              </select>
            </div>

            {/* Speaker volume slider */}
            <SliderRow
              label={t('settings.speaker_volume')}
              value={settings.speakerVolume}
              onChange={(v) => updateSettings({ speakerVolume: v })}
            />

            {/* Sample rate — advanced but exposed on request */}
            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.sample_rate')}
              </label>
              <select
                value={settings.audioSampleRate}
                onChange={(e) => updateSettings({ audioSampleRate: Number(e.target.value) as AudioSampleRate })}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                {SAMPLE_RATE_ORDER.map((rate) => (
                  <option key={rate} value={rate}>{rate.toLocaleString()} Hz</option>
                ))}
              </select>
            </div>

            {/* Audio processing toggles */}
            <div className="pt-3 border-t border-surface-800 space-y-2">
              <p className="text-xs font-semibold text-text-300 uppercase tracking-wide mb-2">
                {t('settings.processing')}
              </p>
              <ToggleRow
                label={t('settings.echo_cancellation')}
                checked={settings.echoCancellation}
                onChange={(v) => updateSettings({ echoCancellation: v })}
              />
              <ToggleRow
                label={t('settings.noise_suppression')}
                checked={settings.noiseSuppression}
                onChange={(v) => updateSettings({ noiseSuppression: v })}
              />
              <ToggleRow
                label={t('settings.auto_gain')}
                checked={settings.autoGainControl}
                onChange={(v) => updateSettings({ autoGainControl: v })}
              />
            </div>

            <button
              onClick={handleTestAudio}
              disabled={testingAudio}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 text-white rounded-lg font-medium transition-colors flex items-center gap-2"
            >
              <Volume2 size={16} />
              {testingAudio ? t('settings.testing') : t('settings.test_audio')}
            </button>
          </div>
        </Section>

        {/* Video Section */}
        <Section title={t('settings.video')} icon={<Camera size={20} />}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.camera')}
              </label>
              <select
                value={settings.videoInputDevice || ''}
                onChange={(e) => updateSettings({ videoInputDevice: e.target.value })}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                <option value="">—</option>
                {videoDevices.map((device, idx) => (
                  <option key={device.deviceId || `cam-${idx}`} value={device.deviceId}>
                    {device.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => setPairDialogOpen(true)}
                className="mt-2 inline-flex items-center gap-2 px-3 py-1.5 text-sm rounded-md bg-surface-800 hover:bg-surface-700 text-text-100 border border-surface-700"
              >
                <Phone size={14} />
                {t('pair.button')}
              </button>
              <PairedSessionsList />
            </div>

            {/* Auto-max quality toggle + detected caps readout */}
            <div className="p-3 rounded-lg bg-surface-800/40 border border-surface-800 space-y-2">
              <ToggleRow
                label={`${t('settings.auto_max_quality')}${autoMaxForced ? ` (${t('settings.forced_by_server')})` : ''}`}
                icon={<Sparkles size={14} />}
                checked={autoMaxEffective}
                onChange={(v) => {
                  if (autoMaxForced) return;
                  updateSettings({ autoMaxQuality: v });
                }}
              />
              <p className="text-xs text-text-500">{t('settings.auto_max_quality_hint')}</p>
              {autoMaxEffective && (
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded bg-surface-900 px-2 py-1.5">
                    <span className="text-text-500">{t('settings.detected_camera')}: </span>
                    <span className="font-mono text-text-100">
                      {probing
                        ? '…'
                        : cameraCaps && cameraCaps.width
                          ? `${cameraCaps.width}×${cameraCaps.height} @ ${cameraCaps.frameRate || '?'} fps`
                          : t('settings.not_detected')}
                    </span>
                  </div>
                  <div className="rounded bg-surface-900 px-2 py-1.5">
                    <span className="text-text-500">{t('settings.detected_mic')}: </span>
                    <span className="font-mono text-text-100">
                      {probing
                        ? '…'
                        : micCaps && micCaps.sampleRate
                          ? `${micCaps.sampleRate.toLocaleString()} Hz${micCaps.channelCount ? ` · ${micCaps.channelCount}ch` : ''}`
                          : t('settings.not_detected')}
                    </span>
                  </div>
                </div>
              )}
            </div>

            {/* Resolution preset */}
            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.resolution')}
              </label>
              <select
                value={settings.videoResolution}
                onChange={(e) => updateSettings({ videoResolution: e.target.value as VideoResolution })}
                disabled={autoMaxEffective}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50 disabled:opacity-50"
              >
                {RESOLUTION_ORDER.map((r) => (
                  <option key={r} value={r}>
                    {r === 'custom'
                      ? t('settings.custom_resolution')
                      : RESOLUTION_LABELS[r]}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-text-500">{t('settings.resolution_hint')}</p>
            </div>

            {/* Custom W/H inputs — only when preset === 'custom' */}
            {settings.videoResolution === 'custom' && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-text-300 mb-1">
                    {t('settings.custom_width')}
                  </label>
                  <input
                    type="number"
                    min={16}
                    max={16384}
                    step={2}
                    value={settings.customVideoWidth}
                    onChange={(e) => updateSettings({
                      customVideoWidth: Math.max(16, Number(e.target.value) || 0),
                    })}
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-text-300 mb-1">
                    {t('settings.custom_height')}
                  </label>
                  <input
                    type="number"
                    min={16}
                    max={16384}
                    step={2}
                    value={settings.customVideoHeight}
                    onChange={(e) => updateSettings({
                      customVideoHeight: Math.max(16, Number(e.target.value) || 0),
                    })}
                    className="w-full px-3 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  />
                </div>
              </div>
            )}

            {/* Frame rate preset + custom override */}
            <div>
              <label className="block text-sm font-medium text-text-200 mb-2">
                {t('settings.frame_rate')}
              </label>
              <select
                value={settings.useCustomFrameRate ? 'custom' : String(settings.videoFrameRate)}
                onChange={(e) => {
                  if (e.target.value === 'custom') {
                    updateSettings({ useCustomFrameRate: true });
                  } else {
                    updateSettings({
                      useCustomFrameRate: false,
                      videoFrameRate: Number(e.target.value) as VideoFrameRate,
                    });
                  }
                }}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                {FRAME_RATE_ORDER.map((fps) => (
                  <option key={fps} value={fps}>{fps} fps</option>
                ))}
                <option value="custom">{t('settings.custom_fps')}</option>
              </select>
            </div>

            {settings.useCustomFrameRate && (
              <div>
                <label className="block text-xs font-medium text-text-300 mb-1">
                  {t('settings.custom_fps')}
                </label>
                <input
                  type="number"
                  min={1}
                  max={240}
                  step={1}
                  value={settings.customVideoFrameRate}
                  onChange={(e) => updateSettings({
                    customVideoFrameRate: Math.max(1, Number(e.target.value) || 0),
                  })}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                />
              </div>
            )}

            {/* Mirror toggle */}
            <ToggleRow
              label={t('settings.mirror_camera')}
              checked={settings.mirrorCamera}
              onChange={(v) => updateSettings({ mirrorCamera: v })}
            />

            <div>
              <button
                onClick={handleCameraPreview}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors"
              >
                {cameraPreview
                  ? t('settings.stop_preview')
                  : t('settings.preview_camera')}
              </button>

              {cameraPreview && (
                <div className="mt-4 space-y-2">
                  <div className="rounded-lg overflow-hidden bg-black">
                    <video
                      ref={videoRef}
                      autoPlay
                      playsInline
                      muted
                      className="w-full aspect-video object-cover"
                      style={settings.mirrorCamera
                        ? { transform: 'scaleX(-1)' }
                        : undefined}
                    />
                  </div>
                  {actualCamera && <ActualCameraInfo
                    actual={actualCamera}
                    requested={{ ...resolvedResolution(), frameRate: resolvedFrameRate() }}
                  />}
                </div>
              )}
            </div>
          </div>
        </Section>

        {/* Appearance Section */}
        <Section title={t('settings.appearance')} icon={<Moon size={20} />}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text-200 mb-3">
                {t('settings.theme')}
              </label>
              <div className="flex gap-3 flex-wrap">
                {[
                  { value: 'dark',   label: t('settings.dark'),   icon: <Moon size={18} /> },
                  { value: 'light',  label: t('settings.light'),  icon: <Sun size={18} /> },
                  { value: 'system', label: t('settings.system') || 'System', icon: <Monitor size={18} /> },
                ].map((option) => (
                  <button
                    key={option.value}
                    onClick={() => updateSettings({ theme: option.value as 'dark' | 'light' | 'system' })}
                    className={`flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all ${
                      settings.theme === option.value
                        ? 'bg-blue-600 text-white'
                        : 'bg-surface-800 text-text-200 hover:bg-surface-700'
                    }`}
                  >
                    {option.icon}
                    {option.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-text-200 mb-2 flex items-center gap-2">
                <Globe size={14} />
                {t('settings.language')}
              </label>
              <select
                value={settings.language}
                onChange={(e) => {
                  const lang = e.target.value as 'en' | 'ar';
                  updateSettings({ language: lang });
                  setLanguage(lang);
                }}
                className="w-full px-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              >
                <option value="en">English</option>
                <option value="ar">العربية</option>
              </select>
            </div>
          </div>
        </Section>

        {/* Notifications & Do Not Disturb */}
        <Section title={t('notifications.dnd')} icon={<Bell size={20} />}>
          <DndPicker />
          <div className="mt-4">
            <NotificationSoundsPanel />
          </div>
          <div className="mt-4">
            <QuickReactionsSettings />
          </div>
          <div className="mt-4">
            <PrivacyPanel />
          </div>
          <div className="mt-4">
            <KeyboardShortcutsPanel />
          </div>
          {/* Custom emoji administration — server enforces the
              admin role, but we hide it from non-admins for clarity. */}
          {user?.role === 'admin' && (
            <div className="mt-4">
              <CustomEmojiAdminPanel />
            </div>
          )}
        </Section>

        {/* Server Info Section — interactive picker, discovery, reconnect */}
        <Section title={t('settings.server_info')} icon={<Server size={20} />}>
          <div className="space-y-4">
            <ServerPicker />
            <div className="pt-2 border-t border-surface-800 space-y-3">
              <InfoRow label={t('settings.app_version')} value={appVersion || '—'} />
              <ClientNameEditor />
              {user?.role === 'admin' && <ServerNameEditor />}
            </div>
          </div>
        </Section>

        {/* Diagnostics — periodic checks for server, video, audio, chat */}
        <Section title="Diagnostics" icon={<Sparkles size={20} />}>
          <HealthCheck />
        </Section>

        {/* Account Section */}
        <Section title={t('settings.account')} icon={<LogOut size={20} />}>
          <div className="space-y-4">
            <div>
              <h3 className="text-sm font-semibold text-text-200 mb-3">
                {t('settings.active_sessions')}
              </h3>
              <div className="space-y-2">
                {sessionsLoading && (
                  <p className="text-sm text-text-500">{t('common.loading')}</p>
                )}
                {!sessionsLoading && sessions.length === 0 && (
                  <div className="p-3 rounded-lg bg-surface-900 border border-surface-800">
                    <p className="font-medium text-text-100">
                      {t('settings.this_device')}
                    </p>
                    <p className="text-xs text-text-500">
                      {t('settings.last_active')}: {new Date().toLocaleString()}
                    </p>
                  </div>
                )}
                {sessions.map((session) => (
                  <div
                    key={session.id}
                    className="p-3 rounded-lg bg-surface-900 border border-surface-800 flex items-start justify-between gap-3"
                  >
                    <div>
                      <p className="font-medium text-text-100">
                        {session.device_name || t('settings.this_device')}
                      </p>
                      <p className="text-xs text-text-500">
                        {t('settings.last_active')}:{' '}
                        {new Date(session.last_activity).toLocaleString()}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {session.is_active && (
                        <span className="px-2 py-1 text-xs bg-green-500/20 text-green-400 rounded">
                          {t('settings.current')}
                        </span>
                      )}
                      {!session.is_active && (
                        <button
                          onClick={() => handleRevokeSession(session.id)}
                          className="px-2 py-1 text-xs bg-red-500/20 hover:bg-red-500/30 text-red-300 rounded transition-colors"
                        >
                          {t('settings.revoke')}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <button
              onClick={handleLogout}
              className="w-full px-4 py-3 bg-red-600 hover:bg-red-700 text-white rounded-lg font-medium transition-colors flex items-center justify-center gap-2"
            >
              <LogOut size={18} />
              {t('settings.logout')}
            </button>
          </div>
        </Section>

        <div className="h-8" />
      </div>

      <PairPhoneDialog isOpen={pairDialogOpen} onClose={() => setPairDialogOpen(false)} />
    </div>
  );
};

const Section: React.FC<{
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}> = ({ title, icon, children }) => (
  <div className="mb-8 p-6 bg-surface-900 border border-surface-800 rounded-xl">
    <h2 className="flex items-center gap-3 text-lg font-semibold text-text-100 mb-6">
      {icon}
      {title}
    </h2>
    {children}
  </div>
);

const InfoRow: React.FC<{ label: string; value: React.ReactNode }> = ({
  label,
  value,
}) => (
  <div className="flex items-center justify-between py-2 border-b border-surface-800 last:border-0">
    <span className="text-sm text-text-400">{label}</span>
    <span className="text-sm font-medium text-text-100">{value}</span>
  </div>
);

// ── Shared UI primitives for media controls ─────────────

const MicLevelBar: React.FC<{ level: number; active: boolean }> = ({ level, active }) => {
  // 20 segments so the bar reads as clearly-quantized, matching the
  // LED-style meters users are used to in OBS/Audacity. Dim when idle.
  const pct = Math.min(1, Math.max(0, level));
  const segments = 20;
  const filled = Math.round(pct * segments);
  return (
    <div className="flex gap-0.5 h-4">
      {Array.from({ length: segments }).map((_, i) => {
        const on = active && i < filled;
        const hot = i >= segments * 0.75;
        const warn = i >= segments * 0.5 && i < segments * 0.75;
        const color = on
          ? (hot ? 'bg-red-500' : warn ? 'bg-yellow-500' : 'bg-green-500')
          : 'bg-surface-800';
        return <div key={i} className={`flex-1 rounded-sm ${color}`} />;
      })}
    </div>
  );
};

const SliderRow: React.FC<{
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
}> = ({ label, value, onChange, min = 0, max = 100 }) => (
  <div>
    <div className="flex items-center justify-between mb-2">
      <label className="text-sm font-medium text-text-200">{label}</label>
      <span className="text-xs font-mono text-text-400">{value}%</span>
    </div>
    <input
      type="range"
      min={min}
      max={max}
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className="w-full accent-blue-500"
    />
  </div>
);

const ToggleRow: React.FC<{
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  icon?: React.ReactNode;
}> = ({ label, checked, onChange, icon }) => (
  <label className="flex items-center justify-between py-1.5 cursor-pointer select-none">
    <span className="flex items-center gap-2 text-sm text-text-200">
      {icon}
      {label}
    </span>
    <button
      type="button"
      onClick={() => onChange(!checked)}
      role="switch"
      aria-checked={checked}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        checked ? 'bg-blue-600' : 'bg-surface-700'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 bg-white rounded-full shadow transform transition-transform ${
          checked ? 'translate-x-4' : 'translate-x-0.5'
        }`}
      />
    </button>
  </label>
);

const ActualCameraInfo: React.FC<{
  actual:    { width: number; height: number; frameRate: number };
  requested: { width: number; height: number; frameRate: number };
}> = ({ actual, requested }) => {
  // Accept a small FPS delta — browsers rarely match exactly.
  const downgraded =
    actual.width < requested.width ||
    actual.height < requested.height ||
    Math.abs(actual.frameRate - requested.frameRate) > 3;
  return (
    <div className="flex items-center justify-between text-xs rounded-md bg-surface-800/60 px-3 py-2">
      <span className="text-text-400">{t('settings.camera_actual')}:</span>
      <span className="font-mono text-text-100">
        {actual.width}×{actual.height} @ {actual.frameRate || '?'} fps
      </span>
      {downgraded && (
        <span className="ml-2 text-yellow-400">{t('settings.not_supported')}</span>
      )}
    </div>
  );
};

// ── Client Name Editor (local — all users) ─────────────

const ClientNameEditor: React.FC = () => {
  const [name, setName] = useState('');
  const [initial, setInitial] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    const api = window.electronAPI?.getDisplayName;
    if (!api) {
      setLoading(false);
      return;
    }
    api()
      .then((current) => {
        if (!alive) return;
        setName(current || '');
        setInitial(current || '');
      })
      .catch(() => {})
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === initial) return;
    setSaving(true);
    setMessage(null);
    try {
      const res = await window.electronAPI!.setDisplayName(trimmed);
      if (res?.success && res.name) {
        setInitial(res.name);
        setName(res.name);
        setMessage({ kind: 'ok', text: t('settings.client_name_saved') });
      } else {
        setMessage({ kind: 'err', text: res?.error || t('settings.client_name_save_failed') });
      }
    } catch (e: any) {
      setMessage({ kind: 'err', text: e?.message || t('settings.client_name_save_failed') });
    } finally {
      setSaving(false);
    }
  };

  if (loading) return null;
  if (!window.electronAPI?.setDisplayName) return null;

  const dirty = name.trim() !== initial && name.trim().length > 0;

  return (
    <div className="pt-3 border-t border-surface-800">
      <label className="block text-sm text-text-300 mb-2">{t('settings.client_name')}</label>
      <div className="flex gap-2">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={64}
          disabled={saving}
          className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 focus:outline-none focus:border-blue-500 disabled:opacity-60"
        />
        <button
          onClick={save}
          disabled={!dirty || saving}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-white text-sm font-medium transition-colors"
        >
          {saving ? t('common.saving') : t('common.save')}
        </button>
      </div>
      {message && (
        <p className={`mt-2 text-xs ${message.kind === 'ok' ? 'text-green-400' : 'text-red-400'}`}>
          {message.text}
        </p>
      )}
      <p className="mt-2 text-xs text-text-500">{t('settings.client_name_hint')}</p>
    </div>
  );
};

// ── Server Name Editor (admin-only) ────────────────────

const ServerNameEditor: React.FC = () => {
  const [name, setName] = useState('');
  const [initial, setInitial] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .getServerConfig()
      .then((res) => {
        if (!alive) return;
        setName(res.server_name || '');
        setInitial(res.server_name || '');
      })
      .catch((e: any) => {
        if (alive) setMessage({ kind: 'err', text: e?.message || t('settings.server_name_load_failed') });
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === initial) return;
    setSaving(true);
    setMessage(null);
    try {
      const res = await api.updateServerName(trimmed);
      setInitial(res.server_name);
      setName(res.server_name);
      setMessage({ kind: 'ok', text: t('settings.server_name_saved') });
    } catch (e: any) {
      setMessage({ kind: 'err', text: e?.message || t('settings.server_name_save_failed') });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="pt-3 border-t border-surface-800">
        <p className="text-sm text-text-500">{t('common.loading')}</p>
      </div>
    );
  }

  const dirty = name.trim() !== initial && name.trim().length > 0;

  return (
    <div className="pt-3 border-t border-surface-800">
      <label className="block text-sm text-text-300 mb-2">{t('settings.server_name')}</label>
      <div className="flex gap-2">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={64}
          disabled={saving}
          className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 focus:outline-none focus:border-blue-500 disabled:opacity-60"
        />
        <button
          onClick={save}
          disabled={!dirty || saving}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg text-white text-sm font-medium transition-colors"
        >
          {saving ? t('common.saving') : t('common.save')}
        </button>
      </div>
      {message && (
        <p
          className={`mt-2 text-xs ${
            message.kind === 'ok' ? 'text-green-400' : 'text-red-400'
          }`}
        >
          {message.text}
        </p>
      )}
      <p className="mt-2 text-xs text-text-500">{t('settings.server_name_hint')}</p>
    </div>
  );
};

// ── Do Not Disturb picker ──────────────────────────────────────────
//
// Quiet-mode toggle. Stored in `settings.dndUntil` as either an ISO
// timestamp, the literal "indefinite", or null/missing for off. The
// notification pipeline (IntegrationBridge._showDesktopNotification)
// reads this same setting on every popup, so changes here take effect
// instantly without restart.

const DndPicker: React.FC = () => {
    const settings = useSettingsStore((s) => s.settings);
    const update = useSettingsStore((s) => s.update);

    const dndUntil = settings.dndUntil ?? null;
    const isActive = (() => {
        if (!dndUntil) return false;
        if (dndUntil === 'indefinite') return true;
        const t = Date.parse(dndUntil);
        return Number.isFinite(t) && t > Date.now();
    })();

    const setUntil = (mode: 'off' | '30min' | '1hour' | '4hours' | 'morning' | 'indefinite') => {
        if (mode === 'off') {
            update({ dndUntil: null });
            return;
        }
        if (mode === 'indefinite') {
            update({ dndUntil: 'indefinite' });
            return;
        }
        const now = new Date();
        let target = new Date(now);
        if (mode === '30min') target = new Date(now.getTime() + 30 * 60_000);
        else if (mode === '1hour') target = new Date(now.getTime() + 60 * 60_000);
        else if (mode === '4hours') target = new Date(now.getTime() + 4 * 60 * 60_000);
        else if (mode === 'morning') {
            target = new Date(now);
            // Tomorrow at 08:00 local time. (No timezone gymnastics needed —
            // the value is consumed by Date.parse, which round-trips local
            // ISO strings just fine on the current machine.)
            target.setDate(target.getDate() + 1);
            target.setHours(8, 0, 0, 0);
        }
        update({ dndUntil: target.toISOString() });
    };

    let activeUntilLabel = '';
    if (isActive && dndUntil !== 'indefinite' && dndUntil) {
        try {
            const d = new Date(dndUntil);
            activeUntilLabel = d.toLocaleString();
        } catch { /* ignore */ }
    }

    const Btn: React.FC<{
        label: string;
        onClick: () => void;
        active?: boolean;
    }> = ({ label, onClick, active }) => (
        <button
            onClick={onClick}
            className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                active
                    ? 'bg-blue-600 text-white'
                    : 'bg-surface-800 text-text-200 hover:bg-surface-700'
            }`}
        >
            {label}
        </button>
    );

    return (
        <div className="space-y-3">
            {isActive && (
                <div className="text-xs text-yellow-400">
                    {dndUntil === 'indefinite'
                        ? t('notifications.dnd_indefinite')
                        : t('notifications.dnd_active').replace('{{time}}', activeUntilLabel)}
                </div>
            )}
            <div className="flex flex-wrap gap-2">
                <Btn label={t('notifications.dnd_off')} onClick={() => setUntil('off')} active={!isActive} />
                <Btn label={t('notifications.dnd_30min')} onClick={() => setUntil('30min')} />
                <Btn label={t('notifications.dnd_1hour')} onClick={() => setUntil('1hour')} />
                <Btn label={t('notifications.dnd_4hours')} onClick={() => setUntil('4hours')} />
                <Btn label={t('notifications.dnd_until_morning')} onClick={() => setUntil('morning')} />
                <Btn
                    label={t('notifications.dnd_indefinite')}
                    onClick={() => setUntil('indefinite')}
                    active={dndUntil === 'indefinite'}
                />
            </div>
        </div>
    );
};

// ── Profile Photo Gallery ──────────────────────────────

const VISIBILITY_ORDER: ProfilePhotoVisibility[] = ['public', 'contacts', 'private'];

const visibilityIcon = (v: ProfilePhotoVisibility, size = 14) => {
  if (v === 'public') return <PublicGlobe size={size} />;
  if (v === 'contacts') return <UsersIcon size={size} />;
  return <Lock size={size} />;
};

const visibilityLabel = (v: ProfilePhotoVisibility) => {
  if (v === 'public') return t('settings.visibility_public');
  if (v === 'contacts') return t('settings.visibility_contacts');
  return t('settings.visibility_private');
};

interface GalleryProps {
  userId: string | undefined;
  onPrimaryChange?: (avatarUrl: string | null) => void;
}

const ProfilePhotosGallery: React.FC<GalleryProps> = ({ userId, onPrimaryChange }) => {
  const [photos, setPhotos] = useState<ProfilePhoto[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.listMyProfilePhotos();
      const list = res.photos || [];
      setPhotos(list);
      // Keep the auth-store avatar in sync with whichever photo is primary on
      // the server — covers first upload, server auto-promote after delete,
      // and live updates pushed over the socket.
      const primary = list.find((p) => p.is_primary);
      onPrimaryChange?.(primary ? primary.url : null);
    } catch (e: any) {
      setError(e?.message || 'Failed to load photos');
    } finally {
      setLoading(false);
    }
  }, [onPrimaryChange]);

  useEffect(() => {
    if (!userId) return;
    load();
  }, [userId, load]);

  // Refresh live when the server broadcasts an update for this user.
  useEffect(() => {
    if (!userId) return;
    const off = socketManager.on('user.photos_updated', (data: any) => {
      if (data?.user_id === userId) load();
    });
    return off;
  }, [userId, load]);

  const handlePick = () => fileInputRef.current?.click();

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setError(null);
    setUploading(true);
    try {
      await api.uploadProfilePhoto(file, { visibility: 'public', makePrimary: photos.length === 0 });
      await load();
    } catch (err: any) {
      setError(err?.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const handleChangeVisibility = async (photoId: string, visibility: ProfilePhotoVisibility) => {
    try {
      await api.updateProfilePhoto(photoId, { visibility });
      setPhotos((prev) => prev.map((p) => (p.id === photoId ? { ...p, visibility } : p)));
    } catch (e: any) {
      setError(e?.message || 'Update failed');
    }
  };

  const handleSetPrimary = async (photoId: string) => {
    try {
      const updated = await api.updateProfilePhoto(photoId, { is_primary: true });
      setPhotos((prev) =>
        prev.map((p) => ({ ...p, is_primary: p.id === photoId })),
      );
      onPrimaryChange?.(updated.url);
    } catch (e: any) {
      setError(e?.message || 'Failed to set primary');
    }
  };

  const handleDelete = async (photoId: string) => {
    if (!window.confirm(t('settings.confirm_delete_photo'))) return;
    try {
      await api.deleteProfilePhoto(photoId);
      // load() now syncs the primary into the auth store on its own.
      await load();
    } catch (e: any) {
      setError(e?.message || 'Delete failed');
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-text-200">{t('settings.photos')}</h3>
        <button
          onClick={handlePick}
          disabled={uploading}
          className="flex items-center gap-2 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 text-white rounded-lg text-xs font-medium transition-colors"
        >
          <Upload size={14} />
          {uploading ? t('settings.uploading') : t('settings.upload_photo')}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={handleUpload}
        />
      </div>

      {error && (
        <p className="text-xs text-red-400 mb-2">{error}</p>
      )}

      {loading && photos.length === 0 && (
        <p className="text-xs text-text-500">{t('common.loading')}</p>
      )}

      {!loading && photos.length === 0 && (
        <p className="text-xs text-text-500">{t('settings.no_photos')}</p>
      )}

      {photos.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {photos.map((photo) => (
            <PhotoCard
              key={photo.id}
              photo={photo}
              onChangeVisibility={handleChangeVisibility}
              onSetPrimary={handleSetPrimary}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
};

interface PhotoCardProps {
  photo: ProfilePhoto;
  onChangeVisibility: (id: string, v: ProfilePhotoVisibility) => void;
  onSetPrimary: (id: string) => void;
  onDelete: (id: string) => void;
}

const PhotoCard: React.FC<PhotoCardProps> = ({
  photo,
  onChangeVisibility,
  onSetPrimary,
  onDelete,
}) => {
  return (
    <div className="relative group rounded-lg overflow-hidden bg-surface-800 aspect-square">
      <AuthorizedImage
        path={photo.url}
        alt=""
        className="w-full h-full object-cover"
        fallback={
          <div className="w-full h-full flex items-center justify-center text-text-500 text-xs">
            …
          </div>
        }
      />

      {photo.is_primary && (
        <span className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded bg-yellow-500/90 text-[10px] font-bold text-black flex items-center gap-1">
          <Star size={10} />
          {t('settings.primary_badge')}
        </span>
      )}

      <span className="absolute top-1.5 right-1.5 px-1.5 py-0.5 rounded bg-black/60 text-[10px] text-white flex items-center gap-1">
        {visibilityIcon(photo.visibility, 10)}
        {visibilityLabel(photo.visibility)}
      </span>

      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 via-black/50 to-transparent opacity-0 group-hover:opacity-100 transition-opacity p-2 flex flex-col gap-1">
        <select
          value={photo.visibility}
          onChange={(e) => onChangeVisibility(photo.id, e.target.value as ProfilePhotoVisibility)}
          className="w-full px-2 py-1 bg-surface-900 border border-surface-700 rounded text-[11px] text-white focus:outline-none"
          aria-label={t('settings.visibility')}
        >
          {VISIBILITY_ORDER.map((v) => (
            <option key={v} value={v}>{visibilityLabel(v)}</option>
          ))}
        </select>
        <div className="flex gap-1">
          {!photo.is_primary && (
            <button
              onClick={() => onSetPrimary(photo.id)}
              title={t('settings.set_primary')}
              className="flex-1 px-2 py-1 bg-yellow-600/80 hover:bg-yellow-600 text-white rounded text-[11px] flex items-center justify-center gap-1 transition-colors"
            >
              <Star size={11} />
            </button>
          )}
          <button
            onClick={() => onDelete(photo.id)}
            title={t('settings.delete_photo')}
            className="flex-1 px-2 py-1 bg-red-600/80 hover:bg-red-600 text-white rounded text-[11px] flex items-center justify-center gap-1 transition-colors"
          >
            <Trash2 size={11} />
          </button>
        </div>
      </div>
    </div>
  );
};

/**
 * ChangePasswordRow — inline form for the user to rotate their own
 * password. Hidden by default behind a "Change password" link; expands
 * to three labelled inputs + actions on click.
 *
 * Posts to `/api/auth/change-password`, which requires the current
 * password to be verified server-side — a stolen access token alone
 * cannot rotate the credential.
 */
const ChangePasswordRow: React.FC = () => {
    const [open, setOpen]   = useState(false);
    const [current, setC]   = useState('');
    const [next, setN]      = useState('');
    const [confirm, setF]   = useState('');
    const [busy, setBusy]   = useState(false);
    const [error, setErr]   = useState<string | null>(null);
    const [done, setDone]   = useState(false);

    const reset = () => { setC(''); setN(''); setF(''); setErr(null); setDone(false); };

    const submit = async () => {
        setErr(null);
        if (next.length < 8)        { setErr('New password must be at least 8 characters'); return; }
        if (next !== confirm)       { setErr('New passwords do not match');                  return; }
        if (next === current)       { setErr('New password must differ from current');       return; }

        setBusy(true);
        try {
            await api.changePassword(current, next);
            setDone(true);
            setTimeout(() => { setOpen(false); reset(); }, 1500);
        } catch (e: any) {
            // The API client throws on non-2xx — surface the server's
            // `detail` message when present, otherwise a generic line.
            const msg = e?.detail || e?.message || 'Failed to update password';
            setErr(/incorrect/i.test(msg) ? 'Current password is incorrect' : msg);
        } finally {
            setBusy(false);
        }
    };

    if (!open) {
        return (
            <button
                onClick={() => setOpen(true)}
                className="text-sm text-blue-400 hover:text-blue-300 mt-3 self-start"
            >
                {t('settings.change_password') || 'Change password'}
            </button>
        );
    }

    return (
        <div className="mt-4 p-4 rounded-xl bg-surface-900 border border-surface-700 space-y-3">
            <p className="text-sm text-text-200 font-medium">
                {t('settings.change_password') || 'Change password'}
            </p>
            <input
                type="password"
                placeholder="Current password"
                value={current}
                onChange={(e) => setC(e.target.value)}
                className="w-full p-2.5 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm"
            />
            <input
                type="password"
                placeholder="New password (≥ 8 characters)"
                value={next}
                onChange={(e) => setN(e.target.value)}
                className="w-full p-2.5 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm"
            />
            <input
                type="password"
                placeholder="Confirm new password"
                value={confirm}
                onChange={(e) => setF(e.target.value)}
                className="w-full p-2.5 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm"
            />

            {error && <p className="text-xs text-red-400">{error}</p>}
            {done  && <p className="text-xs text-green-400">Password updated.</p>}

            <div className="flex gap-2 justify-end pt-1">
                <button
                    onClick={() => { setOpen(false); reset(); }}
                    className="px-3 py-1.5 rounded-lg text-sm text-text-400 hover:bg-surface-800"
                    disabled={busy}
                >
                    Cancel
                </button>
                <button
                    onClick={submit}
                    disabled={busy || !current || !next || !confirm}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-600 text-white
                               hover:bg-blue-500 disabled:bg-surface-700 disabled:text-text-500"
                >
                    {busy ? 'Saving…' : 'Update'}
                </button>
            </div>
        </div>
    );
};

export default SettingsView;
