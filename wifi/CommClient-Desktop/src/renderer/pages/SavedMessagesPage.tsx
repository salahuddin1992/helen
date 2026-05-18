/**
 * SavedMessagesPage — list / search / organize messages the user has
 * bookmarked. Backed by /api/saved (CRUD) + /api/saved/folders.
 *
 * Wires into the existing chat surface: clicking a saved message
 * dispatches the same `chat:focus-message` custom event as GlobalSearch
 * so the chat view can scroll the parent channel to the right place.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bookmark, Folder, Trash2, Edit2, RefreshCw, Search, X } from 'lucide-react';
import { api } from '@/services/api.client';
import { t } from '@/i18n';

interface SavedItem {
    id: string;
    message_id: string;
    folder: string | null;
    note: string | null;
    saved_at: string | null;
    content: string | null;
    sender_username: string | null;
    channel_id: string | null;
}

const fmtDate = (iso?: string | null) => {
    if (!iso) return '';
    try {
        return new Date(iso).toLocaleString();
    } catch {
        return iso;
    }
};

export const SavedMessagesPage: React.FC = () => {
    const [items, setItems] = useState<SavedItem[]>([]);
    const [folders, setFolders] = useState<string[]>([]);
    const [activeFolder, setActiveFolder] = useState<string | null>(null);
    const [search, setSearch] = useState('');
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState<string | null>(null);
    const navigate = useNavigate();

    const load = useCallback(async () => {
        setBusy(true);
        try {
            const [list, fs] = await Promise.allSettled([
                api.savedMessages.list({
                    folder: activeFolder || undefined,
                    limit: 200,
                }),
                api.savedMessages.folders(),
            ]);
            if (list.status === 'fulfilled') setItems(list.value.items || []);
            if (fs.status === 'fulfilled') setFolders(fs.value.folders || []);
            setErr(null);
        } catch (e: any) {
            setErr(e?.message || 'Failed to load');
        } finally {
            setBusy(false);
        }
    }, [activeFolder]);

    useEffect(() => { load(); }, [load]);

    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return items;
        return items.filter((i) => {
            const hay = `${i.content || ''} ${i.note || ''} ${i.sender_username || ''} ${i.folder || ''}`.toLowerCase();
            return hay.includes(q);
        });
    }, [items, search]);

    const open = (item: SavedItem) => {
        if (!item.channel_id) return;
        window.dispatchEvent(new CustomEvent('chat:focus-message', {
            detail: { messageId: item.message_id, channelId: item.channel_id },
        }));
        navigate('/chats');
    };

    const editNote = async (item: SavedItem) => {
        const note = window.prompt(
            t('saved.edit_note_prompt') || 'Edit note (empty to clear):',
            item.note || '',
        );
        if (note === null) return;
        try {
            await api.savedMessages.update(item.message_id, {
                note: note.trim() || null,
            });
            await load();
        } catch (e: any) {
            setErr(e?.message);
        }
    };

    const moveToFolder = async (item: SavedItem) => {
        const folder = window.prompt(
            t('saved.move_folder_prompt') || 'Folder name (empty for none):',
            item.folder || '',
        );
        if (folder === null) return;
        try {
            await api.savedMessages.update(item.message_id, {
                folder: folder.trim() || null,
            });
            await load();
        } catch (e: any) {
            setErr(e?.message);
        }
    };

    const remove = async (item: SavedItem) => {
        if (!window.confirm(t('saved.confirm_delete') || 'Remove from saved?')) return;
        try {
            await api.savedMessages.remove(item.message_id);
            await load();
        } catch (e: any) {
            setErr(e?.message);
        }
    };

    return (
        <div className="h-full flex flex-col bg-surface-950">
            <div className="px-6 py-4 border-b border-surface-800 flex items-center gap-3">
                <Bookmark className="text-yellow-500" size={20} />
                <h1 className="text-lg font-semibold text-white">
                    {t('saved.title') || 'Saved Messages'}
                </h1>
                <span className="text-xs text-gray-500">{filtered.length}</span>
                <div className="flex-1" />
                <button
                    onClick={load}
                    disabled={busy}
                    className="p-2 hover:bg-surface-800 rounded text-gray-300"
                    title={t('admin.refresh') || 'Refresh'}
                >
                    <RefreshCw size={16} className={busy ? 'animate-spin' : ''} />
                </button>
            </div>

            <div className="px-6 py-3 border-b border-surface-800 flex items-center gap-2 overflow-x-auto">
                <button
                    onClick={() => setActiveFolder(null)}
                    className={`px-3 py-1 rounded text-xs whitespace-nowrap ${
                        activeFolder === null
                            ? 'bg-blue-600 text-white'
                            : 'bg-surface-800 text-gray-300 hover:bg-surface-700'
                    }`}
                >
                    {t('saved.all') || 'All'}
                </button>
                {folders.map((f) => (
                    <button
                        key={f}
                        onClick={() => setActiveFolder(f)}
                        className={`px-3 py-1 rounded text-xs whitespace-nowrap inline-flex items-center gap-1 ${
                            activeFolder === f
                                ? 'bg-blue-600 text-white'
                                : 'bg-surface-800 text-gray-300 hover:bg-surface-700'
                        }`}
                    >
                        <Folder size={12} />
                        {f}
                    </button>
                ))}
                <div className="flex-1" />
                <div className="relative">
                    <Search
                        size={14}
                        className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-500"
                    />
                    <input
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder={t('saved.search_placeholder') || 'Search saved…'}
                        className="pl-7 pr-3 py-1 bg-surface-800 border border-surface-700 rounded text-xs text-white outline-none w-48"
                    />
                </div>
            </div>

            {err && (
                <div className="mx-6 my-3 px-3 py-2 bg-red-900/40 border border-red-700 rounded text-red-300 text-xs flex items-center gap-2">
                    <X size={14} />
                    {err}
                </div>
            )}

            <div className="flex-1 overflow-auto px-6 py-4">
                {filtered.length === 0 && !busy && (
                    <div className="text-center text-gray-500 text-sm py-12">
                        <Bookmark size={32} className="mx-auto mb-2 opacity-30" />
                        {t('saved.empty') || 'No saved messages yet.'}
                    </div>
                )}
                <div className="space-y-3">
                    {filtered.map((item) => (
                        <div
                            key={item.id}
                            className="bg-surface-800 border border-surface-700 rounded-lg p-4 hover:border-surface-600 transition-colors"
                        >
                            <div className="flex items-start justify-between gap-2 mb-2">
                                <div className="flex-1 min-w-0">
                                    <button
                                        onClick={() => open(item)}
                                        className="text-left w-full"
                                    >
                                        <div className="text-sm text-white whitespace-pre-wrap break-words">
                                            {item.content || (
                                                <span className="text-gray-500 italic">
                                                    {t('saved.message_unavailable') || 'Message unavailable'}
                                                </span>
                                            )}
                                        </div>
                                    </button>
                                    <div className="text-xs text-gray-500 mt-1 flex items-center gap-2 flex-wrap">
                                        {item.sender_username && (
                                            <span>@{item.sender_username}</span>
                                        )}
                                        <span>·</span>
                                        <span>{fmtDate(item.saved_at)}</span>
                                        {item.folder && (
                                            <>
                                                <span>·</span>
                                                <span className="inline-flex items-center gap-1 text-blue-400">
                                                    <Folder size={11} />
                                                    {item.folder}
                                                </span>
                                            </>
                                        )}
                                    </div>
                                </div>
                                <div className="flex gap-1 shrink-0">
                                    <button
                                        onClick={() => editNote(item)}
                                        className="p-1.5 hover:bg-surface-700 rounded text-gray-400"
                                        title={t('saved.edit_note') || 'Edit note'}
                                    >
                                        <Edit2 size={12} />
                                    </button>
                                    <button
                                        onClick={() => moveToFolder(item)}
                                        className="p-1.5 hover:bg-surface-700 rounded text-gray-400"
                                        title={t('saved.move_folder') || 'Move to folder'}
                                    >
                                        <Folder size={12} />
                                    </button>
                                    <button
                                        onClick={() => remove(item)}
                                        className="p-1.5 hover:bg-surface-700 rounded text-red-400"
                                        title={t('saved.delete') || 'Delete'}
                                    >
                                        <Trash2 size={12} />
                                    </button>
                                </div>
                            </div>
                            {item.note && (
                                <div className="mt-2 px-3 py-2 bg-yellow-900/20 border-l-2 border-yellow-500 rounded text-xs text-yellow-200 whitespace-pre-wrap">
                                    {item.note}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
};

export default SavedMessagesPage;
