/**
 * NetworkAdaptiveVideoMount — invisible component that lives
 * inside the active CallView and runs the NetworkAdaptiveVideo
 * controller against the call store's quality stream.
 *
 * Renders a small banner when the controller has auto-paused
 * the local video, with a one-click "أبقِ الفيديو شغّالاً"
 * override that forfeits adaptation for the rest of the call.
 *
 * Why a sibling component instead of putting the policy in the
 * CallView: keeps the video adaptation isolated. Disabling the
 * feature is one removed import, not surgery on the call layout.
 */

import React, { useEffect, useRef, useState } from 'react';
import { VideoOff } from 'lucide-react';
import { useCallStore } from '@/stores/call.store.v2';
import {
  NetworkAdaptiveVideo,
  type AdaptiveBannerState,
  type AdaptiveQualityLevel,
} from '@/services/call/NetworkAdaptiveVideo';

export const NetworkAdaptiveVideoMount: React.FC = () => {
  const status = useCallStore((s) => s.status);
  const qualityLevel = useCallStore(
    (s) => s.qualityLevel as AdaptiveQualityLevel,
  );
  const isVideoOff = useCallStore((s) => s.isVideoOff);
  const toggleVideo = useCallStore((s) => s.toggleVideo);

  const [banner, setBanner] = useState<AdaptiveBannerState>({
    active: false, reason: null, lastChangeAt: 0,
  });
  const adapterRef = useRef<NetworkAdaptiveVideo | null>(null);
  // Track previous video state so we can tell user-toggle apart from
  // our own pause/resume calls. We expect adapter-driven toggles to
  // change isVideoOff through ``toggleVideo`` as well, so we set a
  // self-trigger flag while we initiate the change.
  const selfTriggerRef = useRef(false);

  // Build the adapter once per call.
  useEffect(() => {
    if (status !== 'active') {
      adapterRef.current = null;
      setBanner({ active: false, reason: null, lastChangeAt: 0 });
      return;
    }
    const adapter = new NetworkAdaptiveVideo({
      sustainedMs: 4000,
      recoverMs: 6000,
      onPause: () => {
        // Only act if video is currently on; otherwise the manual
        // toggle already pre-paused.
        if (!isVideoOffSnapshot()) {
          selfTriggerRef.current = true;
          toggleVideo();
        }
      },
      onResume: () => {
        // Only resume if we still have the camera off.
        if (isVideoOffSnapshot()) {
          selfTriggerRef.current = true;
          toggleVideo();
        }
      },
      onBannerChange: (s) => setBanner(s),
    });
    adapterRef.current = adapter;
    return () => {
      adapterRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  // Feed quality changes into the adapter.
  useEffect(() => {
    const adapter = adapterRef.current;
    if (!adapter || status !== 'active') return;
    adapter.feed({ overallLevel: qualityLevel });
  }, [qualityLevel, status]);

  // Detect manual video toggles. If isVideoOff changed and the
  // self-trigger flag isn't set, the user did it.
  useEffect(() => {
    const adapter = adapterRef.current;
    if (!adapter) return;
    if (selfTriggerRef.current) {
      selfTriggerRef.current = false;
      return;
    }
    adapter.noteManualVideoToggle();
    setBanner(adapter.bannerState());
  }, [isVideoOff]);

  // Pull a fresh snapshot rather than relying on the closed-over
  // value (which lags one render behind in the callback path).
  function isVideoOffSnapshot(): boolean {
    return useCallStore.getState().isVideoOff;
  }

  const handleOverride = () => {
    const adapter = adapterRef.current;
    if (!adapter) return;
    adapter.noteManualVideoToggle();
    setBanner(adapter.bannerState());
    if (isVideoOff) {
      // Bring the camera back on right away — that's what the
      // user clicking "keep video on" expects.
      selfTriggerRef.current = true;
      toggleVideo();
    }
  };

  if (!banner.active) return null;

  return (
    <div className="fixed top-3 left-1/2 -translate-x-1/2 z-50">
      <div className="flex items-center gap-3 px-4 py-2 rounded-full
                      bg-amber-700/90 text-amber-50 text-xs
                      shadow-lg backdrop-blur">
        <VideoOff size={14} />
        <span>
          الشبكة ضعيفة — أوقفنا الفيديو تلقائيّاً للحفاظ على جودة الصوت
        </span>
        <button
          onClick={handleOverride}
          className="px-2 py-1 rounded bg-amber-900/60
                     hover:bg-amber-900/80 text-amber-50
                     border border-amber-200/30"
        >
          أبقِ الفيديو شغّالاً
        </button>
      </div>
    </div>
  );
};
