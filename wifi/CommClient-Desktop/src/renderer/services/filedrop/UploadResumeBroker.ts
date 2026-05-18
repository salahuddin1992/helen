/**
 * UploadResumeBroker — discovers persisted resumable upload sessions at app
 * startup and surfaces them to the UI so users can reattach a file and
 * continue from the last acknowledged chunk.
 *
 * Why a broker and not just `resumableUploader.resumeAll(...)` directly?
 * The renderer cannot reconstruct a `File` handle from an IndexedDB record
 * alone — a picker is needed. This broker owns the "what sessions are
 * pending" state and exposes a simple subscribe/retry/discard API so the UI
 * (modal, tray badge, settings page) can drive the resume flow.
 */
import { resumableUploader } from './ResumableUploader';
import type { ResumableCallbacks } from './ResumableUploader';

export interface PendingResumableSession {
  session_id: string;
  file_name: string;
  file_size: number;
  mime_type: string;
  chunk_size: number;
  total_chunks: number;
  channel_id?: string;
  uploaded_chunks: number;
  uploaded_bytes: number;
  progress: number;  // 0..1
  updated_at: number;
  age_ms: number;
}

type Listener = (sessions: PendingResumableSession[]) => void;

class UploadResumeBroker {
  private _sessions: PendingResumableSession[] = [];
  private _listeners = new Set<Listener>();
  private _scanning = false;
  private _online = true;
  private _reconnectHookInstalled = false;

  subscribe(listener: Listener): () => void {
    this._listeners.add(listener);
    this._installReconnectHook();
    listener(this._sessions);
    return () => this._listeners.delete(listener);
  }

  /**
   * Attach to browser ``online`` events so we automatically rescan
   * IndexedDB after a network drop — without waiting for the user to
   * trigger a full login. The in-flight upload already waits-for-online
   * internally; this hook is for the post-crash case where IDB has
   * orphaned sessions and the active uploader instance is gone.
   */
  private _installReconnectHook(): void {
    if (this._reconnectHookInstalled) return;
    if (typeof window === 'undefined') return;
    this._reconnectHookInstalled = true;
    this._online = typeof navigator === 'undefined' || navigator.onLine !== false;
    const onOnline = () => {
      if (this._online) return;
      this._online = true;
      // A session that was in progress when the network died is now
      // stale in IDB — re-scanning refreshes the UI so the user knows
      // what's ready to resume.
      this.scan().catch(() => { /* tolerate */ });
    };
    const onOffline = () => {
      this._online = false;
    };
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
  }

  getSessions(): PendingResumableSession[] {
    return this._sessions;
  }

  async scan(): Promise<PendingResumableSession[]> {
    if (this._scanning) return this._sessions;
    this._scanning = true;
    try {
      const stored = await resumableUploader.listResumable();
      const now = Date.now();
      const mapped: PendingResumableSession[] = stored.map(s => {
        const mask = s.received_mask;
        let uploadedChunks = 0;
        for (let i = 0; i < mask.length; i++) {
          let b = mask[i];
          while (b) { uploadedChunks += b & 1; b >>>= 1; }
        }
        const uploadedBytes = Math.min(
          uploadedChunks * s.chunk_size,
          s.file_size,
        );
        return {
          session_id: s.session_id,
          file_name: s.file_name,
          file_size: s.file_size,
          mime_type: s.mime_type,
          chunk_size: s.chunk_size,
          total_chunks: s.total_chunks,
          channel_id: s.channel_id,
          uploaded_chunks: uploadedChunks,
          uploaded_bytes: uploadedBytes,
          progress: s.file_size > 0 ? uploadedBytes / s.file_size : 0,
          updated_at: s.updated_at,
          age_ms: now - s.updated_at,
        };
      });
      this._sessions = mapped.sort((a, b) => b.updated_at - a.updated_at);
      this._emit();
      return this._sessions;
    } finally {
      this._scanning = false;
    }
  }

  async retryWithFile(
    session_id: string,
    file: File,
    cb: ResumableCallbacks = {},
    channelIdOverride?: string,
  ): Promise<string> {
    const entry = this._sessions.find(s => s.session_id === session_id);
    if (!entry) throw new Error(`session not found: ${session_id}`);
    if (file.size !== entry.file_size) {
      throw new Error(
        `file size mismatch: expected ${entry.file_size}, got ${file.size}`,
      );
    }
    try {
      const fileId = await resumableUploader.uploadFile(file, cb, {
        channelId: channelIdOverride ?? entry.channel_id,
        chunkSize: entry.chunk_size,
        resumeSessionId: session_id,
      });
      this._sessions = this._sessions.filter(s => s.session_id !== session_id);
      this._emit();
      return fileId;
    } catch (err) {
      await this.scan();
      throw err;
    }
  }

  async discard(session_id: string): Promise<void> {
    await resumableUploader.abort(session_id);
    this._sessions = this._sessions.filter(s => s.session_id !== session_id);
    this._emit();
  }

  async discardStale(maxAgeMs: number): Promise<number> {
    const stale = this._sessions.filter(s => s.age_ms > maxAgeMs);
    for (const s of stale) {
      try { await this.discard(s.session_id); } catch { /* ignore */ }
    }
    return stale.length;
  }

  private _emit(): void {
    for (const l of this._listeners) {
      try { l(this._sessions); } catch { /* ignore */ }
    }
  }
}

export const uploadResumeBroker = new UploadResumeBroker();
export default uploadResumeBroker;
