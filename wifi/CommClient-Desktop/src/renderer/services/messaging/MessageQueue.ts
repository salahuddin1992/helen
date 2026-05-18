/**
 * MessageQueue — local outbound message queue with retry and persistence.
 *
 * Ensures messages are delivered even when the socket connection is
 * temporarily lost. Messages are queued locally, sent via Socket.IO,
 * and removed from the queue only after server ACK.
 *
 * Features:
 *   - Optimistic UI: returns a client-side temp ID immediately
 *   - Automatic retry with exponential backoff
 *   - Queue persistence in memory (survives brief disconnects)
 *   - Deduplication via client_id matching on server ACK
 *   - FIFO ordering per channel
 *   - Configurable max retries and backoff
 */

import { socketManager } from '../socket.manager';

export type MessageType = 'text' | 'file' | 'image' | 'reply';

export interface OutboundMessage {
  /** Client-generated unique ID for dedup */
  clientId: string;
  channelId: string;
  content: string;
  type: MessageType;
  replyTo?: string;
  fileId?: string;
  /** Server-assigned ID after ACK */
  serverId?: string;
  /** ISO timestamp when queued */
  queuedAt: string;
  /** Number of send attempts */
  attempts: number;
  /** Current status */
  status: 'queued' | 'sending' | 'sent' | 'failed';
  /** Error from last attempt */
  lastError?: string;
}

export interface QueueCallbacks {
  /** Message was ACKed by server — UI should update temp → real ID */
  onSent: (clientId: string, serverId: string, createdAt: string) => void;
  /** Message send failed permanently (max retries exceeded) */
  onFailed: (clientId: string, error: string) => void;
  /** Queue processing state changed */
  onQueueStateChange: (pending: number, sending: number) => void;
}

// ── Config ──────────────────────────────────────────

const MAX_RETRIES = 5;
const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const STORAGE_KEY = 'messageQueue_pending_v1';
/** Cap stored queue size to avoid unbounded localStorage growth. Older
 *  messages get dropped on overflow — they have a queuedAt timestamp,
 *  so it's a strict FIFO trim. */
const MAX_PERSISTED_MESSAGES = 200;

let _idCounter = 0;

function generateClientId(): string {
  _idCounter += 1;
  return `msg_${Date.now()}_${_idCounter}_${Math.random().toString(36).slice(2, 8)}`;
}

/** True for errors likely caused by network/socket state rather than the
 * server explicitly rejecting the payload. These should NOT burn the retry
 * budget — we want to wait out the outage and try again.
 *
 * Audit fix: previous version classified by string-match on words
 * like "timeout" and "fetch", which collided with legitimate server
 * error messages (e.g. "rate limit, fetch later" or
 * "Channel removed; messages won't fetch"). Those got infinite-
 * retried. Now we ALSO check for an explicit non-transient signal:
 *   - response carries `code` 4xx (other than 408/429) → fatal
 *   - response carries `permanent: true` → fatal
 * If the error has a 4xx-style status code that isn't a known
 * transient one, we override the string match and treat as fatal.
 */
function _isNetworkLikeError(err: any): boolean {
  if (!err) return false;

  // Fatal (server-rejected) status codes — short-circuit and refuse
  // to classify as transient regardless of message contents.
  const code = (err as any)?.status ?? (err as any)?.code;
  if (typeof code === 'number') {
    // 408 Request Timeout, 429 Too Many Requests are transient.
    // 5xx are server-side, retry. 4xx others are fatal.
    if (code === 408 || code === 429) return true;
    if (code >= 400 && code < 500) return false;
    if (code >= 500) return true;
  }
  if ((err as any)?.permanent === true) return false;

  if (err instanceof TypeError) return true;
  const msg = String(err?.message ?? err).toLowerCase();
  return (
    msg.includes('network') ||
    msg.includes('disconnect') ||
    msg.includes('socket') ||
    msg.includes('econn') ||
    msg.includes('not connected') ||
    msg.includes('connection refused') ||
    msg.includes('connection reset') ||
    // Carefully bounded "timeout" — must look like a verb, not a
    // server message field that happens to contain "timeout, please".
    /\btimed\s*out\b|\btimeout\b(?!\s*[,;:])/.test(msg) ||
    // "fetch" similarly — match only the network-fetch sense.
    /\bfailed\s+to\s+fetch\b|\bnetwork\s+request\s+failed\b|\bxhr\b/.test(msg)
  );
}

export class MessageQueue {
  private queue: Map<string, OutboundMessage> = new Map();
  private callbacks: QueueCallbacks;
  private _processing = false;
  private _processTimer: ReturnType<typeof setTimeout> | null = null;
  private _needsReprocess = false;
  private _destroyed = false;

  constructor(callbacks: QueueCallbacks) {
    this.callbacks = callbacks;
    this._restore();
  }

  /**
   * Restore the persisted queue from localStorage. Called on construct
   * so messages typed before a desktop crash / forced quit aren't lost.
   * Anything in `sending` state at shutdown is reverted to `queued`
   * because it never got an ACK (the server may or may not have stored
   * it; the client_id dedup on the server side handles the duplicate
   * case where the message did land but the ACK was lost).
   */
  private _restore(): void {
    try {
      if (typeof localStorage === 'undefined') return;
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return;
      for (const m of parsed) {
        if (!m || typeof m !== 'object' || !m.clientId) continue;
        const msg: OutboundMessage = {
          clientId: String(m.clientId),
          channelId: String(m.channelId || ''),
          content: String(m.content || ''),
          type: m.type === 'file' || m.type === 'image' || m.type === 'reply'
            ? m.type : 'text',
          replyTo: m.replyTo ? String(m.replyTo) : undefined,
          fileId: m.fileId ? String(m.fileId) : undefined,
          serverId: m.serverId ? String(m.serverId) : undefined,
          queuedAt: String(m.queuedAt || new Date().toISOString()),
          attempts: Number.isFinite(m.attempts) ? Math.max(0, m.attempts) : 0,
          status: m.status === 'failed' ? 'failed' : 'queued',
          lastError: m.lastError ? String(m.lastError) : undefined,
        };
        this.queue.set(msg.clientId, msg);
      }
      if (this.queue.size > 0) {
        console.log(`[MessageQueue] Restored ${this.queue.size} pending messages from disk`);
        this._emitQueueState();
        // Don't fire processing immediately — socket may not be connected
        // yet. The 'connect' listener / external flush() will pick these
        // up. We schedule a delayed attempt as a belt-and-suspenders.
        this._scheduleProcess(BASE_BACKOFF_MS);
      }
    } catch (e) {
      console.warn('[MessageQueue] restore failed:', e);
    }
  }

  /**
   * Persist the current queue to localStorage. Called after every state
   * mutation so a crash between writes can't lose more than the
   * in-flight transition. Storage size is capped to MAX_PERSISTED_MESSAGES
   * (FIFO drop) so a wedged-offline queue can't grow unboundedly.
   */
  private _persist(): void {
    try {
      if (typeof localStorage === 'undefined') return;
      const all = Array.from(this.queue.values());
      // Already-sent (deleted from queue) won't be here. Sort by queuedAt
      // and trim if oversize.
      all.sort((a, b) => a.queuedAt.localeCompare(b.queuedAt));
      const toStore = all.length > MAX_PERSISTED_MESSAGES
        ? all.slice(all.length - MAX_PERSISTED_MESSAGES)
        : all;
      if (toStore.length === 0) {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(toStore));
      }
    } catch (e) {
      // Out-of-space / privacy mode — fall back to in-memory only.
      console.warn('[MessageQueue] persist failed:', e);
    }
  }

  get pendingCount(): number {
    let count = 0;
    for (const msg of this.queue.values()) {
      if (msg.status === 'queued' || msg.status === 'sending') count++;
    }
    return count;
  }

  get allPending(): OutboundMessage[] {
    return Array.from(this.queue.values()).filter(
      (m) => m.status === 'queued' || m.status === 'sending'
    );
  }

  /**
   * Enqueue a message for sending.
   * Returns the client-side temporary ID for optimistic UI.
   */
  enqueue(
    channelId: string,
    content: string,
    type: MessageType = 'text',
    options?: { replyTo?: string; fileId?: string }
  ): string {
    const clientId = generateClientId();

    const msg: OutboundMessage = {
      clientId,
      channelId,
      content,
      type,
      replyTo: options?.replyTo,
      fileId: options?.fileId,
      queuedAt: new Date().toISOString(),
      attempts: 0,
      status: 'queued',
    };

    this.queue.set(clientId, msg);
    this._persist();
    this._emitQueueState();
    this._scheduleProcess(0);

    return clientId;
  }

  /**
   * Process the queue — send pending messages in FIFO order.
   */
  private async _processQueue(): Promise<void> {
    if (this._processing || this._destroyed) return;

    // Pre-check: don't start processing if socket is down
    if (!socketManager.isConnected()) {
      this._scheduleProcess(BASE_BACKOFF_MS);
      return;
    }

    this._processing = true;

    try {
      const pending = Array.from(this.queue.values())
        .filter((m) => m.status === 'queued')
        .sort((a, b) => a.queuedAt.localeCompare(b.queuedAt));

      for (const msg of pending) {
        if (this._destroyed) break;
        if (!socketManager.isConnected()) {
          // Socket disconnected mid-processing — reschedule remaining
          this._scheduleProcess(BASE_BACKOFF_MS);
          break;
        }

        await this._sendMessage(msg);
      }
    } finally {
      this._processing = false;
      // Check if new messages were enqueued while processing
      if (this._needsReprocess) {
        this._needsReprocess = false;
        this._scheduleProcess(0);
      }
    }
  }

  private async _sendMessage(msg: OutboundMessage): Promise<void> {
    msg.status = 'sending';
    msg.attempts += 1;
    this._emitQueueState();

    try {
      const response = await socketManager.emit('v2_chat_send_message', {
        channel_id: msg.channelId,
        content: msg.content,
        type: msg.type,
        reply_to: msg.replyTo,
        file_id: msg.fileId,
        client_id: msg.clientId,
      });

      if (response?.error) {
        throw new Error(response.error);
      }

      // Success — server ACKed
      msg.status = 'sent';
      msg.serverId = response.message_id;

      this.callbacks.onSent(
        msg.clientId,
        response.message_id,
        response.created_at
      );

      // Remove from queue after successful send
      this.queue.delete(msg.clientId);
      this._persist();
      this._emitQueueState();
    } catch (e: any) {
      msg.lastError = e.message || 'Send failed';

      // Network errors (socket disconnected, fetch TypeError, DNS, reset)
      // should not consume the retry budget — the user's connection may
      // return minutes or hours later and we want the message to land the
      // moment it does. Only *server-side* failures (validation errors,
      // permission denied, etc.) count against MAX_RETRIES.
      const isTransientNet =
        !socketManager.isConnected() ||
        _isNetworkLikeError(e) ||
        (typeof navigator !== 'undefined' && navigator.onLine === false);

      if (isTransientNet) {
        msg.status = 'queued';
        msg.attempts = Math.max(0, msg.attempts - 1);  // don't burn budget
        // Cap the wait at MAX_BACKOFF — socketManager reconnect will also
        // trigger a flush() which unblocks us sooner.
        this._scheduleProcess(MAX_BACKOFF_MS);
        this._persist();
        this._emitQueueState();
        return;
      }

      if (msg.attempts >= MAX_RETRIES) {
        msg.status = 'failed';
        this.callbacks.onFailed(msg.clientId, msg.lastError!);
        // Keep the message in the queue so retry() can find it
        this._persist();
        this._emitQueueState();
      } else {
        // Retry with backoff
        msg.status = 'queued';
        const backoff = Math.min(
          BASE_BACKOFF_MS * Math.pow(2, msg.attempts - 1),
          MAX_BACKOFF_MS
        );
        this._scheduleProcess(backoff);
        this._persist();
        this._emitQueueState();
      }
    }
  }

  /**
   * Retry a failed message.
   */
  retry(clientId: string): void {
    const msg = this.queue.get(clientId);
    if (msg && msg.status === 'failed') {
      msg.status = 'queued';
      msg.attempts = 0;
      this._persist();
      this._scheduleProcess(0);
    }
  }

  /**
   * Remove a message from the queue (cancel send).
   */
  cancel(clientId: string): void {
    this.queue.delete(clientId);
    this._persist();
    this._emitQueueState();
  }

  /**
   * Flush all queued messages (retry everything immediately).
   * Called after reconnection.
   */
  flush(): void {
    for (const msg of this.queue.values()) {
      if (msg.status === 'failed') {
        msg.status = 'queued';
        msg.attempts = 0;
      }
    }
    this._persist();
    this._scheduleProcess(0);
  }

  private _scheduleProcess(delayMs: number): void {
    if (this._processTimer) {
      clearTimeout(this._processTimer);
    }
    this._processTimer = setTimeout(() => {
      this._processTimer = null;
      // If already processing, mark for reprocessing instead of queuing another call
      if (this._processing) {
        this._needsReprocess = true;
      } else {
        this._processQueue();
      }
    }, delayMs);
  }

  private _emitQueueState(): void {
    let pending = 0;
    let sending = 0;
    for (const msg of this.queue.values()) {
      if (msg.status === 'queued') pending++;
      if (msg.status === 'sending') sending++;
    }
    this.callbacks.onQueueStateChange(pending, sending);
  }

  destroy(): void {
    this._destroyed = true;
    if (this._processTimer) {
      clearTimeout(this._processTimer);
      this._processTimer = null;
    }
    this.queue.clear();
  }
}
