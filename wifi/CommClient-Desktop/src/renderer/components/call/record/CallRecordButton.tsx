/**
 * CallRecordButton — toggle for local call recording.
 *
 * Off by default. One click starts the CallRecorder against the
 * current local stream + every known remote stream; another click
 * stops and saves the blob to the user's Downloads folder.
 *
 * The button shows three visual states:
 *   * idle      — outline circle, "تسجيل" tooltip.
 *   * recording — solid red dot pulsing, elapsed timer (mm:ss).
 *   * saving    — small spinner while the file is being flushed.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Square, Loader2 } from 'lucide-react';

/** A solid red dot — lucide-react 0.383's d.ts doesn't reliably
 *  export Circle/Disc/Dot, so inline SVG keeps the icon stable
 *  across lucide minor versions. */
const RedDot: React.FC<{ size?: number }> = ({ size = 12 }) => (
  <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden>
    <circle cx="6" cy="6" r="5" fill="#f87171" />
  </svg>
);
import toast from 'react-hot-toast';
import { useCallStore } from '@/stores/call.store.v2';
import { CallRecorder } from '@/services/call/CallRecorder';

interface Props {
  /** Include video in the recording (default: false → audio-only). */
  includeVideo?: boolean;
}

function fmtMmSs(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export const CallRecordButton: React.FC<Props> = ({
  includeVideo = false,
}) => {
  const localStream = useCallStore((s) => s.localStream);
  const remoteStreams = useCallStore((s) => s.remoteStreams);

  const recorderRef = useRef<CallRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const [saving, setSaving] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);

  // Tick the timer while recording.
  useEffect(() => {
    if (!recording) return;
    const id = window.setInterval(() => {
      const r = recorderRef.current;
      if (r) setElapsedMs(r.elapsedMs);
    }, 500);
    return () => window.clearInterval(id);
  }, [recording]);

  // If the call ends mid-recording, flush the file before unmount.
  useEffect(() => {
    return () => {
      const r = recorderRef.current;
      if (r && r.isRecording) {
        // eslint-disable-next-line no-console
        console.warn('CallRecordButton unmounting with active recorder — '
          + 'finalizing without prompt');
        r.stop().catch(() => {});
      }
    };
  }, []);

  const handleStart = async () => {
    if (recording) return;
    const streams: MediaStream[] = [];
    if (localStream) streams.push(localStream);
    if (remoteStreams) {
      for (const s of Object.values(remoteStreams)) {
        if (s) streams.push(s);
      }
    }
    if (streams.length === 0) {
      toast.error('لا توجد مصادر صوت/فيديو نشطة');
      return;
    }
    const rec = new CallRecorder({ includeVideo });
    try {
      await rec.start(streams);
      recorderRef.current = rec;
      setRecording(true);
      setElapsedMs(0);
      toast(
        'بدأ التسجيل — كل المشاركين يتم تسجيلهم محليّاً',
        { icon: '🔴', duration: 3500 },
      );
    } catch (e: any) {
      toast.error('فشل بدء التسجيل: ' + (e?.message || e));
    }
  };

  const handleStop = async () => {
    const rec = recorderRef.current;
    if (!rec) return;
    setSaving(true);
    try {
      const ext = includeVideo ? 'webm' : 'webm';
      const stamp = new Date().toISOString().replace(/[:.]/g, '-');
      const suggestedName = `helen-call-${stamp}.${ext}`;
      const r = await rec.stopAndSave(suggestedName);
      recorderRef.current = null;
      setRecording(false);
      if (r.error) {
        toast.error('فشل الحفظ: ' + r.error);
        return;
      }
      if (r.path) {
        toast.success(`حُفظ في: ${r.path}`);
      } else {
        toast.success('بدأ التحميل');
      }
    } catch (e: any) {
      toast.error('فشل إيقاف التسجيل: ' + (e?.message || e));
    } finally {
      setSaving(false);
    }
  };

  const onClick = () => {
    if (saving) return;
    if (recording) {
      void handleStop();
    } else {
      void handleStart();
    }
  };

  if (saving) {
    return (
      <button
        disabled
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-full
                   bg-surface-700 text-gray-300 text-sm"
      >
        <Loader2 size={14} className="animate-spin" />
        <span>حفظ…</span>
      </button>
    );
  }

  if (recording) {
    return (
      <button
        onClick={onClick}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-full
                   bg-red-600 hover:bg-red-700 text-white text-sm
                   font-medium"
        title="إيقاف وحفظ التسجيل"
      >
        <Square size={12} fill="white" />
        <span>{fmtMmSs(elapsedMs)}</span>
      </button>
    );
  }

  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-full
                 bg-surface-700 hover:bg-surface-600 text-gray-200
                 text-sm border border-surface-600"
      title="بدء تسجيل المكالمة (محلي فقط)"
    >
      <RedDot size={12} />
      <span>تسجيل</span>
    </button>
  );
};
