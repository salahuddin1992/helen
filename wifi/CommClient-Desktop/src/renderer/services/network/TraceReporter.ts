/**
 * TraceReporter — captures trace_id / event_id from incoming socket
 * events and surfaces them in the dev console plus an in-memory ring
 * buffer that DevTools / debug panels can read.
 *
 * Why
 * ---
 * The server-side trace_collector reconstructs the full hop chain
 * for any envelope that traversed the broker fabric. To debug a
 * specific UI action (a missed call, a delayed message, a stale ICE
 * candidate), an operator wants to ask: "what was the trace_id for
 * THAT event?" — and then plug it into `/api/chaos/traces/{tid}`.
 *
 * This module sits at the receiving edge of the socket layer:
 *
 *   socketManager.on('call_incoming', (data) => {
 *     traceReporter.observe('call_incoming', data);
 *     // …existing handler runs unchanged
 *   });
 *
 * Behavior
 * --------
 * * Untraced events (no trace_id field) are ignored — zero overhead.
 * * Traced events are recorded in a 256-entry ring buffer with
 *   timestamps + the originating event_type. Latest first.
 * * Dev mode (`import.meta.env.DEV`) prints a one-line summary to the
 *   console so operators can copy-paste trace_ids from Chrome DevTools.
 * * Optional `getRecent()` exposes the buffer for in-app debug
 *   panels.
 */

import { readTraceMeta } from './EventEnvelope';

interface TraceRecord {
  trace_id: string;
  event_id: string;
  event_type: string;
  observed_at: number;  // epoch ms
  direction: 'incoming' | 'outgoing';
}

const RING_SIZE = 256;

class TraceReporter {
  private _ring: TraceRecord[] = [];
  private _byTrace = new Map<string, TraceRecord>();

  observe(
    eventType: string,
    payload: unknown,
    direction: 'incoming' | 'outgoing' = 'incoming',
  ): void {
    const meta = readTraceMeta(payload);
    if (meta === null) return;
    const rec: TraceRecord = {
      trace_id: meta.trace_id,
      event_id: meta.event_id,
      event_type: eventType,
      observed_at: Date.now(),
      direction,
    };
    this._ring.unshift(rec);
    if (this._ring.length > RING_SIZE) {
      this._ring.length = RING_SIZE;
    }
    this._byTrace.set(meta.trace_id, rec);

    // Dev console — concise enough to scan but greppable.
    if (this._isDev()) {
      console.log(
        `[trace] ${direction} ${eventType} trace=${meta.trace_id} event=${meta.event_id}`,
      );
    }
  }

  /** Return the latest N observed traces, newest first. */
  getRecent(limit = 50): TraceRecord[] {
    return this._ring.slice(0, limit);
  }

  /** Look up a previously-observed trace by id. */
  get(traceId: string): TraceRecord | undefined {
    return this._byTrace.get(traceId);
  }

  /** Number of observed traces in the ring. */
  size(): number {
    return this._ring.length;
  }

  /** Drop all observed traces (useful when a user signs out). */
  clear(): void {
    this._ring.length = 0;
    this._byTrace.clear();
  }

  private _isDev(): boolean {
    try {
      const meta = (import.meta as unknown) as { env?: { DEV?: boolean } };
      return meta?.env?.DEV === true;
    } catch {
      return false;
    }
  }
}

export const traceReporter = new TraceReporter();

/**
 * Install passive trace observation on a socketManager-like object.
 * The integration is intentionally light-weight — we monkey-patch
 * the `on` method to record every payload that flows past, but we
 * don't intercept anything. Callers' handlers run unchanged.
 *
 * Disable in test environments by guarding with `if (!isTest)`.
 */
export function installTraceObserver(socketManager: {
  on: (event: string, handler: (data: unknown) => void) => () => void;
}): void {
  const origOn = socketManager.on.bind(socketManager);
  // Wrap each subscription so we observe before the user's handler runs.
  socketManager.on = (event: string, handler: (data: unknown) => void) => {
    return origOn(event, (data: unknown) => {
      try {
        traceReporter.observe(event, data, 'incoming');
      } catch {
        // never let a tracing failure break the actual handler
      }
      handler(data);
    });
  };
}
