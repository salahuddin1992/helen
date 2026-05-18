/**
 * Keyboard-shortcuts registry — single source of truth for app-wide
 * keybindings. The store keeps the user's customized bindings; the
 * defaults live in ``DEFAULT_SHORTCUTS`` so a Reset returns the
 * known-good set.
 *
 * A shortcut is a keyboard combo serialized as a token like
 * ``Ctrl+K`` / ``Ctrl+Shift+M`` / ``Alt+ArrowUp``. Modifier order
 * is normalized: Ctrl → Alt → Shift → Meta. The serialization is
 * stable so ``store.getCombo('search')`` always returns the same
 * token a registered listener can compare against.
 *
 * Hook usage
 * ----------
 * ``useShortcutListener('search', () => openSearch())`` registers a
 * ``keydown`` listener for the action; the handler fires whenever
 * the matching combo is pressed. Multiple listeners for the same
 * action all fire (rare; reserved for chrome + content swaps).
 */

import { useEffect } from 'react';
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type ShortcutAction =
  | 'search'              // open the global search modal
  | 'new_dm'              // start a new direct message
  | 'new_group'           // create a new group
  | 'toggle_mute'         // toggle local mic in active call
  | 'toggle_video'        // toggle camera in active call
  | 'end_call'            // hangup active call
  | 'jump_to_unread'      // jump to next unread channel
  | 'mark_all_read';      // mark every channel read

export interface ShortcutInfo {
  action: ShortcutAction;
  label: string;
  defaultCombo: string;
}

export const SHORTCUTS_CATALOG: ShortcutInfo[] = [
  { action: 'search',          label: 'البحث الشامل',         defaultCombo: 'Ctrl+K' },
  { action: 'new_dm',          label: 'محادثة شخصية جديدة',  defaultCombo: 'Ctrl+N' },
  { action: 'new_group',       label: 'مجموعة جديدة',         defaultCombo: 'Ctrl+Shift+N' },
  { action: 'toggle_mute',     label: 'كتم/تشغيل الميكروفون',  defaultCombo: 'Ctrl+Shift+M' },
  { action: 'toggle_video',    label: 'الكاميرا تشغيل/إيقاف',   defaultCombo: 'Ctrl+Shift+V' },
  { action: 'end_call',        label: 'إنهاء المكالمة',         defaultCombo: 'Ctrl+Shift+H' },
  { action: 'jump_to_unread',  label: 'الانتقال لغير المقروء',   defaultCombo: 'Alt+J' },
  { action: 'mark_all_read',   label: 'تعيين الكل كمقروء',      defaultCombo: 'Alt+Shift+R' },
];


function defaultsMap(): Record<ShortcutAction, string> {
  const m = {} as Record<ShortcutAction, string>;
  for (const s of SHORTCUTS_CATALOG) m[s.action] = s.defaultCombo;
  return m;
}


interface ShortcutsState {
  bindings: Record<ShortcutAction, string>;
  setBinding: (action: ShortcutAction, combo: string) => void;
  resetAction: (action: ShortcutAction) => void;
  resetAll: () => void;
  getCombo: (action: ShortcutAction) => string;
}

export const useKeyboardShortcutsStore = create<ShortcutsState>()(
  persist(
    (set, get) => ({
      bindings: defaultsMap(),
      setBinding: (action, combo) =>
        set((s) => ({
          bindings: { ...s.bindings, [action]: normalizeCombo(combo) },
        })),
      resetAction: (action) => {
        const def = SHORTCUTS_CATALOG.find((s) => s.action === action);
        if (!def) return;
        set((s) => ({
          bindings: { ...s.bindings, [action]: def.defaultCombo },
        }));
      },
      resetAll: () => set({ bindings: defaultsMap() }),
      getCombo: (action) => get().bindings[action]
        || SHORTCUTS_CATALOG.find((s) => s.action === action)?.defaultCombo
        || '',
    }),
    { name: 'helen.keyboard-shortcuts.v1' },
  ),
);


/** Normalize a key combo string: modifier order, capitalization. */
export function normalizeCombo(combo: string): string {
  if (!combo) return '';
  const parts = combo.split('+').map((p) => p.trim()).filter(Boolean);
  const has = (m: string) => parts.some(
    (p) => p.toLowerCase() === m.toLowerCase(),
  );
  const mods: string[] = [];
  if (has('Ctrl') || has('Control')) mods.push('Ctrl');
  if (has('Alt')) mods.push('Alt');
  if (has('Shift')) mods.push('Shift');
  if (has('Meta') || has('Cmd') || has('Super')) mods.push('Meta');
  const key = parts.find(
    (p) => !['ctrl', 'control', 'alt', 'shift',
              'meta', 'cmd', 'super'].includes(p.toLowerCase()),
  );
  if (!key) return mods.join('+');
  // Capitalize single letters; keep multichar names as typed.
  const keyName = key.length === 1
    ? key.toUpperCase()
    : key.slice(0, 1).toUpperCase() + key.slice(1);
  return [...mods, keyName].join('+');
}


/** Build the canonical combo token from a KeyboardEvent. */
export function comboFromEvent(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push('Ctrl');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  if (e.metaKey) parts.push('Meta');
  // Skip pure modifier presses; they're not a complete combo.
  const ks = e.key;
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(ks)) return '';
  // Normalize special keys.
  let key = ks;
  if (key.length === 1) key = key.toUpperCase();
  parts.push(key);
  return parts.join('+');
}


/** React hook — register a global listener for the given action. */
export function useShortcutListener(
  action: ShortcutAction,
  handler: (e: KeyboardEvent) => void,
): void {
  const combo = useKeyboardShortcutsStore((s) => s.bindings[action]);

  useEffect(() => {
    if (!combo) return;
    const onKey = (e: KeyboardEvent) => {
      const pressed = comboFromEvent(e);
      if (pressed === combo) {
        handler(e);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [combo, handler]);
}
