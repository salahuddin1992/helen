/**
 * NotificationSoundsPanel — settings panel for per-event-type
 * notification sounds. Lives next to the existing settings cards
 * but in its own file so the SettingsView stays digestible.
 *
 * Layout:
 *   * Master mute toggle.
 *   * Master volume slider (0..100).
 *   * One row per EventKind: dropdown of sound IDs + ▶ preview.
 */

import React from 'react';
import { Volume2, VolumeX, Play } from 'lucide-react';
import {
  useNotificationSoundsStore,
  type EventKind,
} from '@/stores/notification-sounds.store';
import { catalog, preview } from '@/services/notification-sounds';

const EVENT_LABELS: Record<EventKind, string> = {
  message: 'رسالة جديدة',
  mention: 'إشارة (@)',
  dm: 'محادثة شخصية جديدة',
  call: 'مكالمة واردة',
  system: 'تنبيه نظام',
};

const EVENT_ORDER: EventKind[] = [
  'message', 'mention', 'dm', 'call', 'system',
];

export const NotificationSoundsPanel: React.FC = () => {
  const muted = useNotificationSoundsStore((s) => s.master_muted);
  const volume = useNotificationSoundsStore((s) => s.master_volume);
  const sounds = useNotificationSoundsStore((s) => s.sounds);
  const setMuted = useNotificationSoundsStore((s) => s.setMuted);
  const setVolume = useNotificationSoundsStore((s) => s.setVolume);
  const setSound = useNotificationSoundsStore((s) => s.setSound);

  return (
    <div className="bg-surface-900 border border-surface-700
                    rounded-lg p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-100">
          أصوات الإشعارات
        </h3>
        <button
          type="button"
          onClick={() => setMuted(!muted)}
          className={
            'p-2 rounded transition ' +
            (muted
              ? 'bg-red-700/30 text-red-300 hover:bg-red-700/50'
              : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
          }
          title={muted ? 'إعادة التفعيل' : 'كتم الكل'}
        >
          {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
        </button>
      </div>

      {/* Master volume */}
      <div className="flex items-center gap-3">
        <span className="text-xs text-gray-400 w-14">المستوى</span>
        <input
          type="range"
          min={0}
          max={100}
          value={Math.round(volume * 100)}
          onChange={(e) => setVolume(Number(e.target.value) / 100)}
          disabled={muted}
          className="flex-1 disabled:opacity-50"
        />
        <span className="text-xs text-gray-400 w-10 text-end">
          {Math.round(volume * 100)}%
        </span>
      </div>

      {/* Per-event chooser */}
      <div className="space-y-2 pt-2 border-t border-surface-800">
        {EVENT_ORDER.map((kind) => (
          <div
            key={kind}
            className="flex items-center gap-2 text-xs"
          >
            <span className="flex-1 text-gray-300">
              {EVENT_LABELS[kind]}
            </span>
            <select
              value={sounds[kind]}
              onChange={(e) =>
                setSound(kind, e.target.value as any)
              }
              className="px-2 py-1 bg-surface-800 border
                         border-surface-700 rounded text-gray-100"
            >
              {catalog.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => preview(sounds[kind], volume)}
              disabled={muted || sounds[kind] === 'none'}
              className="p-1.5 rounded bg-surface-700
                         hover:bg-surface-600 disabled:opacity-40
                         text-gray-200"
              title="معاينة"
            >
              <Play size={11} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};
