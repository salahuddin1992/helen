/**
 * WatchParty — synchronized video playback overlay across all
 * participants of a call.
 *
 * How it works
 * ------------
 * 1. Host enters a video URL (any same-LAN HTTP video) and clicks
 *    "Start". This emits ``v2_call_watchparty_start`` to the server.
 * 2. Server fans out ``call:watchparty_started`` to everyone in the
 *    call. Every client opens the URL in a hidden <video> element
 *    and starts playing.
 * 3. Host's player ticks ``v2_call_watchparty_state`` every ~750ms
 *    AND on play/pause/seek. The server fans out ``call:watchparty_state``.
 * 4. Receivers soft-correct their local playhead toward the host's
 *    position with a 250ms deadband — small drift is ignored to
 *    avoid stuttering.
 *
 * Privacy: 100% LAN-only. The video URL is sent as-is; clients
 * fetch it directly from wherever the host points (file:// is
 * blocked by Chromium so admins typically host it on Helen's own
 * static endpoint).
 */

import React, { useEffect, useRef, useState } from 'react';
import { Play, Pause, X, Square } from 'lucide-react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';
import { useAuthStore } from '@/stores/auth.store';

const SYNC_TICK_MS = 750;
const DRIFT_DEADBAND_SEC = 0.25;

const WatchParty: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const hostId = useCallStore((s) => s.hostId);
  const coHostIds = useCallStore((s) => s.coHostIds);
  const me = useAuthStore((s) => s.user);
  const isHost = !!me && hostId === me.id;
  const isCoHost = !!me && coHostIds.includes(me.id);
  const canDrive = isHost || isCoHost;

  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState('');
  const [active, setActive] = useState(false);
  const [draftUrl, setDraftUrl] = useState('');

  const videoRef = useRef<HTMLVideoElement>(null);
  const syncTickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Subscribe to lifecycle + state events.
  useEffect(() => {
    if (!callId) return;
    const offs: Array<() => void> = [];

    offs.push(socketManager.on('call:watchparty_started', (data: any) => {
      if (data?.call_id !== callId) return;
      setUrl(data.source_url || '');
      setActive(true);
      setOpen(true);
    }));
    offs.push(socketManager.on('call:watchparty_stopped', (data: any) => {
      if (data?.call_id !== callId) return;
      setActive(false);
      setOpen(false);
      setUrl('');
    }));
    offs.push(socketManager.on('call:watchparty_state', (data: any) => {
      if (data?.call_id !== callId) return;
      const v = videoRef.current;
      if (!v) return;
      // Receivers soft-correct toward host's playhead.
      const targetSec = (data.position_ms || 0) / 1000;
      if (Math.abs(v.currentTime - targetSec) > DRIFT_DEADBAND_SEC) {
        try { v.currentTime = targetSec; } catch { /* ignore */ }
      }
      if (data.playing && v.paused) {
        v.play().catch(() => { /* autoplay may be blocked */ });
      } else if (!data.playing && !v.paused) {
        v.pause();
      }
    }));

    return () => { for (const f of offs) { try { f(); } catch { /* */ } } };
  }, [callId]);

  // Cleanup on call end.
  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setActive(false);
      setOpen(false);
      setUrl('');
    }
  }, [status]);

  // Drive the sync tick — only if host/co-host AND active.
  useEffect(() => {
    if (!active || !canDrive || !callId) return;
    const tick = () => {
      const v = videoRef.current;
      if (!v) return;
      socketManager.emitNoAck('v2_call_watchparty_state', {
        call_id: callId,
        playing: !v.paused,
        position_ms: Math.round(v.currentTime * 1000),
      });
    };
    syncTickRef.current = setInterval(tick, SYNC_TICK_MS);
    return () => {
      if (syncTickRef.current) {
        clearInterval(syncTickRef.current);
        syncTickRef.current = null;
      }
    };
  }, [active, canDrive, callId]);

  // Fire one-shot state on play/pause/seek.
  const onLocalPlayPause = (playing: boolean) => {
    if (!canDrive || !callId) return;
    const v = videoRef.current;
    if (!v) return;
    socketManager.emitNoAck('v2_call_watchparty_state', {
      call_id: callId,
      playing,
      position_ms: Math.round(v.currentTime * 1000),
    });
  };

  const start = () => {
    if (!callId || !draftUrl.trim()) return;
    socketManager.emitNoAck('v2_call_watchparty_start', {
      call_id: callId,
      source_url: draftUrl.trim(),
    });
  };
  const stop = () => {
    if (!callId) return;
    socketManager.emitNoAck('v2_call_watchparty_stop', {
      call_id: callId,
    });
  };

  if (!callId) return null;
  if (status !== 'active' && status !== 'reconnecting') return null;

  return (
    <>
      {/* Host launcher chip — sits next to the lobby chip when no
          watch party is active. While active, a small floating
          window holds the synced player. */}
      {canDrive && !active && (
        <button
          onClick={() => setOpen((v) => !v)}
          className="fixed bottom-72 right-4 z-30 px-3 py-1.5 rounded-full
                     bg-black/60 hover:bg-black/80 text-white/90
                     text-xs font-medium shadow-lg flex items-center gap-1"
          title="مشاهدة جماعية"
        >
          <Play size={12} />
          <span>مشاهدة جماعية</span>
        </button>
      )}

      {open && (
        <div className="fixed bottom-32 right-4 z-30 w-96
                        bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-surface-700 flex items-center gap-2">
            <Play size={14} className="text-blue-400" />
            <span className="flex-1 text-sm font-semibold">
              مشاهدة جماعية
            </span>
            <button
              onClick={() => setOpen(false)}
              className="text-text-400 hover:text-text-100"
            >
              <X size={14} />
            </button>
          </div>

          {!active ? (
            <div className="p-3 space-y-2">
              <label className="block text-xs text-text-300">
                رابط الفيديو (HTTP/MP4 على الـ LAN)
              </label>
              <input
                value={draftUrl}
                onChange={(e) => setDraftUrl(e.target.value)}
                placeholder="http://server.local/video.mp4"
                className="w-full bg-surface-800 border border-surface-700
                           rounded px-2 py-1.5 text-sm text-text-100 outline-none"
              />
              <button
                onClick={start}
                disabled={!draftUrl.trim() || !canDrive}
                className="w-full px-3 py-1.5 rounded bg-blue-600
                           hover:bg-blue-500 text-white text-sm disabled:opacity-40"
              >
                ابدأ المشاهدة المتزامنة
              </button>
              {!canDrive && (
                <div className="text-[10px] text-text-500">
                  المضيف فقط يقدر يبدأ مشاهدة جماعية
                </div>
              )}
            </div>
          ) : (
            <>
              <video
                ref={videoRef}
                src={url}
                controls={canDrive}
                autoPlay
                playsInline
                onPlay={() => onLocalPlayPause(true)}
                onPause={() => onLocalPlayPause(false)}
                onSeeked={() => onLocalPlayPause(!videoRef.current?.paused)}
                className="w-full h-56 bg-black"
              />
              <div className="px-3 py-2 flex items-center gap-2">
                <span className="text-[10px] text-text-500 flex-1 truncate">
                  {canDrive ? 'تتحكم بالتشغيل' : 'يتم التشغيل من المضيف'}
                </span>
                {canDrive && (
                  <button
                    onClick={stop}
                    className="text-xs px-2 py-0.5 rounded bg-red-600
                               hover:bg-red-500 text-white flex items-center gap-1"
                  >
                    <Square size={10} /> إنهاء
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
};

export default WatchParty;
