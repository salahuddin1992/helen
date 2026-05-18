/**
 * KeyboardShortcuts — modal that lists every keyboard shortcut the app
 * registers. Opens on `?` (Shift+/) when no input is focused, or via
 * the explicit menu entry. Closes on Esc.
 *
 * The list is hand-maintained; whenever a new shortcut is added in
 * other components, drop a row here so users can discover it.
 */
import React, { useEffect, useState } from 'react';
import { Keyboard, X } from 'lucide-react';
import { t } from '@/i18n';

interface Shortcut {
    keys: string[];
    description: string;
}

const SHORTCUTS: Shortcut[] = [
    // Global
    { keys: ['Ctrl', 'K'], description: 'Open global search' },
    { keys: ['?'], description: 'Show this help' },
    { keys: ['Esc'], description: 'Close modals / overlays' },
    { keys: ['Ctrl', 'Shift', 'D'], description: 'Toggle debug call panel (dev)' },

    // Navigation
    { keys: ['Ctrl', '1'], description: 'Go to chats' },
    { keys: ['Ctrl', '2'], description: 'Go to contacts' },
    { keys: ['Ctrl', '3'], description: 'Go to calls' },
    { keys: ['Ctrl', ','], description: 'Open settings' },

    // Chat
    { keys: ['Enter'], description: 'Send message' },
    { keys: ['Shift', 'Enter'], description: 'New line' },
    { keys: ['↑'], description: 'Edit your last message (when input is empty)' },

    // Calls
    { keys: ['Space'], description: 'Push-to-talk (when enabled)' },
    { keys: ['Ctrl', 'M'], description: 'Toggle mute' },
    { keys: ['Ctrl', 'E'], description: 'End call' },
];

const KeyChip: React.FC<{ k: string }> = ({ k }) => (
    <kbd className="px-2 py-0.5 bg-surface-700 border border-surface-600 rounded text-xs font-mono text-zinc-100">
        {k}
    </kbd>
);

export const KeyboardShortcuts: React.FC = () => {
    const [open, setOpen] = useState(false);

    // Open on `?` (= Shift+/) when no input/textarea/contentEditable is
    // focused. Close on Esc. We also expose Ctrl+/ as an alternative
    // because some keyboard layouts make `?` awkward to type.
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            const tag = (document.activeElement?.tagName || '').toLowerCase();
            const editable =
                tag === 'input' ||
                tag === 'textarea' ||
                (document.activeElement as HTMLElement | null)?.isContentEditable;
            if (e.key === '?' && !editable && !e.ctrlKey && !e.metaKey) {
                e.preventDefault();
                setOpen((v) => !v);
            } else if ((e.ctrlKey || e.metaKey) && e.key === '/' && !editable) {
                e.preventDefault();
                setOpen((v) => !v);
            } else if (e.key === 'Escape' && open) {
                setOpen(false);
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open]);

    if (!open) return null;

    return (
        <div
            className="fixed inset-0 z-[100] bg-black/60 flex items-center justify-center p-6"
            onClick={() => setOpen(false)}
        >
            <div
                className="w-full max-w-lg bg-surface-900 border border-surface-700 rounded-lg shadow-2xl overflow-hidden"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="px-4 py-3 border-b border-surface-800 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <Keyboard size={18} className="text-blue-400" />
                        <h2 className="text-sm font-semibold text-white">
                            {t('shortcuts.title') || 'Keyboard Shortcuts'}
                        </h2>
                    </div>
                    <button
                        onClick={() => setOpen(false)}
                        className="p-1 hover:bg-surface-800 rounded text-gray-400"
                    >
                        <X size={16} />
                    </button>
                </div>
                <div className="p-4 max-h-[60vh] overflow-y-auto">
                    <ul className="space-y-2">
                        {SHORTCUTS.map((s, i) => (
                            <li key={i} className="flex items-center justify-between text-sm">
                                <span className="text-zinc-300">{s.description}</span>
                                <span className="flex items-center gap-1 shrink-0">
                                    {s.keys.map((k, j) => (
                                        <React.Fragment key={j}>
                                            <KeyChip k={k} />
                                            {j < s.keys.length - 1 && (
                                                <span className="text-gray-500 mx-0.5">+</span>
                                            )}
                                        </React.Fragment>
                                    ))}
                                </span>
                            </li>
                        ))}
                    </ul>
                    <p className="mt-4 text-xs text-gray-500">
                        {t('shortcuts.hint') || 'Press ? again to close.'}
                    </p>
                </div>
            </div>
        </div>
    );
};

export default KeyboardShortcuts;
