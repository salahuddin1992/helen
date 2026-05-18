/**
 * CustomEmojiPicker — popover that lists every server-side
 * custom emoji + a starter set of Unicode favorites + a search
 * input. Click an entry to insert it into the active text input.
 *
 * Inserts:
 *   * Custom emoji as ``:shortcode:`` (server's renderers + the
 *     message bubble swap that for an <img> by shortcode).
 *   * Unicode emoji as the literal grapheme.
 *
 * The custom-emoji catalog is fetched on first open and cached
 * for the rest of the session — admins rarely add emoji while a
 * user has the picker pinned.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import { api } from '@/services/api.client';

interface CustomEmojiRow {
  id: string;
  shortcode: string;
  url: string;
  description: string;
}

interface Props {
  onPick: (token: string) => void;
  onClose: () => void;
}

const UNICODE_FAVORITES = [
  '👍', '❤️', '😂', '🎉', '🔥', '👏', '🙏', '😢',
  '😮', '🤔', '👀', '✅', '❌', '💯', '🚀', '⭐',
  '😡', '🥳', '🤝', '💡', '☕', '🎁', '🌹', '🤣',
];

let _cachedList: CustomEmojiRow[] | null = null;

async function fetchListCached(): Promise<CustomEmojiRow[]> {
  if (_cachedList) return _cachedList;
  try {
    const r = await api.customEmoji.list();
    _cachedList = r.emoji.map((e) => ({
      id: e.id,
      shortcode: e.shortcode,
      url: api.customEmoji.rawUrl(e.id),
      description: e.description || '',
    }));
    return _cachedList;
  } catch {
    return [];
  }
}

/** Reset the cache — useful from the admin panel after a new
 *  upload so the picker reflects the change without a relaunch. */
export function invalidateCustomEmojiCache(): void {
  _cachedList = null;
}

export const CustomEmojiPicker: React.FC<Props> = ({
  onPick, onClose,
}) => {
  const [query, setQuery] = useState('');
  const [list, setList] = useState<CustomEmojiRow[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    fetchListCached().then((rows) => setList(rows));
    inputRef.current?.focus();
  }, []);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const filteredCustom = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (e) =>
        e.shortcode.toLowerCase().includes(q) ||
        e.description.toLowerCase().includes(q),
    );
  }, [list, query]);

  return (
    <div
      className="absolute bottom-full mb-2 right-0 z-40 w-80
                 max-h-96 bg-surface-900 border border-surface-700
                 rounded-lg shadow-xl flex flex-col"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center gap-2 p-2 border-b
                      border-surface-800">
        <Search size={13} className="text-gray-500" />
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="بحث…"
          className="flex-1 bg-transparent text-xs text-gray-100
                     placeholder-gray-500 outline-none"
        />
        <button
          onClick={onClose}
          className="p-0.5 rounded hover:bg-surface-700"
        >
          <X size={12} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-3">
        {/* Custom (server) emoji */}
        <div>
          <div className="text-[10px] text-gray-400 mb-1
                          uppercase tracking-wide">
            مخصّص ({filteredCustom.length})
          </div>
          {filteredCustom.length === 0 ? (
            <div className="text-[11px] text-gray-500 py-2">
              لا توجد إيموجي مخصّصة بعد
            </div>
          ) : (
            <div className="grid grid-cols-8 gap-1">
              {filteredCustom.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  onClick={() => onPick(`:${e.shortcode}:`)}
                  className="aspect-square flex items-center
                             justify-center rounded
                             hover:bg-surface-700"
                  title={`:${e.shortcode}:${e.description ? ` — ${e.description}` : ''}`}
                >
                  <img
                    src={e.url}
                    alt={e.shortcode}
                    className="max-w-[24px] max-h-[24px]
                               object-contain"
                  />
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Unicode favorites */}
        {!query && (
          <div>
            <div className="text-[10px] text-gray-400 mb-1
                            uppercase tracking-wide">
              مفضّلة
            </div>
            <div className="grid grid-cols-8 gap-1">
              {UNICODE_FAVORITES.map((u) => (
                <button
                  key={u}
                  type="button"
                  onClick={() => onPick(u)}
                  className="aspect-square flex items-center
                             justify-center rounded
                             hover:bg-surface-700 text-lg"
                >
                  {u}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
