/**
 * CallRecorder — local-only recording of an active call.
 *
 * What it captures
 * ----------------
 * Mixes the local microphone stream + every remote participant's
 * audio + (optionally) the local camera/screen video into a single
 * MediaStream, then runs MediaRecorder over it. The encoded chunks
 * are buffered until ``stop()`` is called and the result is handed
 * to the Electron downloads IPC for save-to-disk.
 *
 * Privacy posture
 * ---------------
 * Recording is **off by default**. Starting it is an explicit user
 * action — there is no auto-start. We optionally emit a system
 * notification to every other participant ("X started recording")
 * via Socket.IO so peers know to consent; that signaling is the
 * caller's responsibility (this class only handles the local
 * MediaRecorder side). The output file lives only on the recorder's
 * machine — nothing is uploaded.
 *
 * Codec
 * -----
 * MediaRecorder picks ``video/webm;codecs=vp8,opus`` when video is
 * included, ``audio/webm;codecs=opus`` for audio-only. Both play in
 * VLC, browsers, and Telegram. We deliberately avoid H.264 because
 * Chromium's MediaRecorder support is patchy across builds.
 *
 * Wiring
 * ------
 *   const rec = new CallRecorder({ includeVideo: true });
 *   rec.start([localStream, ...remoteStreamsArray]);
 *   ...
 *   const file = await rec.stopAndSave('call-2026-05-06.webm');
 */

import { downloadFileToDisk } from '@/services/chat-downloads';
// The full window.electronAPI typing is set up in src/renderer/types/index.ts.
// We access ``downloads`` defensively at runtime to handle older builds.

export interface CallRecorderOptions {
  /** Include video tracks in the recording. Default: false (audio-
   *  only). Setting true with no video tracks present silently
   *  falls back to audio. */
  includeVideo?: boolean;
  /** Codec hint passed to MediaRecorder.mimeType. The default
   *  values cover Chromium's reliable subset. */
  preferredMimeType?: string;
  /** Chunk emit interval in ms; lower = smoother memory profile,
   *  but more chunks to concat at the end. */
  timeslice_ms?: number;
}

export class CallRecorder {
  private opts: Required<CallRecorderOptions>;
  private chunks: Blob[] = [];
  private recorder: MediaRecorder | null = null;
  private audioCtx: AudioContext | null = null;
  private mixedStream: MediaStream | null = null;
  private startedAt: number = 0;
  private state: 'idle' | 'recording' | 'stopped' = 'idle';

  constructor(opts: CallRecorderOptions = {}) {
    this.opts = {
      includeVideo: false,
      preferredMimeType: '',
      timeslice_ms: 1000,
      ...opts,
    };
  }

  get isRecording(): boolean {
    return this.state === 'recording';
  }

  get elapsedMs(): number {
    if (this.state !== 'recording') return 0;
    return Date.now() - this.startedAt;
  }

  /** Start recording. Pass every MediaStream that should land in
   *  the output mix — local mic + each remote peer's stream is
   *  the typical set. */
  async start(streams: Array<MediaStream | null | undefined>): Promise<void> {
    if (this.state === 'recording') return;

    const real = streams.filter(
      (s): s is MediaStream => !!s && s.getTracks().length > 0,
    );
    if (real.length === 0) {
      throw new Error('CallRecorder.start: no streams to record');
    }

    // Audio mix via WebAudio. We can't just merge MediaStreamTracks
    // directly — multiple audio tracks in one MediaStream play
    // serially in MediaRecorder, not concurrently. WebAudio mixes
    // them into one output node we can hand to MediaRecorder.
    const ctx = new AudioContext();
    const dest = ctx.createMediaStreamDestination();
    for (const s of real) {
      const audioTracks = s.getAudioTracks();
      if (audioTracks.length === 0) continue;
      try {
        const src = ctx.createMediaStreamSource(
          new MediaStream(audioTracks),
        );
        src.connect(dest);
      } catch (e) {
        // Some peer streams refuse re-wrapping (Safari quirk).
        // Drop them silently rather than failing the whole record.
        // eslint-disable-next-line no-console
        console.warn('CallRecorder: skipped audio source', e);
      }
    }

    const mixed = new MediaStream(dest.stream.getAudioTracks());

    if (this.opts.includeVideo) {
      // Pick the first video track we find — combining multiple
      // remote video tracks into one output would require a canvas
      // compositor, which is a bigger feature. Single-stream
      // recording (active speaker, screen share, or local cam) is
      // the common need.
      for (const s of real) {
        const vt = s.getVideoTracks()[0];
        if (vt) {
          mixed.addTrack(vt);
          break;
        }
      }
    }

    const mimeType = this.pickMimeType(this.opts.includeVideo);
    const recorder = new MediaRecorder(mixed, {
      mimeType: mimeType || undefined,
    });
    this.chunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) this.chunks.push(e.data);
    };
    recorder.start(this.opts.timeslice_ms);

    this.recorder = recorder;
    this.audioCtx = ctx;
    this.mixedStream = mixed;
    this.startedAt = Date.now();
    this.state = 'recording';
  }

  /** Stop the recorder and flush all buffered chunks into a single
   *  Blob. The blob's MIME matches whatever MediaRecorder ended up
   *  using (so the output filename should match). */
  async stop(): Promise<Blob> {
    if (this.state !== 'recording' || !this.recorder) {
      throw new Error('CallRecorder.stop: not recording');
    }
    const recorder = this.recorder;

    const stopped = new Promise<void>((resolve) => {
      recorder.onstop = () => resolve();
    });
    recorder.stop();
    await stopped;

    // Tear down WebAudio + the mixed stream tracks so we don't
    // leak an open AudioContext into the next call.
    try {
      this.audioCtx?.close();
    } catch { /* ignore */ }
    this.audioCtx = null;
    this.mixedStream?.getTracks().forEach((t) => t.stop());
    this.mixedStream = null;
    this.recorder = null;
    this.state = 'stopped';

    const mime = recorder.mimeType || 'audio/webm';
    return new Blob(this.chunks, { type: mime });
  }

  /** Stop recording and write the result to the user's Downloads
   *  folder via the Electron IPC. Returns the absolute path on
   *  success, or null when running in browser mode (the browser
   *  owns the file). */
  async stopAndSave(
    suggestedFilename: string,
  ): Promise<{ path: string | null; bytes: number; error?: string }> {
    const blob = await this.stop();
    const buf = await blob.arrayBuffer();
    // Direct save through saveBuffer (we already have the bytes in
    // memory — streaming would be wasted work). We use ``downloadFileToDisk``
    // shape for browser-mode fallback parity.
    const dl = (window as any).electronAPI?.downloads;
    if (dl?.saveBuffer) {
      const r = await dl.saveBuffer(suggestedFilename, buf);
      if (!r.ok) {
        return { path: null, bytes: buf.byteLength, error: r.error };
      }
      return { path: r.path, bytes: buf.byteLength };
    }
    // Browser-mode fallback: hand the blob to a download anchor.
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = suggestedFilename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    return { path: null, bytes: buf.byteLength };
  }

  // Suppress the unused warning — kept exported for symmetry with
  // file downloads even though we save the buffer directly above.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  private __keepImportAlive = downloadFileToDisk;

  private pickMimeType(includeVideo: boolean): string {
    if (this.opts.preferredMimeType
        && MediaRecorder.isTypeSupported(this.opts.preferredMimeType)) {
      return this.opts.preferredMimeType;
    }
    const candidates = includeVideo
      ? [
          'video/webm;codecs=vp8,opus',
          'video/webm;codecs=vp9,opus',
          'video/webm',
        ]
      : [
          'audio/webm;codecs=opus',
          'audio/webm',
          'audio/ogg;codecs=opus',
        ];
    for (const c of candidates) {
      if (MediaRecorder.isTypeSupported(c)) return c;
    }
    return ''; // let the browser pick whatever it has
  }
}
