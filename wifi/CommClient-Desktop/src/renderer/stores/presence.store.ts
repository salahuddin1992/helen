/**
 * presence.store — single source of truth for the user's *self* presence
 * (the dot color they advertise). Two surfaces share it:
 *
 *   1. `MyPresencePill` (TitleBar) — the picker that lets the user
 *      flip themselves to متاح / بعيد / مشغول / لا تزعجني and emits
 *      `presence_set_status` to the server.
 *   2. `TitleBar` itself — colors the "Helen" wordmark with the
 *      matching tint so the user can spot their advertised state at a
 *      glance from anywhere in the app.
 *
 * Persisted to localStorage so the choice survives renderer restarts.
 * The presence is *re-asserted* to the server on every socket
 * (re)connect by MyPresencePill — losing the socket doesn't lose the
 * user's choice.
 */

import { create } from 'zustand';

export type SelfPresence = 'online' | 'away' | 'busy' | 'dnd';

const STORAGE_KEY = 'helen.presence.self';

function loadCached(): SelfPresence {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'online' || v === 'away' || v === 'busy' || v === 'dnd') return v;
  } catch { /* localStorage may be blocked */ }
  return 'online';
}

interface PresenceState {
  status: SelfPresence;
  setStatus: (s: SelfPresence) => void;
}

export const usePresenceStore = create<PresenceState>((set) => ({
  status: loadCached(),
  setStatus: (s) => {
    try { localStorage.setItem(STORAGE_KEY, s); } catch { /* */ }
    set({ status: s });
  },
}));
