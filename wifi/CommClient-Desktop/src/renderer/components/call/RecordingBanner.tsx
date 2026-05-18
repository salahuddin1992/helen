/**
 * RecordingBanner — top-of-screen indicator that the call is being
 * recorded, plus an attention-grabbing consent prompt for the first
 * 8 seconds so participants notice.
 *
 * Hooks into existing server events:
 *   - ``call:recording_started`` / ``call_sfu_recording_started`` → show banner.
 *   - ``call:recording_stopped`` / ``call_sfu_recording_stopped``  → hide banner.
 *
 * The banner is non-dismissible — recording transparency is a hard
 * UX requirement (legal in many jurisdictions). Users always know
 * a recording is running.
 */

import React, { useEffect, useState } from 'react';
import { socketManager } from '@/services/socket.manager';
import { useCallStore } from '@/stores/call.store.v2';

const RecordingBanner: React.FC = () => {
  const callId = useCallStore((s) => s.callId);
  const status = useCallStore((s) => s.status);
  const [recording, setRecording] = useState(false);
  const [recordingId, setRecordingId] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!callId) return;

    const onStart = (data: any) => {
      if (data?.call_id !== callId) return;
      setRecording(true);
      setRecordingId(data.recording_id || null);
      setStartedAt(Date.now());
    };
    const onStop = (data: any) => {
      if (data?.call_id !== callId) return;
      setRecording(false);
      setRecordingId(null);
      setStartedAt(null);
    };

    const offs = [
      socketManager.on('call:recording_started', onStart),
      socketManager.on('call_sfu_recording_started', onStart),
      socketManager.on('call_sfu_recording_event', (data: any) => {
        if (data?.call_id !== callId) return;
        if (data.type === 'recording_started') onStart(data);
        if (data.type === 'recording_stopped') onStop(data);
      }),
      socketManager.on('call:recording_stopped', onStop),
      socketManager.on('call_sfu_recording_stopped', onStop),
    ];
    return () => { for (const f of offs) { try { f(); } catch { /* */ } } };
  }, [callId]);

  // Tick elapsed timer for the inline display.
  useEffect(() => {
    if (!recording || !startedAt) return;
    const t = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => clearInterval(t);
  }, [recording, startedAt]);

  // Reset when call ends.
  useEffect(() => {
    if (status !== 'active' && status !== 'reconnecting') {
      setRecording(false);
      setRecordingId(null);
      setStartedAt(null);
    }
  }, [status]);

  if (!recording) return null;

  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  const elapsedLabel = `${minutes}:${String(seconds).padStart(2, '0')}`;
  const isFreshConsent = elapsed < 8;

  return (
    <div className="fixed top-0 left-0 right-0 z-40 flex justify-center pointer-events-none">
      <div
        className={`mt-2 px-4 py-1.5 rounded-full shadow-2xl flex items-center gap-2
                    text-xs font-semibold transition-all ${
          isFreshConsent
            ? 'bg-red-600 text-white scale-110'
            : 'bg-red-500/90 text-white scale-100'
        }`}
        title={recordingId ? `Recording ID: ${recordingId}` : 'Recording'}
      >
        {/* Pulsating red dot — universal "REC" indicator. */}
        <span className="relative flex w-2.5 h-2.5">
          <span className="absolute inset-0 rounded-full bg-white animate-ping opacity-70" />
          <span className="relative w-2.5 h-2.5 rounded-full bg-white" />
        </span>
        <span>
          {isFreshConsent ? 'يتم تسجيل هذه المكالمة' : 'تسجيل'}
          {' '}
          <span className="font-mono tabular-nums">{elapsedLabel}</span>
        </span>
      </div>
    </div>
  );
};

export default RecordingBanner;
