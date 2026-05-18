/**
 * Notification sound library — synthesized, asset-free.
 *
 * Each sound is a tiny WebAudio composition built on the fly. We
 * deliberately avoid bundling MP3/OGG assets to keep the renderer
 * bundle small and to dodge platform codec quirks.
 *
 * The library exposes three tiers:
 *   * ``catalog``  — the set of known sound IDs + display labels.
 *   * ``play(id)`` — one-shot playback; quietly no-ops on
 *     unsupported browsers / with a mute setting on.
 *   * ``preview(id)`` — alias for ``play``, used by the settings UI
 *     when the user clicks a sound to hear it before committing.
 *
 * Volume scaling honors the ``notification-sounds.store`` master
 * volume (0..1), so a single slider in settings affects every
 * sound. Per-event-type sound choice + master mute live in the same
 * store.
 */

type ToneSpec = {
  freq: number;
  durMs: number;
  type?: OscillatorType;
  /** Delay before this tone fires, relative to the previous one. */
  delayMs?: number;
  /** Final gain at the peak (0..1). */
  gain?: number;
};

const NONE_SPEC: ToneSpec[] = [];

const SOUND_SPECS: Record<string, ToneSpec[]> = {
  none: NONE_SPEC,
  // Soft two-note chime — message default.
  chime: [
    { freq: 1100, durMs: 90, type: 'sine', gain: 0.5 },
    { freq: 1480, durMs: 140, type: 'sine', delayMs: 80, gain: 0.6 },
  ],
  // Higher single bling — mention.
  bling: [
    { freq: 1760, durMs: 110, type: 'triangle', gain: 0.6 },
  ],
  // Soft thump — DM message.
  pop: [
    { freq: 380, durMs: 70, type: 'sine', gain: 0.7 },
    { freq: 720, durMs: 90, type: 'sine', delayMs: 30, gain: 0.45 },
  ],
  // Long sweep up — incoming call.
  ring: [
    { freq: 660, durMs: 220, type: 'sine', gain: 0.5 },
    { freq: 880, durMs: 220, type: 'sine', delayMs: 220, gain: 0.5 },
  ],
  // Two short blips — system notification.
  blip: [
    { freq: 1320, durMs: 60, type: 'square', gain: 0.4 },
    { freq: 1320, durMs: 60, type: 'square', delayMs: 100, gain: 0.4 },
  ],
};

export type SoundId = keyof typeof SOUND_SPECS;

export const catalog: Array<{ id: SoundId; label: string }> = [
  { id: 'none', label: 'بدون صوت' },
  { id: 'chime', label: 'جرس' },
  { id: 'bling', label: 'تنبيه عالي' },
  { id: 'pop', label: 'دق' },
  { id: 'ring', label: 'رنين' },
  { id: 'blip', label: 'بلب-بلب' },
];

let _ctx: AudioContext | null = null;

function ctx(): AudioContext | null {
  if (typeof window === 'undefined') return null;
  if (_ctx) return _ctx;
  try {
    _ctx = new (window.AudioContext
      || (window as any).webkitAudioContext)();
    return _ctx;
  } catch {
    return null;
  }
}

/** Play a sound by id at the given master volume. Returns the
 *  approximate total duration in ms so callers can throttle bursts.
 *  Master volume should be in [0, 1]; values outside are clamped. */
export function play(id: SoundId, masterVolume = 0.6): number {
  if (id === 'none') return 0;
  const spec = SOUND_SPECS[id];
  if (!spec || spec.length === 0) return 0;

  const c = ctx();
  if (!c) return 0;
  const v = Math.max(0, Math.min(1, masterVolume));
  if (v === 0) return 0;

  let cursorMs = 0;
  let totalMs = 0;
  const startTime = c.currentTime;
  for (const tone of spec) {
    cursorMs += tone.delayMs ?? 0;
    const startSec = startTime + cursorMs / 1000;
    const endSec = startSec + tone.durMs / 1000;

    const osc = c.createOscillator();
    osc.type = tone.type ?? 'sine';
    osc.frequency.value = tone.freq;

    const gain = c.createGain();
    const peak = (tone.gain ?? 0.5) * v;
    // Quick attack + soft release to avoid click artifacts.
    gain.gain.setValueAtTime(0, startSec);
    gain.gain.linearRampToValueAtTime(peak, startSec + 0.005);
    gain.gain.exponentialRampToValueAtTime(0.0001, endSec);

    osc.connect(gain);
    gain.connect(c.destination);
    osc.start(startSec);
    osc.stop(endSec + 0.01);

    if (cursorMs + tone.durMs > totalMs) {
      totalMs = cursorMs + tone.durMs;
    }
  }
  return totalMs;
}

export const preview = play;
