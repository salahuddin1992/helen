/**
 * CaptionsOverlay — bottom-of-screen live caption strip during a
 * call. Shows the last few lines (speaker + text) from the rolling
 * caption buffer in the call store.
 *
 * The captions stream is opt-in via the toolbar toggle; whoever
 * enables captions has their mic chunked client-side and uploaded
 * to the server's whisper-cli worker. The resulting text is fanned
 * out to every participant, so a single user enabling captions
 * makes them visible for the whole call.
 */

import React, { useEffect, useRef } from 'react';
import { useCallStore } from '@/stores/call.store.v2';

const CaptionsOverlay: React.FC = () => {
  const captions = useCallStore((s) => s.captions);
  const participants = useCallStore((s) => s.participants);
  const status = useCallStore((s) => s.status);
  const enabled = useCallStore((s) => s.liveCaptionsEnabled);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the newest line.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [captions]);

  // Only render when captions are flowing OR a transcript line is
  // recent enough that the user might still want to read it.
  const showOverlay = (status === 'active' || status === 'reconnecting')
    && (enabled || captions.length > 0);
  if (!showOverlay) return null;

  // Show only the last 4 lines so the strip stays unobtrusive.
  const visible = captions.slice(-4);

  return (
    <div className="absolute bottom-28 left-1/2 -translate-x-1/2 z-20
                    w-[min(720px,90%)] pointer-events-none">
      <div
        ref={scrollRef}
        className="bg-black/70 backdrop-blur rounded-lg p-3 max-h-32
                   overflow-y-auto space-y-1 shadow-2xl"
      >
        {visible.length === 0 ? (
          <div className="text-white/60 text-xs italic text-center">
            في انتظار الكلام...
          </div>
        ) : (
          visible.map((c) => {
            const speaker =
              participants[c.userId]?.displayName || c.userId.slice(0, 6);
            return (
              <div key={c.id} className="text-sm leading-snug">
                <span className="font-semibold text-blue-300 me-2">
                  {speaker}:
                </span>
                <span className="text-white">{c.text}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default CaptionsOverlay;
