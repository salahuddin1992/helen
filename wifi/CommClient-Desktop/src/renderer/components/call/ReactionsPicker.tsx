/**
 * ReactionsPicker — toolbar button that opens a small emoji palette
 * for sending live reactions during a call.
 *
 * Tapping an emoji fires a transient ``call:reaction`` socket event
 * that the server fans out to everyone in the call. The receivers
 * see the emoji float up via ReactionsLayer for ~2s.
 */

import React, { useState, useRef, useEffect } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { Smile } from 'lucide-react';

const QUICK_REACTIONS: string[] = ['👍', '❤️', '😂', '😮', '🎉', '👏', '🙌', '🔥'];

const ReactionsPicker: React.FC = () => {
  const sendReaction = useCallStore((s) => s.sendReaction);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click — without this, the picker stays open
  // when the user moves on to another control and looks orphaned.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const handlePick = (emoji: string) => {
    sendReaction(emoji);
    setOpen(false);
  };

  return (
    <div className="flex flex-col items-center gap-2 relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`w-14 h-14 rounded-full flex items-center justify-center transition-all duration-200
                    ${open ? 'bg-surface-800 text-text-100' : 'bg-surface-700 text-text-300 hover:bg-surface-600'}`}
        title="إرسال تفاعل"
      >
        <Smile size={24} />
      </button>
      <span className="text-xs text-text-400 font-medium">تفاعل</span>

      {open && (
        <div
          className="absolute bottom-20 left-1/2 -translate-x-1/2
                     bg-surface-900/95 border border-surface-700
                     rounded-full px-3 py-2 shadow-2xl flex gap-1
                     backdrop-blur z-50"
        >
          {QUICK_REACTIONS.map((emoji) => (
            <button
              key={emoji}
              onClick={() => handlePick(emoji)}
              className="w-10 h-10 flex items-center justify-center text-2xl
                         hover:bg-surface-700 rounded-full transition-transform
                         hover:scale-125"
              title={`Send ${emoji}`}
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default ReactionsPicker;
