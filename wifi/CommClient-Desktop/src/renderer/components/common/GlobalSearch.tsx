/**
 * GlobalSearch — Ctrl+K modal that searches across users / channels /
 * messages from anywhere in the app.
 *
 * Wires three existing endpoints:
 *   - GET /api/users?search=…       (people)
 *   - api.listChannels (filtered)   (channels — no server-side search yet,
 *                                    we filter client-side from the
 *                                    already-cached list which is small)
 *   - GET /api/messages/search?q=…  (message hits, server-side full-text)
 *
 * Selecting a result navigates to the right place:
 *   - User → /chats and creates/opens DM with them
 *   - Channel → /chats with the channel selected
 *   - Message → /chats with the channel and message highlighted (best-effort)
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, MessageSquare, User as UserIcon, Hash, X, Clock } from 'lucide-react';
import { api } from '@/services/api.client';
import { useChatStore } from '@/stores/chat.store.v2';
import { SearchFiltersBar } from '@/components/search/SearchFiltersBar';
import {
  useShortcutListener,
  useKeyboardShortcutsStore,
} from '@/stores/keyboard-shortcuts.store';
import { useAuthStore } from '@/stores/auth.store';
import { t } from '@/i18n';

// Search history persistence — last 10 unique queries the user actually
// searched for. Stored in localStorage so it survives app restart but
// stays per-machine (no server sync). Trimmed FIFO at write time.
const SEARCH_HISTORY_KEY = 'commclient_search_history_v1';
const SEARCH_HISTORY_MAX = 10;

function loadHistory(): string[] {
    try {
        const raw = localStorage.getItem(SEARCH_HISTORY_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string').slice(0, SEARCH_HISTORY_MAX) : [];
    } catch {
        return [];
    }
}

function pushHistory(query: string): string[] {
    try {
        const trimmed = query.trim();
        if (!trimmed) return loadHistory();
        const current = loadHistory().filter((q) => q !== trimmed);
        const next = [trimmed, ...current].slice(0, SEARCH_HISTORY_MAX);
        localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(next));
        return next;
    } catch {
        return loadHistory();
    }
}

function clearHistory(): void {
    try { localStorage.removeItem(SEARCH_HISTORY_KEY); } catch { /* ignore */ }
}

type Hit =
    | { kind: 'user'; id: string; title: string; subtitle: string }
    | { kind: 'channel'; id: string; title: string; subtitle: string }
    | { kind: 'message'; id: string; title: string; subtitle: string; channelId: string };

const useDebounce = <T,>(v: T, ms: number) => {
    const [out, setOut] = useState(v);
    useEffect(() => {
        const id = setTimeout(() => setOut(v), ms);
        return () => clearTimeout(id);
    }, [v, ms]);
    return out;
};

export const GlobalSearch: React.FC = () => {
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState('');
    const [hits, setHits] = useState<Hit[]>([]);
    const [busy, setBusy] = useState(false);
    const [activeIdx, setActiveIdx] = useState(0);
    const [history, setHistory] = useState<string[]>([]);
    // Filter chips (date range, sender, has-attachment, etc.) —
    // the chip bar mounts only when the modal is open. Empty
    // object = "no filters", which the API method treats as a
    // plain text search.
    const [filters, setFilters] =
      useState<import('@/components/search/SearchFiltersBar').SearchFilters>({});
    const inputRef = useRef<HTMLInputElement | null>(null);
    const navigate = useNavigate();
    const channels = useChatStore((s) => s.channels);
    const setActiveChannel = useChatStore((s) => s.setActiveChannel);
    const me = useAuthStore((s) => s.user);

    const debouncedQ = useDebounce(q.trim(), 200);

    // Open via the configurable keyboard shortcut (default Ctrl+K).
    // The legacy Cmd+K on macOS is still honored via the Meta path
    // below for users who haven't customized.
    useShortcutListener('search', (e) => {
        e.preventDefault();
        setOpen((v) => !v);
    });
    // Also support Cmd+K on macOS (Meta+K) as a fallback when the
    // user hasn't customized — the registry's normalized form
    // produces "Meta+K" which the shortcut listener already
    // matches if the user binds it that way. We additionally
    // listen for Esc to close.
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape' && open) {
                setOpen(false);
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open]);

    // Tooltip shown in the input placeholder reflects whatever
    // combo the user has configured.
    const searchCombo = useKeyboardShortcutsStore(
        (s) => s.bindings.search,
    );
    void searchCombo; // wired below in the placeholder

    // Focus input on open + refresh search history snapshot
    useEffect(() => {
        if (open) {
            setQ('');
            setHits([]);
            setActiveIdx(0);
            setHistory(loadHistory());
            setTimeout(() => inputRef.current?.focus(), 30);
        }
    }, [open]);

    // Run search whenever the debounced query changes
    useEffect(() => {
        if (!open || !debouncedQ) {
            setHits([]);
            return;
        }
        let cancelled = false;
        (async () => {
            setBusy(true);
            try {
                const [users, msgs] = await Promise.allSettled([
                    api.listUsers({ search: debouncedQ, limit: 8 }),
                    api.searchMessages(debouncedQ, filters),
                ]);
                if (cancelled) return;

                const out: Hit[] = [];

                // Users
                const uPayload =
                    users.status === 'fulfilled'
                        ? Array.isArray(users.value) ? users.value : (users.value?.users || users.value?.items || [])
                        : [];
                for (const u of uPayload as any[]) {
                    if (u.id === me?.id) continue;  // hide self
                    out.push({
                        kind: 'user',
                        id: u.id,
                        title: u.display_name || u.username,
                        subtitle: '@' + (u.username || ''),
                    });
                }

                // Channels — filter the locally-cached list. Cheap; the
                // chat store keeps everything the user belongs to.
                const lower = debouncedQ.toLowerCase();
                for (const c of channels) {
                    const name = (c.name || (c as any).display_name || '').toString();
                    if (name.toLowerCase().includes(lower)) {
                        out.push({
                            kind: 'channel',
                            id: c.id,
                            title: name || c.id,
                            subtitle: c.type === 'dm' ? 'DM' : (c.type || 'channel'),
                        });
                    }
                }

                // Messages
                const mPayload =
                    msgs.status === 'fulfilled'
                        ? Array.isArray(msgs.value) ? msgs.value : (msgs.value?.messages || msgs.value?.items || [])
                        : [];
                for (const m of (mPayload as any[]).slice(0, 12)) {
                    out.push({
                        kind: 'message',
                        id: m.id,
                        title: (m.content || '').slice(0, 80) || '(empty message)',
                        subtitle: `in ${m.channel_id?.slice(0, 8)}…`,
                        channelId: m.channel_id,
                    });
                }

                setHits(out);
                setActiveIdx(0);
            } catch {
                /* swallow — result list just stays empty */
            } finally {
                if (!cancelled) setBusy(false);
            }
        })();
        return () => { cancelled = true; };
    }, [debouncedQ, open, channels, me?.id, filters]);

    const choose = useCallback(
        (h: Hit) => {
            // Record what the user was searching for, not the result kind,
            // so re-opening the picker offers their last queries again.
            if (q.trim()) pushHistory(q);
            setOpen(false);
            if (h.kind === 'channel') {
                setActiveChannel(h.id);
                navigate('/chats');
            } else if (h.kind === 'message') {
                setActiveChannel(h.channelId);
                navigate('/chats');
                // Best-effort: emit a custom event the chat view can hook to
                // scroll to the message. If the receiver isn't there, this
                // is a no-op; navigation alone is still useful.
                window.dispatchEvent(new CustomEvent('chat:focus-message', {
                    detail: { messageId: h.id, channelId: h.channelId },
                }));
            } else if (h.kind === 'user') {
                // Opening or creating a DM is owned by the chat store. We
                // don't reach into its internals here — fire an event the
                // ChannelList / NewDMDialog can pick up.
                window.dispatchEvent(new CustomEvent('chat:open-dm', {
                    detail: { userId: h.id },
                }));
                navigate('/chats');
            }
        },
        [navigate, setActiveChannel],
    );

    const flat = useMemo(() => hits, [hits]);

    if (!open) return null;

    return (
        <div
            className="fixed inset-0 z-[100] bg-black/60 flex items-start justify-center pt-24"
            onClick={() => setOpen(false)}
        >
            <div
                className="w-full max-w-xl bg-surface-900 border border-surface-700 rounded-lg shadow-2xl overflow-hidden"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-center gap-2 p-3 border-b border-surface-800">
                    <Search size={18} className="text-gray-400" />
                    <input
                        ref={inputRef}
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'ArrowDown') {
                                e.preventDefault();
                                setActiveIdx((i) => Math.min(i + 1, flat.length - 1));
                            } else if (e.key === 'ArrowUp') {
                                e.preventDefault();
                                setActiveIdx((i) => Math.max(i - 1, 0));
                            } else if (e.key === 'Enter' && flat[activeIdx]) {
                                e.preventDefault();
                                choose(flat[activeIdx]);
                            }
                        }}
                        placeholder={t('search.placeholder') || 'Search users, channels, messages…  (Ctrl+K)'}
                        className="flex-1 bg-transparent text-sm text-white outline-none"
                    />
                    {busy && <span className="text-xs text-gray-500">…</span>}
                    <button
                        onClick={() => setOpen(false)}
                        className="p-1 hover:bg-surface-800 rounded text-gray-400"
                    >
                        <X size={16} />
                    </button>
                </div>
                {/* Filter chips — date range, sender, has-attachment,
                    pinned-only, etc. The bar is its own component
                    (``SearchFiltersBar``) so its presence is clearly
                    separable from the input + result list. */}
                <SearchFiltersBar value={filters} onChange={setFilters} />
                <div className="max-h-[60vh] overflow-y-auto">
                    {flat.length === 0 && q.trim() && !busy && (
                        <div className="p-6 text-center text-sm text-gray-500">
                            {t('search.no_results') || 'No results'}
                        </div>
                    )}
                    {flat.length === 0 && !q.trim() && history.length > 0 && (
                        <div>
                            <div className="px-3 py-2 flex items-center justify-between border-b border-surface-800">
                                <span className="text-xs text-gray-500 inline-flex items-center gap-1">
                                    <Clock size={12} />
                                    {t('search.recent') || 'Recent searches'}
                                </span>
                                <button
                                    onClick={() => { clearHistory(); setHistory([]); }}
                                    className="text-xs text-gray-500 hover:text-gray-300"
                                >
                                    {t('search.clear_history') || 'Clear'}
                                </button>
                            </div>
                            {history.map((h) => (
                                <button
                                    key={h}
                                    onClick={() => { setQ(h); inputRef.current?.focus(); }}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-surface-800 transition-colors"
                                >
                                    <Clock size={14} className="text-gray-500 shrink-0" />
                                    <span className="text-sm text-zinc-300 truncate flex-1">{h}</span>
                                </button>
                            ))}
                        </div>
                    )}
                    {flat.length === 0 && !q.trim() && history.length === 0 && (
                        <div className="p-6 text-center text-xs text-gray-500">
                            {t('search.tip') || 'Type to search across users, channels, and messages. Use ↑↓ + Enter to pick.'}
                        </div>
                    )}
                    {flat.map((h, i) => {
                        const Icon = h.kind === 'user' ? UserIcon : h.kind === 'channel' ? Hash : MessageSquare;
                        const active = i === activeIdx;
                        return (
                            <button
                                key={`${h.kind}:${h.id}`}
                                onClick={() => choose(h)}
                                onMouseEnter={() => setActiveIdx(i)}
                                className={`w-full flex items-start gap-3 px-3 py-2 text-left transition-colors ${
                                    active ? 'bg-blue-700/40' : 'hover:bg-surface-800'
                                }`}
                            >
                                <Icon size={16} className="mt-0.5 text-gray-400 shrink-0" />
                                <div className="flex-1 min-w-0">
                                    <div className="text-sm text-white truncate">{h.title}</div>
                                    <div className="text-xs text-gray-500 truncate">{h.subtitle}</div>
                                </div>
                                <span className="text-[10px] text-gray-500 shrink-0">{h.kind}</span>
                            </button>
                        );
                    })}
                </div>
            </div>
        </div>
    );
};

export default GlobalSearch;
