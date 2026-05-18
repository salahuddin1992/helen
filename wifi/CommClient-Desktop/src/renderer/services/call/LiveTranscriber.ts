/**
 * LiveTranscriber — slice the local mic into 3-second chunks and
 * upload each one to the server's whisper.cpp worker for live
 * captions.
 *
 * Why client-driven chunking
 * --------------------------
 * Whisper is a batch transcriber. Real streaming requires sliding
 * window inference + WebSocket streaming, which whisper.cpp doesn't
 * provide. Chunking the local stream and posting each chunk gets us
 * "captions every ~3s" with about ~1-2s additional latency from the
 * whisper inference itself — good enough for live meetings.
 *
 * Why ONLY the local stream
 * -------------------------
 * Each participant's client is the only place that has uncompressed,
 * pristine audio of *that participant's* voice. We could downstream
 * remote tracks, mix them, and transcribe server-side, but on a
 * 50-person call that's 50× the bandwidth and CPU. Each client doing
 * its own mic is O(1) regardless of call size.
 *
 * How chunks are delivered
 * ------------------------
 * MediaRecorder → blob → base64 → ``v2_call_transcribe_chunk`` socket
 * event. The server runs whisper-cli on the chunk and broadcasts a
 * ``call:caption`` event back to every participant of the call. The
 * sender does NOT echo-mute themselves — caption fanout is round-
 * tripped so all clients render the same string.
 */

import { socketManager } from '../socket.manager';

const CHUNK_INTERVAL_MS = 3000;

export class LiveTranscriber {
  private _stream: MediaStream;
  private _callId: string;
  private _recorder: MediaRecorder | null = null;
  private _chunkSeq = 0;
  private _running = false;
  private _restartTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(stream: MediaStream, callId: string) {
    this._stream = stream;
    this._callId = callId;
  }

  start(): void {
    if (this._running) return;
    this._running = true;
    this._spinUpRecorder();
  }

  stop(): void {
    this._running = false;
    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }
    if (this._recorder && this._recorder.state !== 'inactive') {
      try { this._recorder.stop(); } catch { /* ignore */ }
    }
    this._recorder = null;
  }

  /**
   * Swap the underlying audio stream — used when the user picks a
   * different mic mid-call. Without this the transcriber would
   * keep recording silence from the detached track.
   */
  updateStream(stream: MediaStream): void {
    this._stream = stream;
    if (this._running) {
      try { this._recorder?.stop(); } catch { /* ignore */ }
      this._spinUpRecorder();
    }
  }

  private _spinUpRecorder(): void {
    const audioOnly = new MediaStream(this._stream.getAudioTracks());
    if (audioOnly.getTracks().length === 0) {
      // No audio yet — try again on the next chunk window so we
      // gracefully pick up audio if the mic comes online late.
      this._scheduleRestart();
      return;
    }

    let recorder: MediaRecorder;
    try {
      // Pick the first MIME the browser will give us. webm/opus is
      // universally supported on Chromium; whisper-cli accepts it
      // via libavcodec under the hood.
      const candidates = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/mp4',
        '',
      ];
      const mime = candidates.find(
        (m) => m === '' || (window.MediaRecorder as any)?.isTypeSupported?.(m),
      ) ?? '';
      recorder = new MediaRecorder(audioOnly, mime ? { mimeType: mime } : undefined);
    } catch (err) {
      console.warn('[LiveTranscriber] MediaRecorder unavailable:', err);
      // Don't loop trying — surface the failure once and stay idle.
      this._running = false;
      return;
    }

    recorder.ondataavailable = (e) => {
      if (!e.data || e.data.size === 0) return;
      this._uploadChunk(e.data, recorder.mimeType || 'audio/webm');
    };

    recorder.onstop = () => {
      // Only restart while we're still meant to be running — when
      // the caller invokes stop() this branch is skipped.
      if (this._running) this._scheduleRestart();
    };

    try {
      // start(timeslice) emits a dataavailable event every timeslice
      // ms. We use it to align chunks to wall-clock 3-second windows.
      recorder.start(CHUNK_INTERVAL_MS);
      this._recorder = recorder;
    } catch (err) {
      console.warn('[LiveTranscriber] recorder.start failed:', err);
      this._running = false;
    }
  }

  private _scheduleRestart(): void {
    if (this._restartTimer) clearTimeout(this._restartTimer);
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      if (this._running) this._spinUpRecorder();
    }, 250);
  }

  private async _uploadChunk(blob: Blob, mime: string): Promise<void> {
    try {
      const buf = await blob.arrayBuffer();
      // Base64 encode for socket transport. For 3s @ 24kbps Opus
      // we're talking ~9KB raw → ~12KB base64 — well within socket
      // frame limits.
      const b64 = btoa(
        Array.from(new Uint8Array(buf))
          .map((b) => String.fromCharCode(b))
          .join(''),
      );
      socketManager.emitNoAck('v2_call_transcribe_chunk', {
        call_id: this._callId,
        audio_b64: b64,
        mime,
        chunk_id: this._chunkSeq++,
        started_at: Date.now(),
      });
    } catch (err) {
      console.warn('[LiveTranscriber] chunk upload failed:', err);
    }
  }
}
