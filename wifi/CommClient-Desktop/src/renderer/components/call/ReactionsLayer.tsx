/**
 * ReactionsLayer — floating emoji reactions during a call.
 *
 * Each reaction enters from the bottom-right of the screen, floats
 * upward over ~2 seconds with a slight horizontal drift, and fades
 * out. The store auto-removes entries after 2.2s so we don't need
 * any cleanup here — when the entry disappears from the array, its
 * DOM node unmounts.
 *
 * The float animation uses Tailwind's animate-* utilities + a
 * keyframe defined inline so we don't have to touch the global
 * Tailwind config. It's pointer-events-none so it never intercepts
 * clicks meant for the call controls beneath.
 */

import React from 'react';
import { useCallStore } from '@/stores/call.store.v2';

const ReactionsLayer: React.FC = () => {
  const reactions = useCallStore((s) => s.activeReactions);
  const participants = useCallStore((s) => s.participants);

  if (reactions.length === 0) return null;

  return (
    <>
      <style>{`
        @keyframes helen-reaction-float {
          0%   { opacity: 0; transform: translateY(0)    scale(0.6); }
          15%  { opacity: 1; transform: translateY(-30px) scale(1.1); }
          80%  { opacity: 1; transform: translateY(-220px) scale(1); }
          100% { opacity: 0; transform: translateY(-280px) scale(0.9); }
        }
      `}</style>
      <div className="absolute inset-0 z-40 pointer-events-none overflow-hidden">
        {reactions.map((r, i) => {
          // Spread reactions across the bottom — modulo so a flood
          // doesn't stack on the same x.
          const offset = ((i % 7) - 3) * 60;
          const senderName =
            participants[r.userId]?.displayName || r.userId.slice(0, 6);
          return (
            <div
              key={r.id}
              className="absolute bottom-32 left-1/2 flex flex-col items-center"
              style={{
                transform: `translateX(calc(-50% + ${offset}px))`,
                animation: 'helen-reaction-float 2200ms ease-out forwards',
              }}
            >
              <span className="text-5xl drop-shadow-lg">{r.emoji}</span>
              <span className="mt-1 px-2 py-0.5 rounded-full bg-black/50 text-white text-[10px] font-medium">
                {senderName}
              </span>
            </div>
          );
        })}
      </div>
    </>
  );
};

export default ReactionsLayer;
