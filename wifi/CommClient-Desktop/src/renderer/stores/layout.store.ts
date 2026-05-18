/**
 * Layout store — UI-only setting for which call layout to render.
 *
 * Three modes:
 *   - "gallery"  — equal grid of every participant (existing default).
 *   - "speaker"  — one big tile (active speaker or spotlit), no
 *                  thumbnails. Best for presentations.
 *   - "sidebar"  — main content (screen share or speaker) on the
 *                  left, vertical strip of thumbnails on the right.
 *                  Best when someone is sharing AND others are
 *                  visible.
 *
 * Pure UI state — never sent to the server. Persisted in
 * localStorage so the user's preference survives reloads.
 */

import { create } from 'zustand';

export type LayoutMode = 'gallery' | 'speaker' | 'sidebar';

const STORAGE_KEY = 'helen.callLayout';

const initial: LayoutMode = (() => {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'gallery' || v === 'speaker' || v === 'sidebar') return v;
  } catch { /* ignore */ }
  return 'gallery';
})();

interface LayoutState {
  layout: LayoutMode;
  setLayout: (m: LayoutMode) => void;
  cycleLayout: () => void;
}

export const useLayoutStore = create<LayoutState>((set, get) => ({
  layout: initial,
  setLayout: (m) => {
    set({ layout: m });
    try { localStorage.setItem(STORAGE_KEY, m); } catch { /* ignore */ }
  },
  cycleLayout: () => {
    const order: LayoutMode[] = ['gallery', 'speaker', 'sidebar'];
    const cur = get().layout;
    const idx = order.indexOf(cur);
    const next = order[(idx + 1) % order.length];
    get().setLayout(next);
  },
}));
