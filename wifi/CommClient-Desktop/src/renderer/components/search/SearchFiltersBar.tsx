/**
 * SearchFiltersBar — chip-style filter row for the global message
 * search modal. Lives in its own file so the search dialog stays
 * focused on result rendering.
 *
 * Emits a structured filter object (the same shape ``api.searchMessages``
 * accepts as its second argument) every time the user toggles a chip
 * or picks a date. The parent owns the filter state.
 *
 * Filters surfaced (matched to what the server already supports —
 * see ``messages.py:search_messages``):
 *
 *   * ``has_file``        — has any attachment
 *   * ``msg_type=image``  — pictures only
 *   * ``is_pinned``       — pinned messages
 *   * ``has_reactions``   — messages with reactions
 *   * ``date_from``       — from a calendar-picked date
 *   * ``date_to``         — until a calendar-picked date
 *
 * The "from sender" filter takes a free-text username; we route it
 * via ``sender_username`` rather than ``sender_id`` because users
 * type names, not UUIDs.
 */

import React from 'react';
import { Paperclip, Image as ImageIcon, Pin, Smile, X } from 'lucide-react';

export interface SearchFilters {
  has_file?: boolean;
  msg_type?: 'image';
  is_pinned?: boolean;
  has_reactions?: boolean;
  date_from?: string;
  date_to?: string;
  sender_username?: string;
}

interface Props {
  value: SearchFilters;
  onChange: (next: SearchFilters) => void;
}

interface ChipDef {
  key: keyof SearchFilters;
  label: string;
  // Lucide icons accept ``size`` as ``number | string`` — keep this
  // permissive so any ``LucideIcon`` slots in.
  Icon: React.ComponentType<any>;
  /** Value applied when the chip is toggled on. */
  apply: SearchFilters;
}

const CHIPS: ChipDef[] = [
  {
    key: 'has_file', label: 'مرفقات',
    Icon: Paperclip,
    apply: { has_file: true },
  },
  {
    key: 'msg_type', label: 'صور',
    Icon: ImageIcon,
    apply: { msg_type: 'image' },
  },
  {
    key: 'is_pinned', label: 'مثبَّتة',
    Icon: Pin,
    apply: { is_pinned: true },
  },
  {
    key: 'has_reactions', label: 'مع تفاعلات',
    Icon: Smile,
    apply: { has_reactions: true },
  },
];

function isChipActive(value: SearchFilters, chip: ChipDef): boolean {
  const probe = chip.apply[chip.key];
  return (value as any)[chip.key] === probe;
}

export const SearchFiltersBar: React.FC<Props> = ({
  value, onChange,
}) => {
  const toggleChip = (chip: ChipDef) => {
    if (isChipActive(value, chip)) {
      const next = { ...value };
      delete (next as any)[chip.key];
      onChange(next);
    } else {
      onChange({ ...value, ...chip.apply });
    }
  };

  const updateField = (k: keyof SearchFilters, v: any) => {
    const next = { ...value };
    if (v == null || v === '') delete (next as any)[k];
    else (next as any)[k] = v;
    onChange(next);
  };

  const activeCount = Object.keys(value).length;

  return (
    <div className="px-3 py-2 border-b border-surface-800 space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {CHIPS.map((c) => {
          const on = isChipActive(value, c);
          return (
            <button
              key={c.key}
              onClick={() => toggleChip(c)}
              className={
                'flex items-center gap-1 px-2 py-1 text-[11px] rounded ' +
                (on
                  ? 'bg-blue-700 text-white'
                  : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
              }
              type="button"
            >
              <c.Icon size={11} />
              <span>{c.label}</span>
            </button>
          );
        })}
        {activeCount > 0 && (
          <button
            onClick={() => onChange({})}
            className="flex items-center gap-1 px-2 py-1 text-[11px]
                       rounded bg-red-700/30 text-red-200
                       hover:bg-red-700/50"
            type="button"
            title="مسح كل الفلاتر"
          >
            <X size={11} />
            <span>مسح</span>
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px]
                      text-gray-400">
        <label className="flex items-center gap-1">
          من
          <input
            type="date"
            value={value.date_from?.slice(0, 10) || ''}
            onChange={(e) =>
              updateField(
                'date_from',
                e.target.value ? `${e.target.value}T00:00:00` : '',
              )
            }
            className="px-1 py-0.5 bg-surface-800 border
                       border-surface-700 rounded text-gray-100"
          />
        </label>
        <label className="flex items-center gap-1">
          إلى
          <input
            type="date"
            value={value.date_to?.slice(0, 10) || ''}
            onChange={(e) =>
              updateField(
                'date_to',
                e.target.value ? `${e.target.value}T23:59:59` : '',
              )
            }
            className="px-1 py-0.5 bg-surface-800 border
                       border-surface-700 rounded text-gray-100"
          />
        </label>
        <label className="flex items-center gap-1 flex-1 min-w-[140px]">
          المرسل
          <input
            type="text"
            value={value.sender_username || ''}
            onChange={(e) =>
              updateField('sender_username', e.target.value)
            }
            placeholder="@username"
            className="flex-1 px-1.5 py-0.5 bg-surface-800 border
                       border-surface-700 rounded text-gray-100
                       placeholder-gray-500"
          />
        </label>
      </div>
    </div>
  );
};
