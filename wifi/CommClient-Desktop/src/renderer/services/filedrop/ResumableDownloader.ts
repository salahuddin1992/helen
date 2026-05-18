/**
 * ResumableDownloader — fetch a file from ``/api/files/:id`` with full
 * resume-after-disconnect semantics. Keeps an append-only buffer in
 * IndexedDB so a crash, OS suspend, or hours-long offline window doesn't
 * discard the partial payload. On resume, sends
 *   ``Range: bytes=<received>-``
 *   ``If-Range: <etag>``
 * so the server either replays the tail (206) or — if the file changed —
 * restarts from byte 0 (200). That matches ``files.py`` Range handling.
 *
 * The persisted state is small (meta only). Chunks stream straight to an
 * IndexedDB object store keyed by ``(download_id, offset)`` so we never
 * hold the whole body in memory — same pattern as the uploader but in the
 * opposite direction.
 */

export interface DownloadCallbacks {
  onProgress?: (p: { received: number; total: number | null; speedBps: number }) => void;
  onOffline?: () => void;
  onReconnected?: () => void;
  onComplete?: (blob: Blob) => void;
  onError?: (message: string) => void;
}

export interface DownloadOptions {
  /** Override the computed download_id (e.g. to match a UI transfer id). */
  downloadId?: string;
  /** Custom Authorization header ("Bearer …"). */
  authHeader?: string;
  /** Abort signal. Callers can wire this to UI cancel/pause. */
  signal?: AbortSignal;
  /** Max retries on transient HTTP errors (5xx). Default 8. */
  maxRetries?: number;
}

const IDB_NAME = 'commclient-downloads';
const IDB_META = 'meta';
const IDB_CHUNKS = 'chunks';
const IDB_VERSION = 1;

export interface DownloadMeta {
  download_id: string;
  url: string;
  file_name: string;
  mime_type: string;
  total_size: number | null;
  etag: string | null;
  received: number;
  created_at: number;
  updated_at: number;
  completed: boolean;
}

interface StoredChunk {
  download_id: string;
  offset: number;
  data: Uint8Array;
}

class DownloadStore {
  private _db: Promise<IDBDatabase> | null = null;

  private _open(): Promise<IDBDatabase> {
    if (this._db) return this._db;
    this._db = new Promise((res, rej) => {
      const req = indexedDB.open(IDB_NAME, IDB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(IDB_META)) {
          db.createObjectStore(IDB_META, { keyPath: 'download_id' });
        }
        if (!db.objectStoreNames.contains(IDB_CHUNKS)) {
          const store = db.createObjectStore(IDB_CHUNKS, {
            keyPath: ['download_id', 'offset'],
          });
          store.createIndex('by_download', 'download_id', { unique: false });
        }
      };
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
    return this._db;
  }

  async putMeta(meta: DownloadMeta): Promise<void> {
    const db = await this._open();
    await new Promise<void>((res, rej) => {
      const tx = db.transaction(IDB_META, 'readwrite');
      tx.objectStore(IDB_META).put(meta);
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }

  async getMeta(id: string): Promise<DownloadMeta | null> {
    const db = await this._open();
    return new Promise((res, rej) => {
      const tx = db.transaction(IDB_META, 'readonly');
      const r = tx.objectStore(IDB_META).get(id);
      r.onsuccess = () => res(r.result ?? null);
      r.onerror = () => rej(r.error);
    });
  }

  async putChunk(c: StoredChunk): Promise<void> {
    const db = await this._open();
    await new Promise<void>((res, rej) => {
      const tx = db.transaction(IDB_CHUNKS, 'readwrite');
      tx.objectStore(IDB_CHUNKS).put(c);
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }

  async listChunks(id: string): Promise<StoredChunk[]> {
    const db = await this._open();
    return new Promise((res, rej) => {
      const tx = db.transaction(IDB_CHUNKS, 'readonly');
      const idx = tx.objectStore(IDB_CHUNKS).index('by_download');
      const r = idx.getAll(id);
      r.onsuccess = () => {
        const rows = (r.result as StoredChunk[]).slice();
        rows.sort((a, b) => a.offset - b.offset);
        res(rows);
      };
      r.onerror = () => rej(r.error);
    });
  }

  async listAllMeta(): Promise<DownloadMeta[]> {
    const db = await this._open();
    return new Promise((res, rej) => {
      const tx = db.transaction(IDB_META, 'readonly');
      const r = tx.objectStore(IDB_META).getAll();
      r.onsuccess = () => res((r.result as DownloadMeta[]) ?? []);
      r.onerror = () => rej(r.error);
    });
  }

  async drop(id: string): Promise<void> {
    const db = await this._open();
    await new Promise<void>((res, rej) => {
      const tx = db.transaction([IDB_META, IDB_CHUNKS], 'readwrite');
      tx.objectStore(IDB_META).delete(id);
      const idx = tx.objectStore(IDB_CHUNKS).index('by_download');
      const cur = idx.openCursor(IDBKeyRange.only(id));
      cur.onsuccess = () => {
        const c = cur.result;
        if (c) {
          c.delete();
          c.continue();
        }
      };
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }
}

const store = new DownloadStore();

function makeDownloadId(url: string): string {
  // Stable per-URL so retries across restarts hit the same record.
  return `dl-${btoa(unescape(encodeURIComponent(url))).replace(/=+$/, '')}`;
}

export class ResumableDownloader {
  /**
   * Download a URL with resume-after-disconnect. Returns the assembled
   * Blob once all bytes have arrived.
   */
  async download(
    url: string,
    fileName: string,
    cb: DownloadCallbacks = {},
    opts: DownloadOptions = {},
  ): Promise<Blob> {
    const downloadId = opts.downloadId ?? makeDownloadId(url);
    const maxRetries = Math.max(1, Math.min(opts.maxRetries ?? 8, 30));

    const existing = await store.getMeta(downloadId);
    let meta: DownloadMeta = existing ?? {
      download_id: downloadId,
      url,
      file_name: fileName,
      mime_type: 'application/octet-stream',
      total_size: null,
      etag: null,
      received: 0,
      created_at: Date.now(),
      updated_at: Date.now(),
      completed: false,
    };

    // Re-sync received byte count from what's actually on disk — the meta
    // record could be newer than the last persisted chunk after a crash.
    const diskBytes = await this._sumChunks(downloadId);
    if (diskBytes !== meta.received) {
      meta.received = diskBytes;
    }

    if (meta.completed) {
      return this._reassemble(downloadId, meta);
    }

    let retries = 0;
    const startedAt = Date.now();
    let lastReportAt = startedAt;
    let lastReportBytes = meta.received;

    while (!meta.completed) {
      if (opts.signal?.aborted) throw new Error('download aborted');
      if (!this._isOnline()) {
        cb.onOffline?.();
        await this._waitForOnline(opts.signal);
        cb.onReconnected?.();
      }

      const headers: Record<string, string> = {};
      if (opts.authHeader) headers['Authorization'] = opts.authHeader;
      if (meta.received > 0) {
        headers['Range'] = `bytes=${meta.received}-`;
        if (meta.etag) headers['If-Range'] = meta.etag;
      }

      let resp: Response;
      try {
        resp = await fetch(url, { headers, signal: opts.signal });
      } catch (err) {
        if (opts.signal?.aborted) throw err;
        // Network layer error — wait for connectivity and retry.
        cb.onOffline?.();
        await this._waitForOnline(opts.signal);
        cb.onReconnected?.();
        continue;
      }

      if (resp.status === 200) {
        // Server served full body (no validator match or no Range
        // support). Discard whatever we had — it may not be valid.
        if (meta.received > 0) {
          await store.drop(downloadId);
          meta = {
            ...meta,
            received: 0,
            etag: resp.headers.get('ETag'),
            total_size: this._contentLength(resp),
            mime_type: resp.headers.get('Content-Type') ?? meta.mime_type,
            updated_at: Date.now(),
            completed: false,
          };
          await store.putMeta(meta);
        } else {
          meta.etag = resp.headers.get('ETag');
          meta.total_size = this._contentLength(resp);
          meta.mime_type = resp.headers.get('Content-Type') ?? meta.mime_type;
        }
      } else if (resp.status === 206) {
        if (meta.total_size === null) {
          meta.total_size = this._totalFromContentRange(resp);
        }
        if (!meta.etag) meta.etag = resp.headers.get('ETag');
      } else if (resp.status === 416) {
        // Range not satisfiable → our ``received`` is past EOF. Could
        // mean the file shrank, or we already have everything. Probe
        // with a HEAD to decide.
        const headResp = await fetch(url, {
          method: 'HEAD',
          headers: opts.authHeader ? { Authorization: opts.authHeader } : {},
        });
        const size = Number(headResp.headers.get('Content-Length'));
        if (Number.isFinite(size) && meta.received >= size) {
          meta.total_size = size;
          meta.completed = true;
          meta.updated_at = Date.now();
          await store.putMeta(meta);
          break;
        }
        // File shrank under us — start over.
        await store.drop(downloadId);
        meta.received = 0;
        meta.etag = null;
        meta.completed = false;
        meta.updated_at = Date.now();
        await store.putMeta(meta);
        continue;
      } else if (resp.status >= 500 && resp.status < 600) {
        retries++;
        if (retries > maxRetries) {
          cb.onError?.(`download failed: HTTP ${resp.status}`);
          throw new Error(`download failed: HTTP ${resp.status}`);
        }
        await this._backoff(retries, opts.signal);
        continue;
      } else {
        cb.onError?.(`download failed: HTTP ${resp.status}`);
        throw new Error(`download failed: HTTP ${resp.status}`);
      }

      await store.putMeta(meta);

      const reader = resp.body?.getReader();
      if (!reader) throw new Error('response body not readable');

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (opts.signal?.aborted) {
            try { await reader.cancel(); } catch { /* ignore */ }
            throw new Error('download aborted');
          }
          const chunk = value as Uint8Array;
          await store.putChunk({
            download_id: downloadId,
            offset: meta.received,
            data: chunk,
          });
          meta.received += chunk.byteLength;
          meta.updated_at = Date.now();
          // Throttle meta writes — chunk writes are authoritative.
          if (Date.now() - lastReportAt > 250) {
            const dt = (Date.now() - lastReportAt) / 1000;
            const speed = dt > 0 ? (meta.received - lastReportBytes) / dt : 0;
            cb.onProgress?.({
              received: meta.received,
              total: meta.total_size,
              speedBps: speed,
            });
            lastReportAt = Date.now();
            lastReportBytes = meta.received;
            // Persist meta periodically so resume knows how far we got.
            await store.putMeta(meta);
          }
        }
      } catch (err) {
        if (opts.signal?.aborted) throw err;
        // Stream broke — persist what we have and loop back to retry.
        await store.putMeta(meta);
        cb.onOffline?.();
        await this._waitForOnline(opts.signal);
        cb.onReconnected?.();
        continue;
      }

      await store.putMeta(meta);

      if (meta.total_size !== null && meta.received >= meta.total_size) {
        meta.completed = true;
        await store.putMeta(meta);
      } else if (meta.total_size === null) {
        // Server never advertised a length — best effort: assume EOF once
        // the reader ran dry without error.
        meta.completed = true;
        await store.putMeta(meta);
      } else {
        // Body ended short of total_size → loop and re-request the tail.
        continue;
      }
    }

    const blob = await this._reassemble(downloadId, meta);
    cb.onProgress?.({
      received: meta.received,
      total: meta.total_size,
      speedBps: 0,
    });
    cb.onComplete?.(blob);
    return blob;
  }

  async discard(url: string, downloadId?: string): Promise<void> {
    const id = downloadId ?? makeDownloadId(url);
    await store.drop(id);
  }

  /** Enumerate unfinished downloads persisted in IDB. Used by the resume
   * broker at app startup to surface "continue where you left off" entries. */
  async listResumable(): Promise<DownloadMeta[]> {
    const all = await store.listAllMeta();
    return all.filter(m => !m.completed);
  }

  /** Resume a specific persisted download. If the meta record is missing
   * (e.g. user already discarded) this starts a fresh download. */
  async resume(
    meta: Pick<DownloadMeta, 'download_id' | 'url' | 'file_name' | 'mime_type'>,
    cb: DownloadCallbacks = {},
    opts: DownloadOptions = {},
  ): Promise<Blob> {
    return this.download(meta.url, meta.file_name, cb, {
      ...opts,
      downloadId: meta.download_id,
    });
  }

  // ── Private helpers ────────────────────────────────────────

  private async _sumChunks(downloadId: string): Promise<number> {
    const chunks = await store.listChunks(downloadId);
    let total = 0;
    for (const c of chunks) total += c.data.byteLength;
    return total;
  }

  private async _reassemble(
    downloadId: string,
    meta: DownloadMeta,
  ): Promise<Blob> {
    const chunks = await store.listChunks(downloadId);
    // Copy into a dedicated ArrayBuffer so the Blob constructor is happy on
    // TS lib.dom's stricter BlobPart typing (SharedArrayBuffer exclusion).
    const parts: BlobPart[] = chunks.map(c => {
      const copy = new Uint8Array(c.data.byteLength);
      copy.set(c.data);
      return copy.buffer;
    });
    return new Blob(parts, { type: meta.mime_type || 'application/octet-stream' });
  }

  private _contentLength(resp: Response): number | null {
    const v = resp.headers.get('Content-Length');
    if (v === null) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  private _totalFromContentRange(resp: Response): number | null {
    const v = resp.headers.get('Content-Range');
    if (!v) return null;
    const m = /\/(\d+)$/.exec(v);
    return m ? Number(m[1]) : null;
  }

  private _isOnline(): boolean {
    return typeof navigator === 'undefined' || navigator.onLine !== false;
  }

  private _waitForOnline(signal?: AbortSignal): Promise<void> {
    if (this._isOnline()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let done = false;
      const cleanup = () => {
        window.removeEventListener('online', onOnline);
        clearInterval(poller);
        if (signal) signal.removeEventListener('abort', onAbort);
      };
      const onOnline = () => {
        if (done || !this._isOnline()) return;
        done = true;
        cleanup();
        resolve();
      };
      const onAbort = () => {
        if (done) return;
        done = true;
        cleanup();
        reject(new Error('download aborted'));
      };
      const poller = setInterval(onOnline, 1000);
      window.addEventListener('online', onOnline);
      if (signal) {
        if (signal.aborted) return onAbort();
        signal.addEventListener('abort', onAbort);
      }
    });
  }

  private _backoff(attempt: number, signal?: AbortSignal): Promise<void> {
    const ms = Math.min(30_000, 500 * Math.pow(2, attempt));
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        if (signal) signal.removeEventListener('abort', onAbort);
        resolve();
      }, ms);
      const onAbort = () => {
        clearTimeout(t);
        reject(new Error('download aborted'));
      };
      if (signal) {
        if (signal.aborted) return onAbort();
        signal.addEventListener('abort', onAbort);
      }
    });
  }
}

export const resumableDownloader = new ResumableDownloader();
export default resumableDownloader;
