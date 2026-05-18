/**
 * EventEnvelope — TypeScript mirror of the server-side Pydantic
 * Envelope schema (app/services/event_envelope.py).
 *
 * Why
 * ---
 * The server now wraps every cross-server event in a uniform envelope
 * with traceId, idempotencyKey, hopIndex, etc. The client doesn't
 * (yet) construct envelopes itself — we pass through whatever shape
 * Socket.IO delivers. But we DO want to:
 *
 *   1. Read trace_id off incoming events when present so the dev
 *      console can correlate UI actions with server-side route
 *      traces (TraceReporter).
 *   2. Generate idempotencyKeys for client-initiated commands so
 *      retries on flaky networks don't double-execute.
 *   3. Be ready when the client opts into producing envelopes
 *      directly (Phase 2 — once the server's emit handlers all
 *      accept envelope-shaped payloads as input too).
 *
 * Hard guards mirrored from the server schema
 * -------------------------------------------
 *   * payload size <= 8 KB (control plane only)
 *   * plane = "data" forbidden
 *   * P0 requires_ack = true
 */

export type Priority = 'P0' | 'P1' | 'P2' | 'P3' | 'P4';

export interface Envelope {
  // ── Identity ────────────────────────────────────────────────
  event_id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  idempotency_key: string;

  // ── Type & Priority ────────────────────────────────────────
  event_type: string;
  command_type: string | null;
  priority: Priority;
  plane: 'control';

  // ── Routing ────────────────────────────────────────────────
  source_user_id: string | null;
  destination_user_id: string | null;
  source_server_id: string;
  destination_server_id: string | null;
  current_server_id: string;
  next_server_id: string | null;
  route_id: string | null;
  route_version: number;
  hop_index: number;
  max_hops: number;

  // ── Domain context ─────────────────────────────────────────
  call_id: string | null;
  channel_id: string | null;

  // ── Reliability ────────────────────────────────────────────
  ttl_ms: number;
  sequence: number | null;
  requires_ack: boolean;
  retry_count: number;
  max_retries: number;

  // ── Lifecycle ──────────────────────────────────────────────
  created_at: string;   // ISO-8601
  expires_at: string;   // ISO-8601

  // ── Payload ────────────────────────────────────────────────
  payload: Record<string, unknown>;
}

const MAX_PAYLOAD_BYTES = 8 * 1024;
const MAX_HOPS_DEFAULT = 8;

const PRIORITY_DEFAULT_TTL_MS: Record<Priority, number> = {
  P0: 5_000,
  P1: 30_000,
  P2: 60_000,
  P3: 2_000,
  P4: 10_000,
};

/**
 * Generate a ULID-style ID. Sortable by creation time, 26 chars
 * after the prefix. Uses crypto.getRandomValues so it's safe to
 * collide with server-generated IDs (different prefix space).
 */
function genId(prefix: string): string {
  const tsMs = Date.now() & ((1 << 30) - 1); // 30 bits is plenty
  const tsB32 = tsMs.toString(32).padStart(7, '0');
  const randBytes = new Uint8Array(10);
  crypto.getRandomValues(randBytes);
  let randB32 = '';
  for (let i = 0; i < randBytes.length; i++) {
    randB32 += randBytes[i].toString(32).padStart(2, '0');
  }
  return `${prefix}_${tsB32}${randB32}`.slice(0, prefix.length + 1 + 26);
}

export class PayloadTooLargeError extends Error {
  constructor(size: number) {
    super(
      `payload size ${size} exceeds ${MAX_PAYLOAD_BYTES} bytes — ` +
        `large payloads must travel via S3 (files) or SFU (media)`,
    );
    this.name = 'PayloadTooLargeError';
  }
}

export interface NewEnvelopeArgs {
  event_type: string;
  priority: Priority;
  source_server_id: string;
  payload?: Record<string, unknown>;
  source_user_id?: string;
  destination_user_id?: string;
  destination_server_id?: string;
  call_id?: string;
  channel_id?: string;
  idempotency_key?: string;
  ttl_ms?: number;
  max_hops?: number;
  sequence?: number;
  requires_ack?: boolean;
  max_retries?: number;
  command_type?: string;
  trace_id?: string;
  parent_span_id?: string;
}

/**
 * Construct a fresh envelope. Mirrors `Envelope.new()` on the server.
 * Throws `PayloadTooLargeError` if the resulting JSON exceeds 8 KB.
 */
export function newEnvelope(args: NewEnvelopeArgs): Envelope {
  const now = new Date();
  const ttl = args.ttl_ms ?? PRIORITY_DEFAULT_TTL_MS[args.priority] ?? 5_000;
  const requiresAck = args.priority === 'P0' ? true : args.requires_ack ?? false;

  const env: Envelope = {
    event_id: genId('evt'),
    trace_id: args.trace_id ?? genId('trace'),
    span_id: genId('span'),
    parent_span_id: args.parent_span_id ?? null,
    idempotency_key: args.idempotency_key ?? genId('idem'),
    event_type: args.event_type,
    command_type: args.command_type ?? null,
    priority: args.priority,
    plane: 'control',
    source_user_id: args.source_user_id ?? null,
    destination_user_id: args.destination_user_id ?? null,
    source_server_id: args.source_server_id,
    destination_server_id: args.destination_server_id ?? null,
    current_server_id: args.source_server_id,
    next_server_id: null,
    route_id: null,
    route_version: 1,
    hop_index: 0,
    max_hops: args.max_hops ?? MAX_HOPS_DEFAULT,
    call_id: args.call_id ?? null,
    channel_id: args.channel_id ?? null,
    ttl_ms: ttl,
    sequence: args.sequence ?? null,
    requires_ack: requiresAck,
    retry_count: 0,
    max_retries: args.max_retries ?? 3,
    created_at: now.toISOString(),
    expires_at: new Date(now.getTime() + ttl).toISOString(),
    payload: args.payload ?? {},
  };

  const size = JSON.stringify(env).length;
  if (size > MAX_PAYLOAD_BYTES) {
    throw new PayloadTooLargeError(size);
  }
  return env;
}

export function isExpired(env: Envelope, now: Date = new Date()): boolean {
  return now.getTime() >= new Date(env.expires_at).getTime();
}

/**
 * Look at any incoming socket payload and return the trace_id /
 * event_id if the server tagged it. Older non-fabric events have
 * neither field — caller treats null as "untraced legacy event".
 */
export function readTraceMeta(
  data: unknown,
): { trace_id: string; event_id: string } | null {
  if (data === null || typeof data !== 'object') return null;
  const obj = data as Record<string, unknown>;
  const tid = obj.trace_id;
  const eid = obj.event_id;
  if (typeof tid === 'string' && typeof eid === 'string') {
    return { trace_id: tid, event_id: eid };
  }
  return null;
}

/**
 * Construct an idempotency key for a client-initiated command. Used
 * by handlers that want safe retry semantics on flaky networks.
 *
 * Format: `{action}:{stable_input_hash}` — stable across retries of
 * the same logical action so the server's idempotency cache returns
 * the cached result instead of double-executing.
 */
export function clientIdempotencyKey(action: string, stableInput: string): string {
  return `${action}:${stableInput}`;
}
