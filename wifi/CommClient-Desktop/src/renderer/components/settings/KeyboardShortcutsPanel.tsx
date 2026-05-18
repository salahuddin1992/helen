/**
 * KeyboardShortcutsPanel — settings panel that lists every
 * registered shortcut + lets the user remap each one.
 *
 * Click "تغيير" on a row to enter capture mode — the next key
 * combo pressed becomes the new binding (Esc cancels). Each row
 * also has a "افتراضي" link to revert just that row.
 */

import React, { useEffect, useState } from 'react';
import { Keyboard, X } from 'lucide-react';
import {
  useKeyboardShortcutsStore,
  SHORTCUTS_CATALOG,
  comboFromEvent,
  type ShortcutAction,
} from '@/stores/keyboard-shortcuts.store';

interface RowProps {
  info: typeof SHORTCUTS_CATALOG[number];
}

const ShortcutRow: React.FC<RowProps> = ({ info }) => {
  const combo = useKeyboardShortcutsStore((s) => s.bindings[info.action]);
  const setBinding = useKeyboardShortcutsStore((s) => s.setBinding);
  const resetAction = useKeyboardShortcutsStore((s) => s.resetAction);
  const [capturing, setCapturing] = useState(false);

  // Capture next keydown while in capture mode.
  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === 'Escape') {
        setCapturing(false);
        return;
      }
      const next = comboFromEvent(e);
      if (!next) return; // pure modifier — ignore, wait for full combo
      setBinding(info.action, next);
      setCapturing(false);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [capturing, info.action, setBinding]);

  const isDefault = combo === info.defaultCombo;

  return (
    <div className="flex items-center gap-2 py-1.5">
      <span className="flex-1 text-sm text-gray-200">{info.label}</span>

      <kbd
        className={
          'px-2 py-0.5 text-[11px] font-mono rounded border ' +
          (capturing
            ? 'bg-amber-700/30 border-amber-400 text-amber-100'
            : 'bg-surface-800 border-surface-700 text-gray-100')
        }
      >
        {capturing ? 'اضغط الآن…' : combo}
      </kbd>

      <button
        type="button"
        onClick={() => setCapturing((v) => !v)}
        className={
          'px-2 py-1 text-[11px] rounded ' +
          (capturing
            ? 'bg-red-700/30 text-red-200 hover:bg-red-700/50'
            : 'bg-surface-700 text-gray-200 hover:bg-surface-600')
        }
      >
        {capturing ? <X size={11} /> : 'تغيير'}
      </button>

      <button
        type="button"
        onClick={() => resetAction(info.action)}
        disabled={isDefault}
        className="px-2 py-1 text-[11px] text-gray-300
                   hover:text-white disabled:opacity-30
                   disabled:cursor-not-allowed"
        title={isDefault ? 'هذا الإعداد الافتراضي بالفعل' : 'إعادة الافتراضي'}
      >
        افتراضي
      </button>
    </div>
  );
};

export const KeyboardShortcutsPanel: React.FC = () => {
  const resetAll = useKeyboardShortcutsStore((s) => s.resetAll);

  return (
    <div className="bg-surface-900 border border-surface-700
                    rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between mb-2">
        <h3 className="flex items-center gap-2 text-sm
                       font-semibold text-gray-100">
          <Keyboard size={14} />
          اختصارات لوحة المفاتيح
        </h3>
        <button
          type="button"
          onClick={resetAll}
          className="px-2 py-1 text-[11px] bg-surface-700
                     hover:bg-surface-600 rounded text-gray-300"
          title="استعادة كل الاختصارات الافتراضية"
        >
          استعادة الكل
        </button>
      </div>

      <p className="text-xs text-gray-400">
        اضغط «تغيير» ثم اضغط مجموعة المفاتيح الجديدة. Esc للإلغاء.
      </p>

      <div className="divide-y divide-surface-800">
        {SHORTCUTS_CATALOG.map((info) => (
          <ShortcutRow key={info.action} info={info} />
        ))}
      </div>
    </div>
  );
};
