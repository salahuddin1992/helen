/**
 * AutoMuteWhileTyping — opt-in privacy helper that mutes the mic
 * while the user is actively typing on the keyboard, then restores
 * the previous state ~1.5s after typing stops.
 *
 * Why
 * ---
 * Mechanical-keyboard click sounds are the #1 complaint on calls
 * after dog barks. Push-to-talk is one solution but breaks
 * conversational flow; auto-mute-on-keystroke is invisible — the
 * user never notices unless they LOOK at their mute icon.
 *
 * Behaviour
 * ---------
 * Listens for ``keydown`` globally. When the user is unmuted and
 * presses any character key, we flip mute on and remember the
 * pre-mute state. After ``UNMUTE_DELAY_MS`` of no keystrokes, we
 * flip mute back off.
 *
 * If the user manually toggles mute mid-typing (e.g. they DO want
 * to speak), we abort the auto-restore so we don't silently
 * un-mute them later.
 *
 * Excludes:
 *   - Modifier-only presses (Shift, Ctrl alone).
 *   - Typing in a field that should NOT auto-mute (rare; we just
 *     listen on keydown anywhere). The trigger is conservative:
 *     letter/number/space/punctuation.
 */

import React, { useEffect, useRef } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { useSettingsStore } from '@/stores/settings.store';

const UNMUTE_DELAY_MS = 1500;

const AutoMuteWhileTyping: React.FC = () => {
  const isMuted = useCallStore((s) => s.isMuted);
  const status = useCallStore((s) => s.status);
  const toggleMute = useCallStore((s) => s.toggleMute);
  // Surface a settings flag so users can opt out. The setting lives
  // on the existing settings store; if it doesn't exist, we treat
  // the feature as enabled by default.
  const settings: any = useSettingsStore((s) => s.settings);
  const enabled = settings?.autoMuteWhileTyping !== false;

  const restoreTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasMuteFlippedByUs = useRef(false);
  const lastKeyAt = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    if (status !== 'active' && status !== 'reconnecting') return;

    const isTypingKey = (e: KeyboardEvent): boolean => {
      // Filter to keys that actually generate audible sound:
      // anything that produces a 1-character key or Space / Enter /
      // Backspace / Tab. Modifier-only events (Shift, Control,
      // Meta, Alt) don't make a sound and shouldn't trigger mute.
      if (e.key.length === 1) return true;
      return ['Space', 'Enter', 'Backspace', 'Tab'].includes(e.code);
    };

    const onKey = (e: KeyboardEvent) => {
      if (!isTypingKey(e)) return;
      lastKeyAt.current = Date.now();

      // Already muted by something else (the user, force-mute, etc.)
      // — do nothing so we don't fight the existing state.
      if (isMuted && !wasMuteFlippedByUs.current) return;

      // First key in a typing burst: flip mute and remember it.
      if (!isMuted && !wasMuteFlippedByUs.current) {
        wasMuteFlippedByUs.current = true;
        toggleMute();
      }

      // Reset the un-mute timer.
      if (restoreTimer.current) {
        clearTimeout(restoreTimer.current);
      }
      restoreTimer.current = setTimeout(() => {
        // Only restore if the gap is genuinely past the delay AND
        // the user hasn't manually toggled mute since.
        if (
          wasMuteFlippedByUs.current &&
          Date.now() - lastKeyAt.current >= UNMUTE_DELAY_MS - 50
        ) {
          wasMuteFlippedByUs.current = false;
          // Re-read the latest mute state — if the user manually
          // un-muted earlier, do nothing.
          if (useCallStore.getState().isMuted) {
            toggleMute();
          }
        }
      }, UNMUTE_DELAY_MS);
    };

    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (restoreTimer.current) clearTimeout(restoreTimer.current);
    };
  }, [enabled, status, isMuted, toggleMute]);

  // Cancel the pending restore if the user manually toggles mute.
  useEffect(() => {
    if (wasMuteFlippedByUs.current && !isMuted) {
      // Mic became un-muted by something other than our timer —
      // forget about the auto-restore.
      wasMuteFlippedByUs.current = false;
      if (restoreTimer.current) {
        clearTimeout(restoreTimer.current);
        restoreTimer.current = null;
      }
    }
  }, [isMuted]);

  return null;  // headless
};

export default AutoMuteWhileTyping;
