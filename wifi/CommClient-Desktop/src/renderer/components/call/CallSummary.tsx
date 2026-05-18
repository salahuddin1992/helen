/**
 * CallSummary — post-call modal showing duration, participants, and
 * transcript text. Generated client-side from data the call store
 * already accumulated; no extra server round-trip is needed.
 *
 * The modal opens automatically when ``status`` transitions to
 * ``ended`` AND ``callDuration > 0`` (so we don't pop a summary
 * for an unanswered ring). Closes on user dismiss or after 30s
 * inactivity.
 *
 * Includes:
 *   - Duration + participant count
 *   - Compact list of who attended
 *   - Full caption transcript (if captions were enabled)
 *   - "Copy transcript" + "Save as text file" actions
 */

import React, { useEffect, useMemo, useState } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { Clock, Copy, Download, FileText, X, Users } from 'lucide-react';

const CallSummary: React.FC = () => {
  // Snapshot the moment the call ends so React doesn't re-render
  // the summary as the active call store fields are reset.
  const status = useCallStore((s) => s.status);
  const callDuration = useCallStore((s) => s.callDuration);
  const participants = useCallStore((s) => s.participants);
  const captions = useCallStore((s) => s.captions);
  const endReason = useCallStore((s) => s.endReason);

  const [snapshot, setSnapshot] = useState<{
    durationSec: number;
    participants: Array<{ id: string; name: string }>;
    captions: Array<{ userId: string; text: string; ts: number }>;
    endReason: string;
  } | null>(null);

  // Capture state at the moment the call ends.
  useEffect(() => {
    if (status === 'ended' && callDuration > 0 && !snapshot) {
      setSnapshot({
        durationSec: callDuration,
        participants: Object.values(participants).map((p: any) => ({
          id: p.peerId,
          name: p.displayName || p.peerId,
        })),
        captions: captions.map((c) => ({
          userId: c.userId,
          text: c.text,
          ts: c.ts,
        })),
        endReason: endReason || 'انتهت المكالمة',
      });
    }
    if (status === 'idle' || status === 'ringing' || status === 'connecting' ||
        status === 'active' || status === 'reconnecting') {
      // A new lifecycle has begun — drop the old snapshot.
      if (snapshot) setSnapshot(null);
    }
  }, [status, callDuration, participants, captions, endReason, snapshot]);

  // Auto-dismiss after 30s of inactivity.
  useEffect(() => {
    if (!snapshot) return;
    const t = setTimeout(() => setSnapshot(null), 30_000);
    return () => clearTimeout(t);
  }, [snapshot]);

  const transcript = useMemo(() => {
    if (!snapshot) return '';
    if (snapshot.captions.length === 0) return '';
    const nameOf = (uid: string) => {
      const p = snapshot.participants.find((x) => x.id === uid);
      return p?.name || uid.slice(0, 8);
    };
    return snapshot.captions
      .map((c) => `[${new Date(c.ts).toLocaleTimeString()}] ${nameOf(c.userId)}: ${c.text}`)
      .join('\n');
  }, [snapshot]);

  if (!snapshot) return null;

  const minutes = Math.floor(snapshot.durationSec / 60);
  const seconds = snapshot.durationSec % 60;
  const durationLabel = `${minutes}:${String(seconds).padStart(2, '0')}`;

  const copyTranscript = () => {
    navigator.clipboard?.writeText(transcript).catch(() => { /* ignore */ });
  };

  const downloadTranscript = () => {
    const text = [
      `ملخص المكالمة`,
      `المدة: ${durationLabel}`,
      `المشاركون: ${snapshot.participants.map((p) => p.name).join(', ')}`,
      `السبب: ${snapshot.endReason}`,
      ``,
      `النص الكامل:`,
      transcript || '(لم تكن التسميات الحية مفعَّلة)',
    ].join('\n');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `helen-call-${Date.now()}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur
                    flex items-center justify-center p-4">
      <div className="bg-surface-900 rounded-xl border border-surface-700
                      shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col overflow-hidden">
        <div className="px-5 py-3 border-b border-surface-700 flex items-center justify-between">
          <h2 className="text-base font-semibold text-text-100">
            ملخص المكالمة
          </h2>
          <button
            onClick={() => setSnapshot(null)}
            className="text-text-400 hover:text-text-100"
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-4 grid grid-cols-2 gap-3 border-b border-surface-700">
          <div className="bg-surface-800 rounded-lg p-3">
            <div className="flex items-center gap-1 text-xs text-text-400 mb-1">
              <Clock size={12} /> المدة
            </div>
            <div className="text-2xl font-bold text-text-100 tabular-nums">
              {durationLabel}
            </div>
          </div>
          <div className="bg-surface-800 rounded-lg p-3">
            <div className="flex items-center gap-1 text-xs text-text-400 mb-1">
              <Users size={12} /> المشاركون
            </div>
            <div className="text-2xl font-bold text-text-100 tabular-nums">
              {snapshot.participants.length}
            </div>
          </div>
        </div>

        <div className="px-5 py-2 border-b border-surface-700 text-xs text-text-400">
          السبب: <span className="text-text-200">{snapshot.endReason}</span>
        </div>

        <div className="px-5 py-2 border-b border-surface-700 flex flex-wrap gap-1">
          {snapshot.participants.map((p) => (
            <span
              key={p.id}
              className="px-2 py-0.5 rounded-full bg-surface-800 text-xs text-text-200"
            >
              {p.name}
            </span>
          ))}
          {snapshot.participants.length === 0 && (
            <span className="text-xs text-text-500">لا توجد بيانات مشاركين</span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          <div className="flex items-center gap-1 text-xs text-text-400 mb-2">
            <FileText size={12} />
            <span>النص الكامل</span>
          </div>
          {transcript ? (
            <pre className="text-xs leading-relaxed text-text-200 whitespace-pre-wrap font-mono">
              {transcript}
            </pre>
          ) : (
            <div className="text-xs text-text-500 italic">
              لم تكن التسميات الحية مفعَّلة في هذه المكالمة
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-surface-700 flex justify-end gap-2">
          {transcript && (
            <button
              onClick={copyTranscript}
              className="text-xs px-3 py-1.5 rounded bg-surface-800
                         hover:bg-surface-700 text-text-100 flex items-center gap-1"
            >
              <Copy size={12} /> نسخ
            </button>
          )}
          <button
            onClick={downloadTranscript}
            className="text-xs px-3 py-1.5 rounded bg-blue-600
                       hover:bg-blue-500 text-white flex items-center gap-1"
          >
            <Download size={12} /> حفظ كملف
          </button>
          <button
            onClick={() => setSnapshot(null)}
            className="text-xs px-3 py-1.5 rounded bg-surface-700
                       hover:bg-surface-600 text-text-300"
          >
            إغلاق
          </button>
        </div>
      </div>
    </div>
  );
};

export default CallSummary;
