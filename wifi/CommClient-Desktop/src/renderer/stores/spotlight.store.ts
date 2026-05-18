/**
 * Spotlight store — which participant should be rendered large.
 *
 * Pure UI state — never sent to the server. The CallView reads
 * this to decide who gets the big tile and who gets a thumbnail
 * in the strip at the bottom.
 *
 * Spotlight rules:
 *   * One spotlight at a time. Selecting another participant
 *     replaces the previous spotlight.
 *   * ``null`` (the default) means "auto" — CallView falls back
 *     to whatever it normally does (active speaker / equal grid).
 *   * Clearing on hangup is the caller's responsibility — see the
 *     ``clear`` action.
 *
 * No persistence: spotlight choice is per-call only. Survives
 * route changes inside the call but resets when the call ends.
 */

import { create } from 'zustand';

interface SpotlightState {
  /** Single "spotlight" peer for the legacy single-tile big view. */
  spotlightedPeerId: string | null;
  /** Multi-pin: a set of peers always rendered large in addition to
   *  the active speaker. Different from ``spotlightedPeerId`` because
   *  multi-pin doesn't replace the grid — it elevates a few peers
   *  above the rest. Order is preserved (insertion order). */
  pinnedPeerIds: string[];

  setSpotlight: (peerId: string | null) => void;
  toggleSpotlight: (peerId: string) => void;
  /** Add/remove a peer from the pinned set. Independent of spotlight. */
  togglePin: (peerId: string) => void;
  clearPins: () => void;
  clear: () => void;
}

const MAX_PINS = 6;

export const useSpotlightStore = create<SpotlightState>((set, get) => ({
  spotlightedPeerId: null,
  pinnedPeerIds: [],

  setSpotlight: (peerId) => set({ spotlightedPeerId: peerId }),
  toggleSpotlight: (peerId) => {
    const current = get().spotlightedPeerId;
    set({
      spotlightedPeerId: current === peerId ? null : peerId,
    });
  },
  togglePin: (peerId) => {
    const current = get().pinnedPeerIds;
    if (current.includes(peerId)) {
      set({ pinnedPeerIds: current.filter((id) => id !== peerId) });
    } else {
      // Cap at MAX_PINS so the grid never collapses to a wall of
      // pins. Drops the oldest to make room.
      const next = [...current, peerId].slice(-MAX_PINS);
      set({ pinnedPeerIds: next });
    }
  },
  clearPins: () => set({ pinnedPeerIds: [] }),
  clear: () => set({ spotlightedPeerId: null, pinnedPeerIds: [] }),
}));
