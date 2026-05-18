/**
 * PiPButton — toggle Picture-in-Picture for a hidden video element
 * that mirrors the dominant speaker's stream (or the local stream
 * as a fallback). Lets the user keep an eye on the call while
 * working in another app.
 *
 * Implementation
 * --------------
 * We mount a hidden <video> tag that we keep srcObject-bound to
 * whichever stream is most relevant:
 *   1. Active screen-share, if any (most useful to track).
 *   2. The dominant speaker's remote stream.
 *   3. The local stream as a last resort (1:1 calls).
 *
 * On click, we call requestPictureInPicture() on the hidden video.
 * The browser opens the floating window automatically. When the
 * user closes it (via the OS chrome) the leavepictureinpicture
 * event fires and we update local state.
 *
 * Browser support: Chromium-only API. We check for it before
 * exposing the button so non-Chromium builds don't show a broken
 * control.
 */

import React, { useEffect, useRef, useState } from 'react';
import { useCallStore } from '@/stores/call.store.v2';

/** PiP icon — two nested rectangles (a tile inside a frame). Inline
 *  because the bare `PictureInPicture2` symbol isn't re-exported in
 *  our pinned lucide-react. */
const PiPSvg: React.FC<{ size?: number }> = ({ size = 22 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 9V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4" />
    <rect x="11" y="13" width="10" height="8" rx="2" />
  </svg>
);

const PiPButton: React.FC = () => {
  const remoteStreams = useCallStore((s) => s.remoteStreams);
  const localStream = useCallStore((s) => s.localStream);
  const isScreenSharing = useCallStore((s) => s.isScreenSharing);
  const screenStream = useCallStore((s) => s.screenStream);
  const status = useCallStore((s) => s.status);

  const videoRef = useRef<HTMLVideoElement>(null);
  const [active, setActive] = useState(false);

  // Pick the most relevant stream for the PiP window.
  const targetStream: MediaStream | null = (() => {
    if (isScreenSharing && screenStream) return screenStream;
    const ids = Object.keys(remoteStreams);
    if (ids.length > 0) return remoteStreams[ids[0]];
    return localStream;
  })();

  // Sync srcObject on every relevant change.
  useEffect(() => {
    if (videoRef.current && targetStream) {
      (videoRef.current as any).srcObject = targetStream;
      // The hidden <video> needs to be "playing" before PiP can
      // be requested. Calling play() is idempotent and silent
      // because muted=true.
      videoRef.current.play().catch(() => { /* ignore */ });
    }
  }, [targetStream]);

  // Listen for OS-driven PiP close so the button reflects reality.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onEnter = () => setActive(true);
    const onLeave = () => setActive(false);
    v.addEventListener('enterpictureinpicture', onEnter);
    v.addEventListener('leavepictureinpicture', onLeave);
    return () => {
      v.removeEventListener('enterpictureinpicture', onEnter);
      v.removeEventListener('leavepictureinpicture', onLeave);
    };
  }, []);

  // Hide entirely on browsers without PiP API + while no call.
  const supported =
    typeof document !== 'undefined' &&
    'pictureInPictureEnabled' in document &&
    (document as any).pictureInPictureEnabled;
  if (!supported) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  const toggle = async () => {
    const v = videoRef.current;
    if (!v) return;
    try {
      if (active && (document as any).pictureInPictureElement === v) {
        await (document as any).exitPictureInPicture();
      } else {
        await (v as any).requestPictureInPicture();
      }
    } catch (err) {
      console.warn('[PiP] toggle failed:', err);
    }
  };

  return (
    <div className="flex flex-col items-center gap-2">
      <button
        onClick={toggle}
        className={`w-14 h-14 rounded-full flex items-center justify-center transition-all duration-200
                    ${active
                      ? 'bg-blue-600 text-white'
                      : 'bg-surface-700 text-text-300 hover:bg-surface-600'}`}
        title="نافذة عائمة (PiP)"
      >
        <PiPSvg size={22} />
      </button>
      <span className="text-xs text-text-400 font-medium">PiP</span>

      {/* Hidden video element bound to whichever stream we mirror.
          Style hides it visually without removing it from the DOM
          (PiP requires the element to be in the document). */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted
        className="absolute opacity-0 pointer-events-none w-1 h-1"
      />
    </div>
  );
};

export default PiPButton;
