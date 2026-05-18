/**
 * Quick-reactions store — the user's top-N emojis exposed in
 * the context menu's reaction strip.
 *
 * Six is a comfortable cap for a horizontal strip; less than that
 * and we keep what the user picked. Default set is "industry
 * standard" — Telegram and Slack ship with ≈the same emoji.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

const DEFAULT_QUICK_REACTIONS = ['👍', '❤️', '😂', '😮', '😢', '🎉'];

const MAX_QUICK = 8;
const MIN_QUICK = 1;

interface QuickReactionsState {
  emojis: string[];

  setAll: (emojis: string[]) => void;
  addEmoji: (emoji: string) => void;
  removeEmoji: (emoji: string) => void;
  reset: () => void;
}

function dedupAndClamp(input: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const e of input) {
    const trimmed = (e || '').trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    out.push(trimmed);
    if (out.length >= MAX_QUICK) break;
  }
  return out.length >= MIN_QUICK ? out : DEFAULT_QUICK_REACTIONS;
}

export const useQuickReactionsStore = create<QuickReactionsState>()(
  persist(
    (set, get) => ({
      emojis: [...DEFAULT_QUICK_REACTIONS],
      setAll: (emojis) => set({ emojis: dedupAndClamp(emojis) }),
      addEmoji: (emoji) => {
        const next = [...get().emojis, emoji];
        set({ emojis: dedupAndClamp(next) });
      },
      removeEmoji: (emoji) => {
        const next = get().emojis.filter((e) => e !== emoji);
        set({ emojis: dedupAndClamp(next) });
      },
      reset: () => set({ emojis: [...DEFAULT_QUICK_REACTIONS] }),
    }),
    { name: 'helen.quick-reactions.v1' },
  ),
);
