/**
 * ResumableUploader — next-generation uploader that consumes the backend's
 * new `/api/files/resumable/*` protocol.
 *
 * Key properties:
 *   • Per-chunk CRC32 + SHA-256 integrity headers (matches server validation)
 *   • Session state persisted in IndexedDB → true resume across app restart
 *   • Parallel uploads with backpressure (configurable concurrency)
 *   • Deterministic resume: server reports `received_chunks` / bitmap,
 *     client skips anything already verified
 *   • Pause / resume / abort / cancel-all
 *   • Exponential backoff per-chunk retries
 *
 * This class is ADDITIVE — it does NOT replace ChunkedUploader.ts.
 * Callers can migrate at their own pace (FileDropManager gets a feature flag).
 */

import { getBaseUrl, refreshTokensIfPossible, notifyAuthFailed } from '../api.client';
import { useAuthStore } from '../../stores/auth.store';

// ─── Types ──────────────────────────────────────────────────────────────────

export interface ResumableProgress {
  uploaded: number;
  total: number;
  speedBps: number;
  chunkIndex: number;
  totalChunks: number;
  session_id: string;
}

export interface ResumableCallbacks {
  onProgress?: (p: ResumableProgress) => void;
  onComplete?: (fileId: string, session_id: string) => void;
  onError?: (message: string, session_id?: string) => void;
  onSessionCreated?: (session_id: string) => void;
  onResumed?: (session_id: string, resumedFromByte: number) => void;
  /** Fires every time the uploader pauses waiting for the network to return. */
  onOffline?: (session_id: string) => void;
  /** Fires when a previously-offline upload starts flowing again. */
  onReconnected?: (session_id: string) => void;
}

export interface ResumableUploadOptions {
  channelId?: string;
  chunkSize?: number;             // bytes; default 256 KB
  concurrency?: number;           // parallel chunk PUTs; default 4
  maxRetriesPerChunk?: number;    // default 5
  expectedSha256?: string;        // optional — if caller already hashed the file
  metadata?: Record<string, any>;
  /** Use an existing session_id (resumes it instead of creating a new one). */
  resumeSessionId?: string;
  /**
   * Dynamically adjust the number of parallel chunk PUTs based on observed
   * throughput. Starts with ``concurrency`` and may scale down on RTT
   * regression, then back up as conditions improve. Useful on flaky WiFi
   * where a fixed pool either underutilises good periods or piles up
   * retries during bad ones. Default: true.
   */
  autoConcurrency?: boolean;
}

interface InitResponse {
  session_id: string;
  chunk_size: number;
  total_chunks: number;
  received: number[];           // list of chunk indexes already on the server
  status: string;
  expires_at?: string;
}

interface ChunkResponse {
  session_id: string;
  received_chunks: number;
  total_chunks: number;
  bytes_received: number;
  status: string;
  duplicate?: boolean;
}

interface CompleteResponse {
  session_id: string;
  file_id: string;
  status: string;
  sha256: string;
  size: number;
}

/**
 * Thrown when the server tells us the session is gone (404) or already
 * past the point of no return (409 on completed/aborted). These are
 * terminal — retrying the same session_id is pointless; the caller
 * should either give up or re-init a fresh session.
 */
export class ResumableSessionGoneError extends Error {
  constructor(public readonly status: number, msg: string) {
    super(msg);
    this.name = 'ResumableSessionGoneError';
  }
}

// ─── Constants ──────────────────────────────────────────────────────────────

const DEFAULT_CHUNK_SIZE = 256 * 1024; // 256 KB — matches server DEFAULT
const DEFAULT_CONCURRENCY = 4;
const DEFAULT_MAX_RETRIES = 5;

/**
 * Auto-select a chunk size based on file size. The goal is to keep the
 * per-file chunk count between ~16 and ~4096 so we don't thrash the session
 * table on tiny files or pay per-request overhead N×1000 on huge ones.
 *
 *   ≤ 4 MB   → 256 KB chunks (N ≤ 16, fine on phones)
 *   ≤ 256 MB → 512 KB chunks
 *   ≤ 4 GB   → 2 MB chunks
 *   > 4 GB   → 4 MB chunks (server MAX_CHUNK_SIZE)
 */
function autoChunkSize(fileSize: number): number {
  if (fileSize <= 4 * 1024 * 1024) return 256 * 1024;
  if (fileSize <= 256 * 1024 * 1024) return 512 * 1024;
  if (fileSize <= 4 * 1024 * 1024 * 1024) return 2 * 1024 * 1024;
  return 4 * 1024 * 1024;
}
const IDB_NAME = 'commclient-resumable';
const IDB_STORE = 'sessions';
const IDB_VERSION = 1;

// ─── Adaptive concurrency tuner ────────────────────────────────────────────

/**
 * Tracks observed per-chunk throughput (bytes/ms) and adjusts a target
 * concurrency level. Grows additively while throughput improves,
 * shrinks multiplicatively on regression.
 *
 * The math is deliberately simple — pick two non-overlapping windows of
 * ``windowSize`` chunks each and compare. If the newer window is notably
 * faster (per-chunk), raise the target; if it's notably slower, cut it
 * roughly in half. Clamped to ``[min, max]``.
 */
class ConcurrencyTuner {
  target: number;
  private readonly _min: number;
  private readonly _max: number;
  private readonly _enabled: boolean;
  private _samples: number[] = [];   // bytes / ms per chunk
  private readonly _windowSize = 6;
  private _lastWindowAvg: number | null = null;

  constructor(opts: { min: number; max: number; initial: number; enabled: boolean }) {
    this._min = Math.max(1, opts.min);
    this._max = Math.max(this._min, opts.max);
    this._enabled = opts.enabled;
    this.target = Math.min(this._max, Math.max(this._min, opts.initial));
  }

  record(bytes: number, durationMs: number): void {
    if (!this._enabled) return;
    if (durationMs <= 0 || bytes <= 0) return;
    this._samples.push(bytes / durationMs);
    if (this._samples.length < this._windowSize) return;

    const avg = this._samples.reduce((a, b) => a + b, 0) / this._samples.length;
    this._samples = [];

    if (this._lastWindowAvg === null) {
      this._lastWindowAvg = avg;
      // First window done — take one step up to start probing.
      if (this.target < this._max) this.target++;
      return;
    }

    const prev = this._lastWindowAvg;
    const ratio = avg / prev;

    if (ratio > 1.08 && this.target < this._max) {
      // Throughput improved meaningfully — grow.
      this.target++;
    } else if (ratio < 0.85 && this.target > this._min) {
      // Regressed — back off (halve, at least -1).
      this.target = Math.max(this._min, Math.floor(this.target / 2));
    }
    this._lastWindowAvg = avg;
  }
}

// ─── CRC32 (zlib-compatible polynomial 0xEDB88320) ─────────────────────────

const _crc32Table: Uint32Array = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    }
    t[i] = c >>> 0;
  }
  return t;
})();

function crc32OfBytes(bytes: Uint8Array): number {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) {
    c = _crc32Table[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

async function sha256HexOfBytes(bytes: Uint8Array): Promise<string> {
  // Slice into a standalone ArrayBuffer — some typings treat generic
  // Uint8Array variants as not-a-BufferSource (SharedArrayBuffer variant).
  const buf: ArrayBuffer = bytes.slice().buffer as ArrayBuffer;
  const digest = await crypto.subtle.digest('SHA-256', buf);
  const hex = Array.from(new Uint8Array(digest))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
  return hex;
}

// ─── IndexedDB persistence (for resume-across-restart) ─────────────────────

interface StoredSession {
  session_id: string;
  file_name: string;
  file_size: number;
  mime_type: string;
  chunk_size: number;
  total_chunks: number;
  channel_id?: string;
  expected_sha256?: string;
  received_mask: Uint8Array;   // bitmap of uploaded chunk indexes
  created_at: number;
  updated_at: number;
}

class SessionStore {
  private _dbPromise: Promise<IDBDatabase> | null = null;

  private _open(): Promise<IDBDatabase> {
    if (this._dbPromise) return this._dbPromise;
    this._dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(IDB_NAME, IDB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(IDB_STORE)) {
          db.createObjectStore(IDB_STORE, { keyPath: 'session_id' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return this._dbPromise;
  }

  async put(s: StoredSession): Promise<void> {
    const db = await this._open();
    await new Promise<void>((res, rej) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).put(s);
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }

  async get(session_id: string): Promise<StoredSession | null> {
    const db = await this._open();
    return new Promise<StoredSession | null>((res, rej) => {
      const tx = db.transaction(IDB_STORE, 'readonly');
      const r = tx.objectStore(IDB_STORE).get(session_id);
      r.onsuccess = () => res(r.result ?? null);
      r.onerror = () => rej(r.error);
    });
  }

  async delete(session_id: string): Promise<void> {
    const db = await this._open();
    await new Promise<void>((res, rej) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).delete(session_id);
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }

  async listActive(): Promise<StoredSession[]> {
    const db = await this._open();
    return new Promise<StoredSession[]>((res, rej) => {
      const tx = db.transaction(IDB_STORE, 'readonly');
      const r = tx.objectStore(IDB_STORE).getAll();
      r.onsuccess = () => res(r.result ?? []);
      r.onerror = () => rej(r.error);
    });
  }
}

// ─── Resumable uploader ─────────────────────────────────────────────────────

export class ResumableUploader {
  private store = new SessionStore();
  private _active: Map<string, AbortController> = new Map();
  private _paused: Set<string> = new Set();

  /**
   * Upload a file using the resumable protocol.
   * Returns the final file_id (compatible with existing FileRecord IDs).
   */
  async uploadFile(
    file: File,
    cb: ResumableCallbacks,
    opts: ResumableUploadOptions = {},
  ): Promise<string> {
    const chunkSize = this._clampChunkSize(opts.chunkSize ?? autoChunkSize(file.size));
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
    const concurrency = Math.max(1, Math.min(opts.concurrency ?? DEFAULT_CONCURRENCY, 16));
    const maxRetries = Math.max(1, Math.min(opts.maxRetriesPerChunk ?? DEFAULT_MAX_RETRIES, 20));
    const autoConcurrency = opts.autoConcurrency !== false;

    // ── 1. INIT (or resume an existing session) ─────────────────────────
    let session: InitResponse;
    if (opts.resumeSessionId) {
      try {
        session = await this._getStatus(opts.resumeSessionId);
        cb.onResumed?.(session.session_id, session.received.length * session.chunk_size);
      } catch {
        session = await this._init(file, chunkSize, totalChunks, opts);
        cb.onSessionCreated?.(session.session_id);
      }
    } else {
      session = await this._init(file, chunkSize, totalChunks, opts);
      cb.onSessionCreated?.(session.session_id);
    }

    const { session_id } = session;
    const receivedSet = new Set<number>(session.received ?? []);

    // Persist session metadata for cross-restart resume
    await this.store.put({
      session_id,
      file_name: file.name,
      file_size: file.size,
      mime_type: file.type || 'application/octet-stream',
      chunk_size: session.chunk_size,
      total_chunks: session.total_chunks,
      channel_id: opts.channelId,
      expected_sha256: opts.expectedSha256,
      received_mask: this._maskFromSet(receivedSet, session.total_chunks),
      created_at: Date.now(),
      updated_at: Date.now(),
    });

    const abort = new AbortController();
    this._active.set(session_id, abort);

    // ── 2. Parallel chunk upload with bounded concurrency ───────────────
    const pending: number[] = [];
    for (let i = 0; i < session.total_chunks; i++) {
      if (!receivedSet.has(i)) pending.push(i);
    }

    const startTime = Date.now();
    let uploadedBytes = receivedSet.size * session.chunk_size;
    let lastReportBytes = uploadedBytes;
    let lastReportTime = startTime;

    const reportProgress = (chunkIndex: number) => {
      const now = Date.now();
      const dt = (now - lastReportTime) / 1000;
      const db = uploadedBytes - lastReportBytes;
      const speed = dt > 0 ? db / dt : 0;
      cb.onProgress?.({
        uploaded: Math.min(uploadedBytes, file.size),
        total: file.size,
        speedBps: speed,
        chunkIndex,
        totalChunks: session.total_chunks,
        session_id,
      });
      lastReportBytes = uploadedBytes;
      lastReportTime = now;
    };

    // Helpers for the offline-recovery branch below.
    const chunkStart = (i: number) => i * session.chunk_size;
    const chunkEnd = (i: number) => Math.min(chunkStart(i) + session.chunk_size, file.size);

    // Adaptive concurrency tuner: starts small and grows while throughput
    // climbs, shrinks when RTT regresses. On flaky WiFi this keeps us from
    // piling retries on top of a saturated link; on LAN it ramps up to the
    // caller's cap quickly.
    const tuner = new ConcurrencyTuner({
      min: 1,
      max: concurrency,
      initial: autoConcurrency ? Math.min(2, concurrency) : concurrency,
      enabled: autoConcurrency,
    });

    const runOne = async (idx: number): Promise<void> => {
      if (abort.signal.aborted) throw new Error('upload aborted');
      while (this._paused.has(session_id)) {
        await this._sleep(250);
        if (abort.signal.aborted) throw new Error('upload aborted');
      }

      // If we already know the network is down, park here until it's back.
      // This is the "internet died in the middle of upload" path — instead
      // of burning our retry budget, we wait until `online` fires.
      if (!this._isOnline()) {
        cb.onOffline?.(session_id);
        await this._waitForOnline(abort.signal);
        cb.onReconnected?.(session_id);
        // Re-sync with server: a chunk may have landed just before the drop.
        await this._reconcileReceived(session_id, receivedSet);
        if (receivedSet.has(idx)) {
          uploadedBytes = Math.min(file.size, uploadedBytes + (chunkEnd(idx) - chunkStart(idx)));
          reportProgress(idx);
          return;
        }
      }

      const s = chunkStart(idx);
      const e = chunkEnd(idx);
      const blob = file.slice(s, e);
      const bytes = new Uint8Array(await blob.arrayBuffer());

      const crc = crc32OfBytes(bytes);
      const sha = await sha256HexOfBytes(bytes);

      let attempt = 0;
      let networkFailures = 0;
      while (true) {
        try {
          const t0 = performance.now();
          const res = await this._putChunk(session_id, idx, bytes, crc, sha, s, e, file.size, abort.signal);
          const t1 = performance.now();
          tuner.record(bytes.length, t1 - t0);
          uploadedBytes += bytes.length;
          receivedSet.add(idx);
          if (res.duplicate !== true) {
            try {
              const rec = await this.store.get(session_id);
              if (rec) {
                rec.received_mask[idx >> 3] |= (1 << (idx & 7));
                rec.updated_at = Date.now();
                await this.store.put(rec);
              }
            } catch { /* tolerate */ }
          }
          reportProgress(idx);
          return;
        } catch (err: any) {
          if (abort.signal.aborted) throw err;
          if (err instanceof ResumableSessionGoneError) {
            try { await this.store.delete(session_id); } catch { /* ignore */ }
            throw err;
          }

          // Network-class failure: either the event loop observed an offline
          // state, or fetch blew up with a TypeError (DNS, ECONNRESET, etc.).
          // These should NOT consume the bounded retry budget — the user's
          // internet may come back 5 minutes or 5 hours later, and we want
          // to resume from the SAME byte, not fail and force a restart.
          if (this._isNetworkError(err) || !this._isOnline()) {
            networkFailures++;
            cb.onOffline?.(session_id);
            await this._waitForOnline(abort.signal);
            cb.onReconnected?.(session_id);
            // After reconnect, ask the server what it actually has. A chunk
            // may have been persisted just before the TCP reset.
            await this._reconcileReceived(session_id, receivedSet);
            if (receivedSet.has(idx)) {
              uploadedBytes += bytes.length;
              reportProgress(idx);
              return;
            }
            // Small jitter so N parallel workers don't hammer the server
            // in lockstep on first reconnect.
            await this._sleep(100 + Math.random() * 400);
            continue;
          }

          attempt++;
          if (attempt >= maxRetries) {
            throw new Error(`chunk ${idx} failed after ${maxRetries} attempts (net drops: ${networkFailures}): ${err?.message ?? err}`);
          }
          const backoff = Math.min(30_000, 500 * 2 ** (attempt - 1)) + Math.random() * 250;
          await this._sleep(backoff);
        }
      }
    };

    // Dynamic worker pool — the live worker count follows ``tuner.target``.
    // When target grows we spawn fresh runners; when it shrinks runners exit
    // voluntarily after finishing their current chunk. A runner that finds
    // the queue empty also exits.
    const queue = [...pending];
    let liveRunners = 0;
    const runners: Set<Promise<void>> = new Set();

    let firstError: unknown = null;

    const spawnRunner = () => {
      liveRunners++;
      const p = (async () => {
        try {
          while (queue.length > 0 && !abort.signal.aborted) {
            if (liveRunners > tuner.target) {
              // Voluntary exit — pool is shrinking.
              return;
            }
            const next = queue.shift();
            if (next === undefined) return;
            await runOne(next);
          }
        } catch (err) {
          if (!firstError) {
            firstError = err;
            try { abort.abort(); } catch { /* ignore */ }
          }
          throw err;
        } finally {
          liveRunners--;
        }
      })();
      runners.add(p);
      p.catch(() => { /* collected via firstError */ })
        .finally(() => runners.delete(p));
    };

    const ensureRunners = () => {
      const target = Math.min(tuner.target, queue.length);
      while (liveRunners < target) spawnRunner();
    };

    ensureRunners();
    // Re-evaluate periodically so the tuner's target changes materialise.
    // Workers auto-exit when oversubscribed; this covers the grow path.
    const tuneTimer = setInterval(() => {
      if (abort.signal.aborted) return;
      ensureRunners();
    }, 500);

    try {
      // Drain: wait until every spawned runner resolves and the queue is
      // empty. New runners may be added mid-flight by the tuner tick.
      while (runners.size > 0 || queue.length > 0) {
        await Promise.allSettled([...runners]);
        if (firstError) break;
        if (queue.length > 0) ensureRunners();
      }
    } finally {
      clearInterval(tuneTimer);
    }

    if (firstError) {
      this._active.delete(session_id);
      const msg = (firstError as any)?.message ?? String(firstError);
      cb.onError?.(msg, session_id);
      throw firstError;
    }

    // ── 3. COMPLETE ──────────────────────────────────────────────────────
    // Wrap finalize in a hard timeout. If the server is alive but slow
    // (or hung at the SHA-256 verification stage on a huge file), the
    // uploader could otherwise hang at 99% with no progress and no
    // error — the user thinks it's almost done forever. 60 s is enough
    // headroom for legitimate long verifications and a reasonable cap
    // before we surface the failure.
    let completed: CompleteResponse;
    try {
      completed = await Promise.race([
        this._complete(session_id, opts.expectedSha256),
        new Promise<never>((_, reject) =>
          setTimeout(
            () => reject(new Error('finalize timeout (60s) — server did not acknowledge upload completion')),
            60_000,
          ),
        ),
      ]);
    } catch (err: any) {
      this._active.delete(session_id);
      cb.onError?.(err?.message ?? 'complete failed', session_id);
      throw err;
    }

    // Final progress tick
    cb.onProgress?.({
      uploaded: file.size,
      total: file.size,
      speedBps: (file.size - lastReportBytes) / Math.max(1, (Date.now() - lastReportTime) / 1000),
      chunkIndex: session.total_chunks - 1,
      totalChunks: session.total_chunks,
      session_id,
    });

    // Clean up persistent record — upload is complete
    try { await this.store.delete(session_id); } catch { /* tolerate */ }
    this._active.delete(session_id);

    cb.onComplete?.(completed.file_id, session_id);
    return completed.file_id;
  }

  // ── Pause / resume / abort ────────────────────────────────────────────

  pause(session_id: string): void {
    this._paused.add(session_id);
  }

  resume(session_id: string): void {
    this._paused.delete(session_id);
  }

  async abort(session_id: string): Promise<void> {
    const ctrl = this._active.get(session_id);
    if (ctrl) ctrl.abort();
    this._active.delete(session_id);
    this._paused.delete(session_id);
    try {
      await this._deleteSession(session_id);
    } catch { /* tolerate */ }
    try { await this.store.delete(session_id); } catch { /* tolerate */ }
  }

  cancelAll(): void {
    for (const [sid, c] of this._active) {
      try { c.abort(); } catch { /* ignore */ }
      this._paused.delete(sid);
    }
    this._active.clear();
  }

  /** List sessions persisted in IndexedDB that can be resumed. */
  async listResumable(): Promise<StoredSession[]> {
    return this.store.listActive();
  }

  /**
   * Resume every persisted session that still has a corresponding File handle
   * available (passed in via ``fileResolver``). Call this on app startup so
   * uploads interrupted by a crash/quit/network-drop pick up automatically.
   *
   * ``fileResolver`` is responsible for producing a ``File`` object from the
   * persisted metadata (e.g. re-prompting the user, reading from a saved
   * path in the Electron main process, etc.). Returning ``null`` skips that
   * session; the caller may later call ``abort(session_id)`` to discard it.
   */
  async resumeAll(
    fileResolver: (s: StoredSession) => Promise<File | null>,
    cb: ResumableCallbacks,
  ): Promise<string[]> {
    const sessions = await this.listResumable();
    const resumed: string[] = [];
    for (const s of sessions) {
      if (this._active.has(s.session_id)) continue;  // already running
      try {
        const file = await fileResolver(s);
        if (!file) continue;
        // Soft network guard — if offline, wait rather than immediately fail.
        if (!this._isOnline()) {
          await this._waitForOnline();
        }
        const fileId = await this.uploadFile(file, cb, {
          channelId: s.channel_id,
          chunkSize: s.chunk_size,
          expectedSha256: s.expected_sha256,
          resumeSessionId: s.session_id,
        });
        resumed.push(fileId);
      } catch (err) {
        // Session may be gone on the server (404/409) — drop the record
        // so we don't keep re-trying something that will never succeed.
        if (err instanceof ResumableSessionGoneError) {
          try { await this.store.delete(s.session_id); } catch { /* ignore */ }
        }
        cb.onError?.((err as Error)?.message ?? String(err), s.session_id);
      }
    }
    return resumed;
  }

  // ── Private: network awareness ─────────────────────────────────────────

  /**
   * ``navigator.onLine`` is advisory but reliable enough in Electron: Chromium
   * wires it to the OS network stack. We treat ``undefined`` (older envs) as
   * online so we don't silently stall.
   */
  private _isOnline(): boolean {
    return typeof navigator === 'undefined' || navigator.onLine !== false;
  }

  /**
   * Resolve when the browser reports ``online`` again. Aborts if the caller's
   * signal fires. Uses both event + polling (the event doesn't always fire
   * reliably on Windows wifi drops).
   */
  private _waitForOnline(signal?: AbortSignal): Promise<void> {
    if (this._isOnline()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      let resolved = false;
      const cleanup = () => {
        window.removeEventListener('online', onOnline);
        clearInterval(poller);
        if (signal) signal.removeEventListener('abort', onAbort);
      };
      const onOnline = () => {
        if (resolved) return;
        if (this._isOnline()) {
          resolved = true;
          cleanup();
          resolve();
        }
      };
      const onAbort = () => {
        if (resolved) return;
        resolved = true;
        cleanup();
        reject(new Error('upload aborted'));
      };
      const poller = setInterval(onOnline, 1000);
      window.addEventListener('online', onOnline);
      if (signal) {
        if (signal.aborted) return onAbort();
        signal.addEventListener('abort', onAbort);
      }
    });
  }

  /**
   * Fetch fails with ``TypeError: Failed to fetch`` for DNS / ECONNRESET /
   * socket-close / CORS — all of which are really "network", not "server".
   * ``AbortError`` is a controlled shutdown; the outer branch handles it.
   */
  private _isNetworkError(err: unknown): boolean {
    if (!err) return false;
    if (err instanceof TypeError) return true;
    const msg = String((err as any)?.message ?? err).toLowerCase();
    return (
      msg.includes('failed to fetch') ||
      msg.includes('network') ||
      msg.includes('econn') ||
      msg.includes('enotfound') ||
      msg.includes('etimedout') ||
      msg.includes('load failed')
    );
  }

  /**
   * After a reconnect, the server is the source of truth for "which chunks
   * actually landed". Pull the authoritative ``received`` list and fold it
   * into the local set so we don't re-upload bytes the server already has.
   */
  private async _reconcileReceived(
    session_id: string,
    receivedSet: Set<number>,
  ): Promise<void> {
    try {
      const status = await this._getStatus(session_id);
      for (const idx of status.received ?? []) {
        receivedSet.add(idx);
      }
      // Fold back into IndexedDB so a later restart has the same view.
      const rec = await this.store.get(session_id);
      if (rec) {
        for (const idx of receivedSet) {
          rec.received_mask[idx >> 3] |= (1 << (idx & 7));
        }
        rec.updated_at = Date.now();
        await this.store.put(rec);
      }
    } catch {
      /* reconcile is best-effort — worst case we re-PUT a chunk and the
         server dedups via the 'duplicate: true' ack. */
    }
  }

  // ── Private: HTTP interactions ─────────────────────────────────────────

  private _authHeaders(): Record<string, string> {
    const tokens = useAuthStore.getState().tokens;
    const h: Record<string, string> = {};
    if (tokens?.access_token) h['Authorization'] = `Bearer ${tokens.access_token}`;
    return h;
  }

  /**
   * Wraps `fetch` with an automatic 401 → refresh → retry loop. This is
   * critical for long-running uploads: an access token that was valid at
   * session init may expire mid-upload after 60m, and without a retry the
   * entire session would fail even though a refresh token was available.
   *
   * The retry is bounded to a single attempt per call; if the refresh itself
   * fails we propagate a 401 so the caller's backoff handles it.
   */
  private async _authedFetch(
    url: string,
    init: RequestInit,
  ): Promise<Response> {
    const mergeAuth = (i: RequestInit): RequestInit => ({
      ...i,
      headers: { ...(i.headers as Record<string, string> | undefined), ...this._authHeaders() },
    });

    let res = await fetch(url, mergeAuth(init));
    if (res.status !== 401) return res;

    // Consume and discard any body so the connection is released cleanly.
    try { await res.text(); } catch { /* ignore */ }

    const ok = await refreshTokensIfPossible();
    if (!ok) {
      notifyAuthFailed();
      return res;  // Let caller see the original 401.
    }

    res = await fetch(url, mergeAuth(init));
    return res;
  }

  private async _init(
    file: File,
    chunkSize: number,
    totalChunks: number,
    opts: ResumableUploadOptions,
  ): Promise<InitResponse> {
    const body = {
      filename: file.name,
      total_size: file.size,
      mime_type: file.type || 'application/octet-stream',
      chunk_size: chunkSize,
      expected_sha256: opts.expectedSha256,
      channel_id: opts.channelId,
      metadata: opts.metadata ?? null,
    };
    const res = await this._authedFetch(`${getBaseUrl()}/api/files/resumable/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const t = await res.text().catch(() => res.statusText);
      throw new Error(`init failed (${res.status}): ${t}`);
    }
    const data = await res.json();
    return {
      session_id: data.session_id ?? data.id,
      chunk_size: data.chunk_size ?? chunkSize,
      total_chunks: data.total_chunks ?? totalChunks,
      received: Array.isArray(data.received) ? data.received : [],
      status: data.status ?? 'init',
      expires_at: data.expires_at,
    };
  }

  private async _putChunk(
    session_id: string,
    index: number,
    bytes: Uint8Array,
    crc32: number,
    sha256Hex: string,
    start: number,
    end: number,
    totalSize: number,
    signal: AbortSignal,
  ): Promise<ChunkResponse> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/octet-stream',
      'Content-Range': `bytes ${start}-${end - 1}/${totalSize}`,
      'X-Chunk-CRC32': String(crc32),
      'X-Chunk-SHA256': sha256Hex,
    };
    const res = await this._authedFetch(
      `${getBaseUrl()}/api/files/resumable/${session_id}/chunk/${index}`,
      {
        method: 'PUT',
        headers,
        // Use the raw ArrayBuffer to keep lib.dom typings happy across
        // Node/Electron variants where Uint8Array<SharedArrayBuffer>
        // isn't treated as BodyInit.
        body: bytes.slice().buffer as ArrayBuffer,
        signal,
      },
    );
    if (res.status === 422) {
      const t = await res.text().catch(() => 'integrity error');
      throw new Error(`chunk integrity failed: ${t}`);
    }
    // 404 = session id unknown / expired / GC'd on server.
    // 409 = session already terminal (completed / aborted / failed).
    // In both cases further retries are pointless; surface a typed error
    // so the outer loop can abort the whole upload and let the caller
    // restart from scratch.
    if (res.status === 404 || res.status === 409) {
      const t = await res.text().catch(() => res.statusText);
      throw new ResumableSessionGoneError(
        res.status,
        `chunk put: session gone (${res.status}): ${t}`,
      );
    }
    if (!res.ok) {
      const t = await res.text().catch(() => res.statusText);
      throw new Error(`chunk put failed (${res.status}): ${t}`);
    }
    return await res.json();
  }

  private async _getStatus(session_id: string): Promise<InitResponse> {
    const res = await this._authedFetch(
      `${getBaseUrl()}/api/files/resumable/${session_id}/status`,
      { method: 'GET' },
    );
    if (!res.ok) throw new Error(`status failed (${res.status})`);
    const data = await res.json();
    return {
      session_id: data.session_id ?? session_id,
      chunk_size: data.chunk_size,
      total_chunks: data.total_chunks,
      received: Array.isArray(data.received) ? data.received : [],
      status: data.status ?? 'unknown',
      expires_at: data.expires_at,
    };
  }

  private async _complete(session_id: string, expected_sha256?: string): Promise<CompleteResponse> {
    const res = await this._authedFetch(
      `${getBaseUrl()}/api/files/resumable/${session_id}/complete`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_sha256: expected_sha256 ?? null }),
      },
    );
    if (!res.ok) {
      const t = await res.text().catch(() => res.statusText);
      throw new Error(`complete failed (${res.status}): ${t}`);
    }
    return await res.json();
  }

  private async _deleteSession(session_id: string): Promise<void> {
    await this._authedFetch(
      `${getBaseUrl()}/api/files/resumable/${session_id}`,
      { method: 'DELETE' },
    );
  }

  // ── Utilities ─────────────────────────────────────────────────────────

  private _clampChunkSize(size: number): number {
    const MIN = 16 * 1024;
    const MAX = 4 * 1024 * 1024;
    if (size < MIN) return MIN;
    if (size > MAX) return MAX;
    return size;
  }

  private _maskFromSet(set: Set<number>, total: number): Uint8Array {
    const bytes = Math.ceil(total / 8);
    const arr = new Uint8Array(bytes);
    for (const idx of set) {
      arr[idx >> 3] |= (1 << (idx & 7));
    }
    return arr;
  }

  private _sleep(ms: number): Promise<void> {
    return new Promise(r => setTimeout(r, ms));
  }
}

// Singleton-ish helper so callers can share one instance.
export const resumableUploader = new ResumableUploader();
