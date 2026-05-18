/**
 * Notification-sounds store — per-event-type sound choice + master
 * volume + master mute.
 *
 * Keeps the user's audio preferences local; the IntegrationBridge
 * (the single piece that decides whether to play *anything* on a
 * new message / call / mention) consults this store.
 *
 * Persisted via zustand's ``persist`` middleware so prefs survive
 * a relaunch.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { SoundId } from '@/services/notification-sounds';
import { play } from '@/services/notification-sounds';

export type EventKind =
  | 'message'    // any new message in any channel
  | 'mention'   // someone @-mentioned me
  | 'dm'        // new DM specifically
  | 'call'      // incoming call ring
  | 'system';   // system event (admin notice, etc.)

interface NotificationSoundsState {
  master_muted: boolean;
  master_volume: number;          // 0..1
  sounds: Record<EventKind, SoundId>;

  setMuted: (m: boolean) => void;
  setVolume: (v: number) => void;
  setSound: (kind: EventKind, id: SoundId) => void;
  /** Play the sound for ``kind`` honoring mute + volume. The
   *  consumer (IntegrationBridge) calls this on each event. */
  playFor: (kind: EventKind) => void;
}

const DEFAULT_SOUNDS: Record<EventKind, SoundId> = {
  message: 'chime',
  mention: 'bling',
  dm: 'pop',
  call: 'ring',
  system: 'blip',
};

export const useNotificationSoundsStore =
  create<NotificationSoundsState>()(
    persist(
      (set, get) => ({
        master_muted: false,
        master_volume: 0.6,
        sounds: { ...DEFAULT_SOUNDS },

        setMuted: (m) => set({ master_muted: m }),
        setVolume: (v) => set({
          master_volume: Math.max(0, Math.min(1, v)),
        }),
        setSound: (kind, id) =>
          set((s) => ({ sounds: { ...s.sounds, [kind]: id } })),
        playFor: (kind) => {
          const s = get();
          if (s.master_muted) return;
          const id = s.sounds[kind] || 'none';
          play(id, s.master_volume);
        },
      }),
      { name: 'helen.notification-sounds.v1' },
    ),
  );
