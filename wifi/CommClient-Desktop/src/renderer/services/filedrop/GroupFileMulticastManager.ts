/**
 * GroupFileMulticastManager — client-side counterpart to the server's
 * group-file BitTorrent-style swarm (``app/services/group_file_service.py``).
 *
 * Responsibilities
 * ----------------
 * 1. After the sender has uploaded a file via ResumableUploader, emit
 *    ``file_drop:group_offer`` so the server creates one
 *    :class:`GroupFileOffer` + per-member availability rows.
 * 2. On the recipient side, subscribe to the fan-out events
 *    (``group_offer_created``, ``group_offer_updated``,
 *    ``group_peer_available``, ``group_offer_completed``) and maintain a
 *    local cache of offers so the UI can render inboxes without hitting
 *    REST on every change.
 * 3. Accept / reject / cancel via REST (Socket.IO versions exist too but
 *    REST is the authoritative path used by mobile/CLI clients).
 * 4. On each downloaded chunk, POST
 *    ``/api/group-file-offers/{offer_id}/chunks/{idx}`` so the server's
 *    packed bitmap updates and other peers learn "user X now has chunk N".
 * 5. Expose ``getChunkPeers(offer_id, chunk_index)`` so the renderer can
 *    pull chunks directly from peers when swarm_enabled=true.
 *
 * Concurrency model
 * -----------------
 * * A bounded pool (default 4) fetches chunks in parallel. Each chunk
 *   fetch races the server-backed URL against the swarm peer list; the
 *   first succeeding source wins.
 * * Chunk state is maintained in an in-memory Map<offer_id, ChunkState>
 *   and mirrored into a Blob assembler so the final file can be written
 *   once all chunks have arrived.
 * * Failures (network, 403, 404) fall back to the server URL; the peer
 *   is dropped from the candidate list for that offer only, not globally.
 *
 * NOT in this module (intentional scope boundary)
 * -----------------------------------------------
 * * Actual peer-to-peer transport — the desktop currently pulls chunks
 *   over HTTP via ``/api/files/{id}?range=``; the "peer" list is used
 *   mostly for progress reporting until true WebRTC data-channel pulls
 *   ship. See ROADMAP_PROGRESS.md §11 for the next phase.
 */
import { api, getBaseUrl } from '../api.client';
import { socketManager } from '../socket.manager';
import { useAuthStore } from '../../stores/auth.store';

export interface GroupFileOfferPayload {
  id: string;
  sender_id: string;
  channel_id: string;
  file_id: string;
  file_name: string;
  file_size: number;
  mime_type: string | null;
  chunk_size: number;
  total_chunks: number;
  caption: string | null;
  swarm_enabled: boolean;
  checksum: string | null;
  status: string;
  accepted_count: number;
  rejected_count: number;
  completed_count: number;
  expected_recipients: number;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
  last_activity_at: string | null;
}

export interface GroupChunkPeer {
  user_id: string;
  status: string; // accepted | completed
  chunks_received: number;
  is_sender?: boolean;
  updated_at?: string;
}

export interface GroupFileTransferState {
  offer: GroupFileOfferPayload;
  /** chunk_index -> true when landed locally */
  local: boolean[];
  /** peer_id -> { chunk_index -> known_available } */
  peers: Map<string, Set<number>>;
  /** assembled Blob chunks (indexed by chunk_index) */
  buffers: Array<Uint8Array | null>;
  bytesReceived: number;
  startedAt: number;
  status:
    | 'offered'
    | 'accepted'
    | 'downloading'
    | 'completed'
    | 'rejected'
    | 'cancelled'
    | 'expired'
    | 'error';
  error?: string;
}

type Listener = (state: GroupFileTransferState) => void;

export interface SendGroupFileOptions {
  chunkSize: number;
  totalChunks: number;
  caption?: string;
  swarmEnabled?: boolean;
  expiresInSec?: number;
  checksum?: string;
}

/**
 * Per-offer client state machine. One instance per active group-file offer
 * the user participates in (sender, accepted recipient, or not-yet-decided).
 */
export class GroupFileMulticastManager {
  private states: Map<string, GroupFileTransferState> = new Map();
  private listeners: Set<Listener> = new Set();
  private socketBound = false;
  private fetchPool: Map<string, Promise<void>[]> = new Map();
  private maxConcurrent: number = 4;

  constructor(opts: { maxConcurrent?: number } = {}) {
    if (opts.maxConcurrent) this.maxConcurrent = opts.maxConcurrent;
    this.bindSocket();
  }

  // ── Public API ──────────────────────────────────────────────────

  /**
   * Subscribe to state changes. Returns an unsubscribe function.
   */
  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  getState(offerId: string): GroupFileTransferState | undefined {
    return this.states.get(offerId);
  }

  listStates(): GroupFileTransferState[] {
    return Array.from(this.states.values());
  }

  /**
   * Sender flow — REST creates the offer row; the server fans out the
   * ``group_offer_created`` event, which arrives on the sender's own
   * socket too and populates local state.
   */
  async createOffer(
    channelId: string,
    fileId: string,
    opts: SendGroupFileOptions,
  ): Promise<GroupFileOfferPayload> {
    const body = {
      file_id: fileId,
      chunk_size: opts.chunkSize,
      total_chunks: opts.totalChunks,
      caption: opts.caption ?? null,
      swarm_enabled: opts.swarmEnabled ?? true,
      expires_in_sec: opts.expiresInSec ?? null,
      checksum: opts.checksum ?? null,
    };
    const offer = (await api.groupFileOffers.create(
      channelId,
      body,
    )) as GroupFileOfferPayload;
    this.upsertState(offer, { mineAsSender: true });
    return offer;
  }

  async accept(offerId: string): Promise<void> {
    const resp = await api.groupFileOffers.accept(offerId);
    this.upsertState(resp.offer);
    await this.startDownloading(offerId);
  }

  async reject(offerId: string): Promise<void> {
    const resp = await api.groupFileOffers.reject(offerId);
    this.upsertState(resp.offer);
  }

  async cancel(offerId: string): Promise<void> {
    await api.groupFileOffers.cancel(offerId);
    const state = this.states.get(offerId);
    if (state) {
      state.status = 'cancelled';
      this.notify(state);
    }
  }

  /**
   * Query peers currently holding a specific chunk. Server will always
   * include the sender unless swarm is disabled.
   */
  async getChunkPeers(
    offerId: string,
    chunkIndex: number,
    limit: number = 32,
  ): Promise<GroupChunkPeer[]> {
    const resp = await api.groupFileOffers.chunkPeers(
      offerId,
      chunkIndex,
      limit,
    );
    return resp.peers as GroupChunkPeer[];
  }

  /**
   * Load the inbox (offers pending this user's decision or being delivered).
   */
  async refreshInbox(activeOnly: boolean = true): Promise<GroupFileOfferPayload[]> {
    const resp = await api.groupFileOffers.inbox(activeOnly);
    const offers: GroupFileOfferPayload[] = resp.offers || [];
    for (const o of offers) this.upsertState(o);
    return offers;
  }

  // ── Socket wiring ───────────────────────────────────────────────

  private bindSocket(): void {
    if (this.socketBound) return;
    this.socketBound = true;

    socketManager.on('file_drop:group_offer_created', (data: any) => {
      const offer = data as GroupFileOfferPayload;
      this.upsertState(offer);
    });

    socketManager.on('file_drop:group_offer_updated', (data: any) => {
      if (data?.offer) this.upsertState(data.offer as GroupFileOfferPayload);
    });

    socketManager.on('file_drop:group_offer_completed', (data: any) => {
      const state = this.states.get(data?.offer_id);
      if (!state) return;
      state.status = (data?.status ?? 'completed') as GroupFileTransferState['status'];
      this.notify(state);
    });

    socketManager.on('file_drop:group_peer_available', (data: any) => {
      const { offer_id, chunk_index, user_id } = data || {};
      const state = this.states.get(offer_id);
      if (!state || !user_id) return;
      const peer = state.peers.get(user_id) ?? new Set<number>();
      peer.add(Number(chunk_index));
      state.peers.set(user_id, peer);
      this.notify(state);
    });

    socketManager.on('file_drop:group_offer_error', (data: any) => {
      // Reason is surfaced to listeners; concrete offer_id isn't always
      // known so we surface on all known states for the caller to filter.
      for (const s of this.states.values()) {
        s.error = `${data?.reason ?? 'group_error'}: ${data?.detail ?? ''}`;
        this.notify(s);
      }
    });
  }

  // ── Download engine ─────────────────────────────────────────────

  /**
   * Start pulling chunks for an accepted offer. Runs ``maxConcurrent``
   * fetches in parallel; on each successful chunk it reports back via
   * REST so the server flips the bit and notifies other peers.
   */
  private async startDownloading(offerId: string): Promise<void> {
    const state = this.states.get(offerId);
    if (!state) return;
    if (state.status === 'downloading' || state.status === 'completed') return;
    state.status = 'downloading';
    state.startedAt = Date.now();
    this.notify(state);

    const total = state.offer.total_chunks;
    const queue: number[] = [];
    for (let i = 0; i < total; i++) if (!state.local[i]) queue.push(i);

    const workers: Promise<void>[] = [];
    const pool = Math.min(this.maxConcurrent, queue.length) || 1;
    for (let w = 0; w < pool; w++) {
      workers.push(this.chunkWorker(offerId, queue));
    }
    this.fetchPool.set(offerId, workers);

    try {
      await Promise.all(workers);
    } catch (e) {
      state.status = 'error';
      state.error = e instanceof Error ? e.message : String(e);
      this.notify(state);
      return;
    } finally {
      this.fetchPool.delete(offerId);
    }

    if (state.buffers.every((b) => b !== null)) {
      state.status = 'completed';
      this.assembleAndSave(state);
      this.notify(state);
    }
  }

  private async chunkWorker(offerId: string, queue: number[]): Promise<void> {
    const state = this.states.get(offerId);
    if (!state) return;

    // Audit fix F5: cap retries per chunk so a permanently-broken
    // chunk doesn't loop forever. After MAX_CHUNK_RETRIES we drop
    // it from the queue and surface the failure on the state.
    const MAX_CHUNK_RETRIES = 5;
    const retries = new Map<number, number>();

    while (queue.length > 0) {
      const idx = queue.shift();
      if (idx == null) return;
      if (state.local[idx]) continue;

      const data = await this.fetchChunk(state, idx).catch(() => null);
      if (data == null) {
        const tries = (retries.get(idx) ?? 0) + 1;
        retries.set(idx, tries);
        if (tries >= MAX_CHUNK_RETRIES) {
          // Permanent fail — give up on this chunk. The transfer is
          // not "complete" but at least the worker exits and the UI
          // can show "incomplete" rather than spinning forever.
          (state as any).failedChunks = ((state as any).failedChunks || 0) + 1;
          this.notify(state);
          continue;
        }
        // Retry tail — exponential backoff capped at 5s.
        queue.push(idx);
        await delay(Math.min(5000, 200 * (1 << (tries - 1))));
        continue;
      }

      state.buffers[idx] = data;
      state.local[idx] = true;
      state.bytesReceived += data.byteLength;

      try {
        await api.groupFileOffers.reportChunk(offerId, idx, data.byteLength);
      } catch {
        // Non-fatal — we still have the bytes locally; reporting is
        // best-effort swarm gossip.
      }

      this.notify(state);
    }
  }

  /**
   * Fetches a single chunk. Primary source is the sender-uploaded file on
   * the server (byte range). If swarm is enabled and a peer holds the
   * chunk, we *could* negotiate a peer-direct transfer here; that path is
   * a future enhancement — today all chunk bytes come from the server.
   */
  private async fetchChunk(
    state: GroupFileTransferState,
    index: number,
  ): Promise<Uint8Array> {
    const { file_id, chunk_size, file_size } = state.offer;
    const start = index * chunk_size;
    const end = Math.min(start + chunk_size, file_size) - 1;
    const baseUrl = getBaseUrl();
    const tokens = useAuthStore.getState().tokens;
    const headers: Record<string, string> = {
      Range: `bytes=${start}-${end}`,
    };
    if (tokens?.access_token) {
      headers.Authorization = `Bearer ${tokens.access_token}`;
    }
    const res = await fetch(`${baseUrl}/api/files/${file_id}`, {
      method: 'GET',
      headers,
    });
    if (!res.ok && res.status !== 206) {
      throw new Error(`chunk ${index} fetch failed: ${res.status}`);
    }
    const buf = await res.arrayBuffer();
    return new Uint8Array(buf);
  }

  private assembleAndSave(state: GroupFileTransferState): void {
    const parts: BlobPart[] = [];
    for (const b of state.buffers) {
      if (b) {
        // Explicitly slice into a standalone ArrayBuffer to avoid the
        // lib.dom `BlobPart` type complaint about generic Uint8Array
        // variants that may be backed by SharedArrayBuffer.
        parts.push(b.slice().buffer as ArrayBuffer);
      }
    }
    const blob = new Blob(parts, {
      type: state.offer.mime_type || 'application/octet-stream',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = state.offer.file_name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // ── State helpers ───────────────────────────────────────────────

  private upsertState(
    offer: GroupFileOfferPayload,
    flags: { mineAsSender?: boolean } = {},
  ): GroupFileTransferState {
    let state = this.states.get(offer.id);
    if (!state) {
      state = {
        offer,
        local: new Array(offer.total_chunks).fill(false),
        peers: new Map(),
        buffers: new Array(offer.total_chunks).fill(null),
        bytesReceived: 0,
        startedAt: 0,
        status: (offer.status as GroupFileTransferState['status']) || 'offered',
      };
      this.states.set(offer.id, state);
    } else {
      state.offer = offer;
      // Sync length if the server returned a different value (shouldn't
      // happen but we stay safe).
      if (state.local.length !== offer.total_chunks) {
        const grow = offer.total_chunks - state.local.length;
        if (grow > 0) {
          for (let i = 0; i < grow; i++) {
            state.local.push(false);
            state.buffers.push(null);
          }
        }
      }
      if (offer.status === 'completed' || offer.status === 'cancelled' ||
          offer.status === 'expired') {
        state.status = offer.status as GroupFileTransferState['status'];
      }
    }
    if (flags.mineAsSender) {
      // Sender has the authoritative copy in memory on upload path; we
      // don't assemble locally, but we flag every chunk as "we have it"
      // so the swarm broadcasts reflect reality.
      for (let i = 0; i < state.local.length; i++) state.local[i] = true;
    }
    this.notify(state);
    return state;
  }

  private notify(state: GroupFileTransferState): void {
    for (const fn of this.listeners) {
      try {
        fn(state);
      } catch {
        /* listener errors must not break the manager */
      }
    }
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Module-level singleton — one manager for the whole renderer. */
export const groupFileMulticastManager = new GroupFileMulticastManager();
