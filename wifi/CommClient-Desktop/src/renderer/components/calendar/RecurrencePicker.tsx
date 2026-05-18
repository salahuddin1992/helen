/**
 * RecurrencePicker — UI control that emits an RFC 5545 RRULE.
 *
 * The server already accepts an RRULE string on calendar events
 * (see ``calendar_service.py`` — ``recurrence`` column). What's
 * missing is a way for the user to *build* one without typing the
 * iCalendar dialect by hand. This component wraps three fields:
 *
 *   * Frequency  — none / daily / weekly / monthly / yearly
 *   * Interval   — every N units (1..31)
 *   * Until      — optional end date (UTC midnight)
 *
 * The output is a string suitable for the server's ``recurrence``
 * column (e.g. ``FREQ=WEEKLY;INTERVAL=2;UNTIL=20260901T000000Z``)
 * or ``""`` when frequency is ``none``.
 *
 * Why we *don't* support BYDAY / BYMONTHDAY here
 * ----------------------------------------------
 * RFC 5545 has 11 expansion attributes. Implementing all of them
 * yields a UI no Iraqi grandfather would touch. The 90% case is
 * "every N days/weeks/months until X", and that's what this
 * picker covers. Power users can still POST an arbitrary RRULE
 * via the API directly.
 */

import React from 'react';

export type Frequency = 'none' | 'DAILY' | 'WEEKLY' | 'MONTHLY' | 'YEARLY';

interface Props {
  /** The current RRULE string. ``null`` / ``""`` means no recurrence. */
  value: string | null;
  onChange: (next: string | null) => void;
}

interface Parsed {
  freq: Frequency;
  interval: number;
  untilEpoch: number | null;
}

function parseRRule(raw: string | null): Parsed {
  if (!raw) return { freq: 'none', interval: 1, untilEpoch: null };
  const parts = raw.split(';').reduce<Record<string, string>>((acc, p) => {
    const [k, v] = p.split('=');
    if (k && v) acc[k.toUpperCase()] = v;
    return acc;
  }, {});
  let freq: Frequency = 'none';
  switch ((parts.FREQ || '').toUpperCase()) {
    case 'DAILY':
    case 'WEEKLY':
    case 'MONTHLY':
    case 'YEARLY':
      freq = parts.FREQ.toUpperCase() as Frequency;
  }
  const interval = Math.max(1, parseInt(parts.INTERVAL || '1', 10));
  let untilEpoch: number | null = null;
  if (parts.UNTIL) {
    // RFC 5545: ``YYYYMMDDTHHMMSSZ``.
    const m = parts.UNTIL.match(
      /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/,
    );
    if (m) {
      const d = Date.UTC(
        Number(m[1]), Number(m[2]) - 1, Number(m[3]),
        Number(m[4]), Number(m[5]), Number(m[6]),
      );
      untilEpoch = Math.floor(d / 1000);
    }
  }
  return { freq, interval, untilEpoch };
}

function buildRRule(p: Parsed): string {
  if (p.freq === 'none') return '';
  let out = `FREQ=${p.freq}`;
  if (p.interval > 1) out += `;INTERVAL=${p.interval}`;
  if (p.untilEpoch != null) {
    const d = new Date(p.untilEpoch * 1000);
    const pad = (n: number) => String(n).padStart(2, '0');
    out += ';UNTIL=' +
      `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
      `T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
  }
  return out;
}

function untilToInputDate(epoch: number | null): string {
  if (epoch == null) return '';
  const d = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function inputDateToUntil(value: string): number | null {
  if (!value) return null;
  const [y, m, d] = value.split('-').map(Number);
  if (!y || !m || !d) return null;
  // Pin UNTIL to UTC midnight so the same calendar date applies
  // regardless of the operator's timezone.
  return Math.floor(Date.UTC(y, m - 1, d, 0, 0, 0) / 1000);
}

const FREQ_OPTIONS: Array<{ label: string; value: Frequency }> = [
  { label: 'بدون تكرار', value: 'none' },
  { label: 'يوميّاً', value: 'DAILY' },
  { label: 'أسبوعيّاً', value: 'WEEKLY' },
  { label: 'شهريّاً', value: 'MONTHLY' },
  { label: 'سنويّاً', value: 'YEARLY' },
];

const FREQ_UNIT_LABEL: Record<Exclude<Frequency, 'none'>, string> = {
  DAILY: 'أيام',
  WEEKLY: 'أسابيع',
  MONTHLY: 'أشهر',
  YEARLY: 'سنوات',
};

export const RecurrencePicker: React.FC<Props> = ({
  value, onChange,
}) => {
  const parsed = parseRRule(value);

  const apply = (next: Partial<Parsed>) => {
    const merged: Parsed = { ...parsed, ...next };
    const rule = buildRRule(merged);
    onChange(rule || null);
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {FREQ_OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => apply({ freq: o.value })}
            className={
              'px-2 py-1 text-[11px] rounded ' +
              (parsed.freq === o.value
                ? 'bg-blue-700 text-white'
                : 'bg-surface-700 text-gray-300 hover:bg-surface-600')
            }
          >
            {o.label}
          </button>
        ))}
      </div>

      {parsed.freq !== 'none' && (
        <div className="flex flex-col gap-2 pt-1">
          <div className="flex items-center gap-2 text-xs
                          text-gray-300">
            <span>كل</span>
            <input
              type="number"
              min={1}
              max={99}
              value={parsed.interval}
              onChange={(e) =>
                apply({
                  interval: Math.max(1, parseInt(e.target.value, 10) || 1),
                })
              }
              className="w-16 px-2 py-1 bg-surface-800 border
                         border-surface-700 rounded text-gray-100"
            />
            <span>{FREQ_UNIT_LABEL[parsed.freq as Exclude<Frequency, 'none'>]}</span>
          </div>

          <div className="flex items-center gap-2 text-xs
                          text-gray-300">
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={parsed.untilEpoch != null}
                onChange={(e) =>
                  apply({
                    untilEpoch: e.target.checked
                      // default: 90 days from now if turning on
                      ? Math.floor(Date.now() / 1000) + 90 * 86400
                      : null,
                  })
                }
              />
              ينتهي بتاريخ
            </label>
            <input
              type="date"
              disabled={parsed.untilEpoch == null}
              value={untilToInputDate(parsed.untilEpoch)}
              onChange={(e) =>
                apply({ untilEpoch: inputDateToUntil(e.target.value) })
              }
              className="px-2 py-1 bg-surface-800 border
                         border-surface-700 rounded text-gray-100
                         disabled:opacity-50"
            />
          </div>

          <div className="text-[10px] text-gray-500 font-mono pt-1"
               title="RRULE المُولَّدة">
            {buildRRule(parsed)}
          </div>
        </div>
      )}
    </div>
  );
};
