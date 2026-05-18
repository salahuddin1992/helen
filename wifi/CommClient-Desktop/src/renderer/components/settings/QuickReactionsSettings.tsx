/**
 * QuickReactionsSettings — settings panel where the user picks the
 * top-N emojis exposed in the message context menu.
 *
 * Two interactions:
 *   * Click a chip in "current" → remove it.
 *   * Click an emoji in "suggestions" → add it (if there's room).
 *
 * Suggestions are a hand-picked 24-emoji rotation that covers the
 * common reactions across Telegram / Slack / Discord. The user can
 * also type any emoji into the small input.
 */

import React, { useState } from 'react';
import { X, Plus } from 'lucide-react';

// Inline SVG — lucide-react 0.383's d.ts doesn't reliably export
// RotateCcw / Undo. Same workaround we used in WhiteboardToolbar.
const RotateCcw: React.FC<{ size?: number }> = ({ size = 11 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <path d="M3 7v6h6" />
    <path d="M21 17a9 9 0 0 0-15-6.7L3 13" />
  </svg>
);
import { useQuickReactionsStore } from '@/stores/quick-reactions.store';

const SUGGESTIONS = [
  '👍', '👎', '❤️', '🔥', '🎉', '😂', '😮', '😢',
  '😡', '👏', '🙏', '🤔', '👀', '✅', '❌', '💯',
  '🚀', '🥳', '🤝', '💡', '⭐', '☕', '🎁', '🌹',
];

export const QuickReactionsSettings: React.FC = () => {
  const emojis = useQuickReactionsStore((s) => s.emojis);
  const addEmoji = useQuickReactionsStore((s) => s.addEmoji);
  const removeEmoji = useQuickReactionsStore((s) => s.removeEmoji);
  const reset = useQuickReactionsStore((s) => s.reset);
  const [custom, setCustom] = useState('');

  const handleCustomAdd = () => {
    if (!custom.trim()) return;
    addEmoji(custom.trim());
    setCustom('');
  };

  return (
    <div className="bg-surface-900 border border-surface-700
                    rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-100">
          ردود الفعل السريعة
        </h3>
        <button
          type="button"
          onClick={reset}
          className="flex items-center gap-1 px-2 py-1 text-[11px]
                     bg-surface-700 hover:bg-surface-600 rounded
                     text-gray-300"
          title="استعادة الافتراضي"
        >
          <RotateCcw size={11} />
          <span>افتراضي</span>
        </button>
      </div>

      <p className="text-xs text-gray-400">
        ستظهر هذه الإيموجي في قائمة السياق على كل رسالة. الحد الأعلى 8.
      </p>

      {/* Current */}
      <div>
        <div className="text-[11px] text-gray-400 mb-1">المختارة</div>
        <div className="flex flex-wrap gap-1.5">
          {emojis.map((e) => (
            <button
              key={e}
              onClick={() => removeEmoji(e)}
              className="flex items-center gap-1 px-2 py-1 rounded
                         bg-blue-700/40 text-white text-base
                         hover:bg-red-700/40"
              title="إزالة"
            >
              <span>{e}</span>
              <X size={10} className="opacity-60" />
            </button>
          ))}
        </div>
      </div>

      {/* Suggestions */}
      <div>
        <div className="text-[11px] text-gray-400 mb-1">اقتراحات</div>
        <div className="flex flex-wrap gap-1">
          {SUGGESTIONS.filter((e) => !emojis.includes(e)).map((e) => (
            <button
              key={e}
              onClick={() => addEmoji(e)}
              className="px-2 py-1 rounded bg-surface-700
                         hover:bg-surface-600 text-base"
              title="إضافة"
            >
              {e}
            </button>
          ))}
        </div>
      </div>

      {/* Custom add */}
      <div className="flex items-center gap-2 pt-1">
        <input
          type="text"
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder="إيموجي مخصّص"
          maxLength={6}
          className="flex-1 px-2 py-1 text-sm bg-surface-800
                     border border-surface-700 rounded text-white
                     placeholder-gray-500"
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              handleCustomAdd();
            }
          }}
        />
        <button
          onClick={handleCustomAdd}
          disabled={!custom.trim() || emojis.length >= 8}
          className="flex items-center gap-1 px-3 py-1 text-xs
                     bg-blue-700 hover:bg-blue-600 disabled:opacity-40
                     text-white rounded"
        >
          <Plus size={11} />
          <span>إضافة</span>
        </button>
      </div>
    </div>
  );
};
