/**
 * AutoFollowSpeaker — opt-in: when the user is in speaker or
 * sidebar layout AND no manual spotlight is set, the spotlight
 * follows the dominant speaker automatically.
 *
 * Behaviour
 * ---------
 * - Hooks the existing dominant-speaker detection from CallView
 *   indirectly: we read the call store + spotlight store and only
 *   intervene when:
 *     1. layoutMode != 'gallery'
 *     2. no manual spotlight is pinned
 *     3. the dominant speaker has been talking for at least
 *        ``DEBOUNCE_MS`` (default 1500ms) — avoids flickering
 *        between two people in a heated debate.
 *
 * - Toggleable via the ``autoFollowSpeaker`` setting (default off).
 *   When off, this component is a no-op.
 *
 * - Headless: renders nothing. Lives next to the other behavioural
 *   mounts in CallView.
 *
 * Note: this component reads ``dominantSpeaker`` indirectly by
 * polling the speaking-peers map — CallView already exposes
 * speakingPeers via the audio meter, so we rely on that signal.
 * If nobody is speaking the spotlight stays where it is.
 */

import React, { useEffect, useRef } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { useLayoutStore } from '@/stores/layout.store';
import { useSpotlightStore } from '@/stores/spotlight.store';
import { useSettingsStore } from '@/stores/settings.store';

const DEBOUNCE_MS = 1500;

const AutoFollowSpeaker: React.FC = () => {
  const status = useCallStore((s) => s.status);
  const remoteStreams = useCallStore((s) => s.remoteStreams);
  const layout = useLayoutStore((s) => s.layout);
  const spotlightedId = useSpotlightStore((s) => s.spotlightedPeerId);
  const setSpotlight = useSpotlightStore((s) => s.setSpotlight);

  const settings: any = useSettingsStore((s) => s.settings);
  const enabled = !!settings?.autoFollowSpeaker;

  const lastSwitchAt = useRef(0);
  const candidateRef = useRef<string | null>(null);
  const candidateSinceRef = useRef(0);
  const ctxRef = useRef<AudioContext | null>(null);
  const analysersRef = useRef<Map<string, {
    getLevel: () => number;
    cleanup: () => void;
  }>>(new Map());
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) return;
    if (status !== 'active' && status !== 'reconnecting') return;
    if (layout === 'gallery') return;
    if (spotlightedId) return; // user has a manual pin — leave it

    // Build per-stream analysers. Reuses the same approach as
    // CallView's mixer but local-only and unaffected by the user-
    // facing audio level meter.
    if (!ctxRef.current) {
      try { ctxRef.current = new AudioContext(); }
      catch { return; }
    }
    const ctx = ctxRef.current;
    if (!ctx) return;

    const ids = Object.keys(remoteStreams);
    for (const id of ids) {
      if (analysersRef.current.has(id)) continue;
      try {
        const src = ctx.createMediaStreamSource(remoteStreams[id]);
        const an = ctx.createAnalyser();
        an.fftSize = 128;
        src.connect(an);
        const buf = new Uint8Array(an.frequencyBinCount);
        analysersRef.current.set(id, {
          getLevel: () => {
            an.getByteFrequencyData(buf);
            let sum = 0;
            for (let i = 0; i < buf.length; i++) sum += buf[i];
            return sum / (buf.length * 255);
          },
          cleanup: () => {
            try { src.disconnect(); } catch { /* ignore */ }
            try { an.disconnect(); } catch { /* ignore */ }
          },
        });
      } catch { /* ignore */ }
    }
    // Drop analysers for streams that left.
    for (const [id, a] of analysersRef.current.entries()) {
      if (!ids.includes(id)) {
        a.cleanup();
        analysersRef.current.delete(id);
      }
    }

    const tick = () => {
      // Identify the loudest stream right now.
      let topId: string | null = null;
      let topLevel = 0;
      for (const [id, a] of analysersRef.current.entries()) {
        const lv = a.getLevel();
        if (lv > topLevel) { topLevel = lv; topId = id; }
      }
      const SPEAK_THRESH = 0.06;
      const now = Date.now();

      if (topId && topLevel > SPEAK_THRESH) {
        if (candidateRef.current !== topId) {
          candidateRef.current = topId;
          candidateSinceRef.current = now;
        } else if (
          // Sustained for the debounce window AND we last switched
          // more than DEBOUNCE_MS ago — promote the candidate.
          now - candidateSinceRef.current >= DEBOUNCE_MS &&
          now - lastSwitchAt.current >= DEBOUNCE_MS
        ) {
          // Re-check the user hasn't pinned in the meantime.
          if (!useSpotlightStore.getState().spotlightedPeerId) {
            setSpotlight(topId);
            lastSwitchAt.current = now;
          }
        }
      } else {
        candidateRef.current = null;
        candidateSinceRef.current = 0;
      }

      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [enabled, status, layout, spotlightedId, remoteStreams, setSpotlight]);

  // Final cleanup on unmount or when feature toggled off.
  useEffect(() => {
    return () => {
      for (const a of analysersRef.current.values()) {
        try { a.cleanup(); } catch { /* ignore */ }
      }
      analysersRef.current.clear();
      if (ctxRef.current) {
        ctxRef.current.close().catch(() => { /* ignore */ });
        ctxRef.current = null;
      }
    };
  }, []);

  return null;
};

export default AutoFollowSpeaker;
