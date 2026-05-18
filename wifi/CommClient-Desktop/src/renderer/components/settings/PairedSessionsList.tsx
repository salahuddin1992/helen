/**
 * PairedSessionsList — surfaces the server's authoritative list of live
 * phone-pair sessions for the current user. Polls ``/api/pair/sessions``
 * once a second while visible and lets the user force-disconnect a phone
 * (e.g. after losing the device).
 *
 * Sessions are tagged with a transport badge so the user can see at a
 * glance which phone is plugged in over USB versus streaming over Wi-Fi.
 */
import React, { useEffect, useState } from 'react';
import { Smartphone, X } from 'lucide-react';
import { api } from '@/services/api.client';
import { t } from '@/i18n';
import toast from 'react-hot-toast';

interface Session {
  phone_sid: string;
  user_id: string;
  label: string;
  user_agent: string;
  started_at: number;
  duration_s: number;
  claimed_by: string | null;
  transport: 'usb_tether' | 'wifi';
}

const POLL_MS = 4_000;

function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export const PairedSessionsList: React.FC = () => {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await api.listPairSessions();
        if (!cancelled) setSessions(res.sessions);
      } catch {
        if (!cancelled) setSessions([]);
      }
    };
    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const terminate = async (sid: string) => {
    setBusy(sid);
    try {
      await api.terminatePairSession(sid);
      toast.success(t('pair.session_terminated'));
      setSessions((cur) => (cur || []).filter((s) => s.phone_sid !== sid));
    } catch (err: any) {
      toast.error(err?.message || t('common.error'));
    } finally {
      setBusy(null);
    }
  };

  if (sessions === null) return null;
  if (sessions.length === 0) return null;

  return (
    <div className="mt-3 rounded-lg bg-surface-800/40 border border-surface-800 p-3 space-y-2">
      <div className="text-xs font-medium text-text-300">
        {t('pair.active_sessions')}
      </div>
      {sessions.map((s) => (
        <div
          key={s.phone_sid}
          className="flex items-center gap-2 rounded bg-surface-900 px-2 py-1.5"
        >
          <Smartphone
            size={14}
            className={s.transport === 'usb_tether' ? 'text-emerald-400' : 'text-blue-400'}
          />
          <div className="flex-1 min-w-0">
            <div className="text-xs text-text-100 truncate">
              {s.label}
              <span
                className={
                  'ml-2 text-[9px] font-bold uppercase tracking-wider px-1 py-0.5 rounded ' +
                  (s.transport === 'usb_tether'
                    ? 'bg-emerald-700/40 text-emerald-200'
                    : 'bg-blue-700/40 text-blue-200')
                }
              >
                {s.transport === 'usb_tether' ? 'USB' : 'WI-FI'}
              </span>
            </div>
            <div className="text-[10px] text-text-500 truncate">
              {formatDuration(s.duration_s)} · {s.user_agent.slice(0, 40) || '—'}
            </div>
          </div>
          <button
            onClick={() => terminate(s.phone_sid)}
            disabled={busy === s.phone_sid}
            className="p-1 rounded hover:bg-red-900/40 text-text-500 hover:text-red-400 disabled:opacity-40"
            title={t('pair.terminate')}
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
};
