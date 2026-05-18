/**
 * QuickReactionsBar — horizontal strip of the user's chosen
 * quick-reaction emojis. Shown at the top of the message context
 * menu (or anywhere else a parent wants to mount it).
 *
 * Click an emoji → toggle that reaction on the message via the
 * chat store. Already-applied reactions get a subtle highlight.
 */

import React from 'react';
import { useQuickReactionsStore } from '@/stores/quick-reactions.store';

interface Props {
  /** Reactions already on the message (for highlighting). */
  appliedEmojis: string[];
  onPick: (emoji: string) => void;
}

export const QuickReactionsBar: React.FC<Props> = ({
  appliedEmojis,
  onPick,
}) => {
  const emojis = useQuickReactionsStore((s) => s.emojis);

  if (emojis.length === 0) return null;

  return (
    <div
      className="flex gap-1 p-1.5 border-b border-surface-700"
      role="toolbar"
      aria-label="ردود سريعة"
    >
      {emojis.map((e) => {
        const applied = appliedEmojis.includes(e);
        return (
          <button
            key={e}
            type="button"
            onClick={() => onPick(e)}
            className={
              'px-2 py-1 rounded text-base transition-transform ' +
              'hover:scale-110 ' +
              (applied
                ? 'bg-blue-700/40 ring-1 ring-blue-400'
                : 'hover:bg-surface-700')
            }
            aria-label={`Toggle ${e} reaction`}
            title={applied ? 'إزالة' : 'إضافة'}
          >
            {e}
          </button>
        );
      })}
    </div>
  );
};
