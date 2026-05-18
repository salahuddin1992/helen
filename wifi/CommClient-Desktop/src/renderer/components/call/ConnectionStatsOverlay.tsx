/**
 * ConnectionStatsOverlay — toggleable in-call diagnostics panel.
 *
 * Polls the QualityController every 2 seconds and renders per-peer
 * RTT, jitter, packet loss, bitrate, and codec. Useful for
 * troubleshooting "why does X look choppy" without leaving the
 * call to open dev tools.
 *
 * Toggle from the call controls (or Ctrl+Shift+S). The polling
 * loop is paused while the overlay is closed so it costs nothing
 * when not in use.
 */

import React, { useEffect, useState } from 'react';
import { Activity, X } from 'lucide-react';
import { useCallStore } from '@/stores/call.store.v2';

interface PeerSnap {
  peerId: string;
  rtt: number;
  jitter: number;
  packetLossRate: number;
  bitrate: number;
  level: string;
}

const fmtMs = (n: number) => (n > 0 ? `${Math.round(n)} ms` : '—');
const fmtPct = (n: number) =>
  n > 0 ? `${(n * 100).toFixed(1)}%` : '0%';
const fmtKbps = (n: number) =>
  n > 0 ? `${Math.round(n)} kbps` : '—';

const ConnectionStatsOverlay: React.FC = () => {
  const [open, setOpen] = useState(false);
  const [snaps, setSnaps] = useState<PeerSnap[]>([]);
  const [overall, setOverall] = useState<{ level: string; score: number } | null>(null);
  const getQualityController = useCallStore((s) => s.getQualityController);
  const status = useCallStore((s) => s.status);

  // Keyboard shortcut (Ctrl+Shift+S).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'S' || e.key === 's')) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Subscribe to QualityController events while the overlay is
  // open. We don't keep a manual interval — the controller already
  // polls every 3 seconds and emits change events; we just mirror.
  useEffect(() => {
    if (!open) return;
    if (status !== 'active' && status !== 'reconnecting') return;
    const ctrl = getQualityController();
    if (!ctrl) return;

    const unsub = ctrl.onChange((event) => {
      setSnaps(event.peerSnapshots.map((s: any) => ({
        peerId: s.peerId,
        rtt: s.rtt,
        jitter: s.jitter,
        packetLossRate: s.packetLossRate,
        bitrate: s.bitrate,
        level: s.level,
      })));
      setOverall({ level: event.overallLevel, score: event.overallScore });
    });
    return () => { try { unsub(); } catch { /* ignore */ } };
  }, [open, status, getQualityController]);

  if (status !== 'active' && status !== 'reconnecting') return null;

  return (
    <>
      {/* Toggle button — rendered as a small floating chip in the
          top-left corner so it doesn't fight with the host menu. */}
      <button
        onClick={() => setOpen((v) => !v)}
        className={`fixed top-4 left-4 z-30 flex items-center gap-2 px-3 py-1.5
                    rounded-full text-xs font-medium shadow-lg transition-colors ${
          open
            ? 'bg-blue-600 text-white'
            : 'bg-black/60 text-white/90 hover:bg-black/80'
        }`}
        title="Connection stats (Ctrl+Shift+S)"
      >
        <Activity size={14} />
        <span>{open ? 'إخفاء' : 'إحصاءات'}</span>
      </button>

      {open && (
        <div className="fixed top-16 left-4 z-30 w-80 max-h-[70vh]
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-surface-700 flex items-center gap-2">
            <Activity size={14} className="text-blue-400" />
            <span className="text-sm font-semibold">إحصاءات الاتصال</span>
            {overall && (
              <span className={`ms-auto text-xs px-2 py-0.5 rounded-full font-bold ${
                overall.level === 'excellent' ? 'bg-green-600/30 text-green-200' :
                overall.level === 'good' ? 'bg-emerald-600/30 text-emerald-200' :
                overall.level === 'fair' ? 'bg-yellow-600/30 text-yellow-200' :
                'bg-red-600/30 text-red-200'
              }`}>
                {Math.round(overall.score)}/100
              </span>
            )}
            <button
              onClick={() => setOpen(false)}
              className="text-text-400 hover:text-text-100"
              title="Close"
            >
              <X size={14} />
            </button>
          </div>

          <div className="overflow-y-auto divide-y divide-surface-800">
            {snaps.length === 0 ? (
              <div className="p-4 text-xs text-text-500 text-center">
                جاري الجمع...
              </div>
            ) : (
              snaps.map((s) => (
                <div key={s.peerId} className="px-3 py-2 text-xs">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      s.level === 'excellent' || s.level === 'good' ? 'bg-green-500' :
                      s.level === 'fair' ? 'bg-yellow-500' : 'bg-red-500'
                    }`} />
                    <span className="font-mono text-text-200 truncate">
                      {s.peerId.slice(0, 12)}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-1 text-text-400 ms-4">
                    <div>RTT: <span className="text-text-200 tabular-nums">{fmtMs(s.rtt)}</span></div>
                    <div>Jitter: <span className="text-text-200 tabular-nums">{fmtMs(s.jitter * 1000)}</span></div>
                    <div>Loss: <span className="text-text-200 tabular-nums">{fmtPct(s.packetLossRate)}</span></div>
                    <div>Rate: <span className="text-text-200 tabular-nums">{fmtKbps(s.bitrate)}</span></div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </>
  );
};

export default ConnectionStatsOverlay;
