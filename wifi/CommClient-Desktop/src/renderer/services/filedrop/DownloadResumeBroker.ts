/**
 * DownloadResumeBroker — mirror of UploadResumeBroker for the download
 * direction. Discovers IndexedDB-persisted partial downloads at app startup
 * and exposes a subscribe/resume/discard surface the UI can drive.
 *
 * Unlike the upload side, resuming a download does NOT require the user to
 * pick anything — the URL and the partial bytes are self-contained. This
 * broker can therefore auto-resume downloads transparently on reconnect if
 * the caller opts in via ``autoResume: true`` in subscribe().
 */
import {
  resumableDownloader,
  type DownloadCallbacks,
  type DownloadMeta,
} from './ResumableDownloader';

export interface PendingDownload {
  download_id: string;
  url: string;
  file_name: string;
  mime_type: string;
  total_size: number | null;
  received: number;
  progress: number;     // 0..1 (0 if total_size unknown)
  updated_at: number;
  age_ms: number;
}

type Listener = (downloads: PendingDownload[]) => void;

function toPending(m: DownloadMeta, now: number): PendingDownload {
  const progress = m.total_size && m.total_size > 0
    ? Math.min(1, m.received / m.total_size)
    : 0;
  return {
    download_id: m.download_id,
    url: m.url,
    file_name: m.file_name,
    mime_type: m.mime_type,
    total_size: m.total_size,
    received: m.received,
    progress,
    updated_at: m.updated_at,
    age_ms: now - m.updated_at,
  };
}

class DownloadResumeBroker {
  private _downloads: PendingDownload[] = [];
  private _listeners = new Set<Listener>();
  private _scanning = false;
  private _online = true;
  private _reconnectHookInstalled = false;
  private _inFlight = new Set<string>();

  subscribe(listener: Listener): () => void {
    this._listeners.add(listener);
    this._installReconnectHook();
    listener(this._downloads);
    return () => this._listeners.delete(listener);
  }

  private _installReconnectHook(): void {
    if (this._reconnectHookInstalled) return;
    if (typeof window === 'undefined') return;
    this._reconnectHookInstalled = true;
    this._online = typeof navigator === 'undefined' || navigator.onLine !== false;
    const onOnline = () => {
      if (this._online) return;
      this._online = true;
      this.scan().catch(() => { /* tolerate */ });
    };
    const onOffline = () => { this._online = false; };
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
  }

  getDownloads(): PendingDownload[] {
    return this._downloads;
  }

  async scan(): Promise<PendingDownload[]> {
    if (this._scanning) return this._downloads;
    this._scanning = true;
    try {
      const stored = await resumableDownloader.listResumable();
      const now = Date.now();
      const mapped = stored.map(m => toPending(m, now));
      this._downloads = mapped.sort((a, b) => b.updated_at - a.updated_at);
      this._emit();
      return this._downloads;
    } finally {
      this._scanning = false;
    }
  }

  /**
   * Resume a persisted download. ``concurrentGuard`` prevents double-start
   * if the UI fires resume twice for the same entry (the downloader is
   * idempotent per downloadId but we avoid a redundant fetch).
   */
  async resume(
    download_id: string,
    cb: DownloadCallbacks = {},
    authHeader?: string,
  ): Promise<Blob> {
    if (this._inFlight.has(download_id)) {
      throw new Error(`download already in progress: ${download_id}`);
    }
    const entry = this._downloads.find(d => d.download_id === download_id);
    if (!entry) throw new Error(`download not found: ${download_id}`);

    this._inFlight.add(download_id);
    try {
      const blob = await resumableDownloader.resume(
        {
          download_id: entry.download_id,
          url: entry.url,
          file_name: entry.file_name,
          mime_type: entry.mime_type,
        },
        cb,
        { authHeader },
      );
      this._downloads = this._downloads.filter(d => d.download_id !== download_id);
      this._emit();
      return blob;
    } catch (err) {
      await this.scan();
      throw err;
    } finally {
      this._inFlight.delete(download_id);
    }
  }

  async discard(download_id: string): Promise<void> {
    const entry = this._downloads.find(d => d.download_id === download_id);
    if (!entry) return;
    await resumableDownloader.discard(entry.url, entry.download_id);
    this._downloads = this._downloads.filter(d => d.download_id !== download_id);
    this._emit();
  }

  async discardStale(maxAgeMs: number): Promise<number> {
    const stale = this._downloads.filter(d => d.age_ms > maxAgeMs);
    for (const d of stale) {
      try { await this.discard(d.download_id); } catch { /* ignore */ }
    }
    return stale.length;
  }

  private _emit(): void {
    for (const l of this._listeners) {
      try { l(this._downloads); } catch { /* ignore */ }
    }
  }
}

export const downloadResumeBroker = new DownloadResumeBroker();
export default downloadResumeBroker;
