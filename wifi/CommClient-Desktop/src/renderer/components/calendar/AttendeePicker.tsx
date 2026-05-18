/**
 * AttendeePicker — chip-list multi-select that searches the user
 * directory (``api.listUsers``) and turns picked entries into
 * removable pills.
 *
 * Replaces the comma-separated user-id ``<input>`` in
 * ``CalendarPage`` so the operator doesn't have to memorize
 * UUIDs to invite people. The component is controlled — the
 * parent owns the array of user IDs and gets ``onChange`` calls.
 *
 * Search behavior: debounced 250ms, hits ``/api/users?search=…``,
 * keeps the last 8 results in a dropdown. Picking an entry
 * appends it to the chip list and clears the search box.
 */

import React, { useEffect, useRef, useState } from 'react';
import { X, UserPlus, Search } from 'lucide-react';
import { api } from '@/services/api.client';

interface UserHit {
  id: string;
  username: string;
  display_name?: string;
  avatar_url?: string | null;
}

interface Props {
  value: string[];                       // array of user IDs
  onChange: (next: string[]) => void;
  placeholder?: string;
  /** Optional pre-resolved name lookup so chips can show
   *  display_name even when the user hasn't searched. */
  knownUsers?: Record<string, UserHit>;
}

export const AttendeePicker: React.FC<Props> = ({
  value,
  onChange,
  placeholder = 'ابحث عن شخص لإضافته…',
  knownUsers = {},
}) => {
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<UserHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(false);
  const [resolved, setResolved] = useState<Record<string, UserHit>>(
    knownUsers,
  );
  const inputRef = useRef<HTMLInputElement | null>(null);
  const debouncerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounced search.
  useEffect(() => {
    if (debouncerRef.current) clearTimeout(debouncerRef.current);
    if (!query.trim()) {
      setHits([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    debouncerRef.current = setTimeout(async () => {
      try {
        const r = await api.listUsers({ search: query, limit: 8 });
        const list: UserHit[] = (r.users || r || []).slice(0, 8);
        setHits(list);
        // Cache for chip lookup.
        setResolved((prev) => {
          const next = { ...prev };
          for (const u of list) next[u.id] = u;
          return next;
        });
      } catch {
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => {
      if (debouncerRef.current) clearTimeout(debouncerRef.current);
    };
  }, [query]);

  // Resolve any value IDs we don't have a name for (initial mount).
  useEffect(() => {
    const missing = value.filter((id) => !resolved[id]);
    if (missing.length === 0) return;
    let cancelled = false;
    (async () => {
      const newly: Record<string, UserHit> = {};
      for (const id of missing) {
        try {
          const u = await api.getUser(id);
          if (u && u.id) newly[u.id] = u;
        } catch { /* ignore — chip falls back to id */ }
      }
      if (!cancelled && Object.keys(newly).length > 0) {
        setResolved((prev) => ({ ...prev, ...newly }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [value, resolved]);

  const addUser = (u: UserHit) => {
    if (value.includes(u.id)) return;
    onChange([...value, u.id]);
    setQuery('');
    setHits([]);
    inputRef.current?.focus();
  };

  const removeUser = (id: string) => {
    onChange(value.filter((v) => v !== id));
  };

  const visibleHits = hits.filter((h) => !value.includes(h.id));

  return (
    <div className="relative">
      <div
        className="flex flex-wrap items-center gap-1 px-2 py-1.5
                   bg-surface-800 border border-surface-700 rounded
                   focus-within:border-blue-500"
      >
        {value.map((id) => {
          const u = resolved[id];
          const label = u?.display_name || u?.username || id.slice(0, 8);
          return (
            <span
              key={id}
              className="inline-flex items-center gap-1 px-2 py-0.5
                         text-[11px] rounded-full bg-blue-700/40
                         text-blue-100"
            >
              {label}
              <button
                onClick={() => removeUser(id)}
                className="hover:text-white"
                aria-label="Remove"
                type="button"
              >
                <X size={10} />
              </button>
            </span>
          );
        })}
        <div className="flex items-center gap-1 flex-1 min-w-[140px]">
          <Search size={12} className="text-gray-500 flex-none" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setTimeout(() => setFocused(false), 150)}
            placeholder={value.length === 0 ? placeholder : ''}
            className="flex-1 bg-transparent outline-none text-xs
                       text-gray-100 placeholder-gray-500"
          />
        </div>
      </div>

      {/* Dropdown */}
      {focused && (visibleHits.length > 0 || loading) && (
        <div className="absolute z-30 left-0 right-0 mt-1 bg-surface-900
                        border border-surface-700 rounded shadow-lg
                        max-h-60 overflow-y-auto">
          {loading && (
            <div className="px-3 py-2 text-xs text-gray-400">
              جارٍ البحث…
            </div>
          )}
          {visibleHits.map((u) => (
            <button
              key={u.id}
              type="button"
              onMouseDown={(e) => {
                // Use mousedown so onBlur doesn't dismiss the
                // dropdown before our click handler fires.
                e.preventDefault();
                addUser(u);
              }}
              className="w-full text-start flex items-center gap-2
                         px-3 py-1.5 text-xs text-gray-100
                         hover:bg-surface-700"
            >
              <UserPlus size={12} className="text-blue-400" />
              <span className="flex-1 truncate">
                {u.display_name || u.username}
              </span>
              <span className="text-[10px] text-gray-500">
                @{u.username}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
