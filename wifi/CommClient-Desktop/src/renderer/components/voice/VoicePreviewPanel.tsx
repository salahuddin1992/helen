/**
 * VoicePreviewPanel — listen-before-send for voice messages.
 *
 * After the user releases the record button, instead of firing the
 * upload immediately, the parent ``VoiceRecorder`` mounts this
 * preview panel with the captured Blob. The user can:
 *
 *   * ▶ Play — listen to what was recorded.
 *   * ✕ Cancel — discard the blob, return to the input.
 *   * ✓ Send — fire the upload + socket emit (same path as before).
 *
 * Waveform is sampled from the Blob's decoded PCM (one bar every
 * ~10ms) so the user gets a real visualization instead of a
 * canned shape. Decode happens in a single async pass on mount —
 * the Blob is small (a few seconds of speech) so blocking the
 * main thread for ≈20ms is acceptable.
 */

import React, { useEffect, useRef, useState } from 'react';
import { Play, Pause, X, Check, Loader2 } from 'lucide-react';

interface Props {
  blob: Blob;
  /** Total recording duration in seconds (the recorder already
   *  knows; we use it for the timer label). */
  durationSec: number;
  onCancel: () => void;
  onSend: () => void;
  isSending?: boolean;
}

const BAR_COUNT = 48;

function fmtMmSs(s: number): string {
  if (!Number.isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

async function decodeWaveform(
  blob: Blob, bars: number,
): Promise<number[]> {
  // Some browsers (and Electron's Chromium) need a typed buffer.
  const arrayBuf = await blob.arrayBuffer();
  // Use AudioContext outside the React tree — we just decode once.
  const Ctx = (window as any).AudioContext
    || (window as any).webkitAudioContext;
  if (!Ctx) return new Array(bars).fill(0.4);
  const ctx: AudioContext = new Ctx();
  try {
    const audio = await ctx.decodeAudioData(arrayBuf.slice(0));
    const data = audio.getChannelData(0);
    const samplesPerBar = Math.max(1, Math.floor(data.length / bars));
    const peaks: number[] = [];
    for (let i = 0; i < bars; i++) {
      let max = 0;
      const start = i * samplesPerBar;
      const end = Math.min(start + samplesPerBar, data.length);
      for (let j = start; j < end; j++) {
        const v = Math.abs(data[j]);
        if (v > max) max = v;
      }
      peaks.push(max);
    }
    // Normalize.
    const peak = Math.max(...peaks, 0.0001);
    return peaks.map((p) => p / peak);
  } catch {
    return new Array(bars).fill(0.5);
  } finally {
    try { await ctx.close(); } catch { /* ignore */ }
  }
}

export const VoicePreviewPanel: React.FC<Props> = ({
  blob, durationSec, onCancel, onSend, isSending = false,
}) => {
  const [isPlaying, setIsPlaying] = useState(false);
  const [progress, setProgress] = useState(0);  // 0..1
  const [waveform, setWaveform] = useState<number[]>(
    new Array(BAR_COUNT).fill(0.4),
  );
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const blobUrlRef = useRef<string>('');

  // Build the blob URL once + decode the waveform.
  useEffect(() => {
    blobUrlRef.current = URL.createObjectURL(blob);
    let cancelled = false;
    decodeWaveform(blob, BAR_COUNT).then((bars) => {
      if (!cancelled) setWaveform(bars);
    });
    return () => {
      cancelled = true;
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
    };
  }, [blob]);

  // Esc cancels.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const togglePlay = () => {
    const a = audioRef.current;
    if (!a) return;
    if (isPlaying) {
      a.pause();
    } else {
      a.currentTime = 0;
      a.play().catch(() => { /* user gesture needed in some browsers */ });
    }
  };

  return (
    <div className="flex items-center gap-2 px-3 py-2 rounded-lg
                    bg-slate-800 border border-slate-700">
      <audio
        ref={audioRef}
        src={blobUrlRef.current || undefined}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => {
          setIsPlaying(false);
          setProgress(0);
        }}
        onTimeUpdate={(e) => {
          const a = e.currentTarget;
          if (a.duration > 0) {
            setProgress(a.currentTime / a.duration);
          }
        }}
        preload="metadata"
      />

      {/* Cancel */}
      <button
        type="button"
        onClick={onCancel}
        disabled={isSending}
        className="p-1.5 rounded-full bg-red-700/30 text-red-200
                   hover:bg-red-700/50 disabled:opacity-50"
        title="إلغاء"
        aria-label="Cancel"
      >
        <X size={14} />
      </button>

      {/* Play / pause */}
      <button
        type="button"
        onClick={togglePlay}
        disabled={isSending}
        className="p-1.5 rounded-full bg-blue-600 hover:bg-blue-700
                   text-white disabled:opacity-50"
        title={isPlaying ? 'إيقاف مؤقت' : 'تشغيل'}
      >
        {isPlaying ? <Pause size={14} /> : <Play size={14} />}
      </button>

      {/* Waveform */}
      <div className="flex-1 flex items-center gap-px h-7">
        {waveform.map((v, i) => {
          const fillFraction = i / Math.max(1, BAR_COUNT - 1);
          const played = fillFraction <= progress;
          return (
            <span
              key={i}
              style={{
                height: `${Math.max(2, Math.round(v * 100))}%`,
                width: '3px',
              }}
              className={
                'rounded-sm ' +
                (played ? 'bg-blue-400' : 'bg-slate-500')
              }
            />
          );
        })}
      </div>

      {/* Duration */}
      <span className="text-[11px] text-gray-300 font-mono w-10
                       text-end">
        {fmtMmSs(durationSec)}
      </span>

      {/* Send */}
      <button
        type="button"
        onClick={onSend}
        disabled={isSending}
        className="p-1.5 rounded-full bg-emerald-600 hover:bg-emerald-700
                   text-white disabled:opacity-50"
        title="إرسال"
        aria-label="Send"
      >
        {isSending
          ? <Loader2 size={14} className="animate-spin" />
          : <Check size={14} />}
      </button>
    </div>
  );
};
