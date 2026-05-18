/**
 * CallController — thin, robust wrapper around the existing CallEngine.
 *
 * Why a wrapper?
 *   • The engine already does the heavy lifting (signaling, RTC, FSM).
 *   • The UI was awaiting `engine.initiateCall` before navigating, with
 *     no timeouts, no retries, no concurrency guards, and no error
 *     surfacing — so any slow operation (`getUserMedia`, signaling)
 *     blocked the main thread visually.
 *
 * This controller adds:
 *   1. A simple, observable lifecycle state (idle → preparing →
 *      requestingPermissions → connecting → connected → reconnecting →
 *      failed → ended) that the UI subscribes to.
 *   2. Locks (`isStartingCall`, `isEndingCall`) so a spammed button can't
 *      kick off concurrent flows.
 *   3. `retryWithBackoff` + `withTimeout` around every async step.
 *   4. A non-blocking call-start: `start()` returns immediately and the
 *      flow runs in the background, so the UI can navigate to the call
 *      view instantly and watch state transitions update.
 *   5. Survives navigation. Created once at app boot, never disposed by
 *      view mounts/unmounts. Only `endCall()` (or terminal failure)
 *      releases resources.
 */

import { callErrorLog } from './CallErrorLog';
import { retryWithBackoff, withTimeout, TimeoutError } from './retry';
import { useCallStore } from '../../stores/call.store.v2';

export type CallLifecycleState =
    | 'idle'
    | 'preparing'
    | 'requestingPermissions'
    | 'connecting'
    | 'connected'
    | 'reconnecting'
    | 'failed'
    | 'ended';

export interface CallStartRequest {
    targetUserId?: string;     // for DM
    channelId?: string;        // for group
    media: 'audio' | 'video';
}

export interface CallControllerSnapshot {
    state: CallLifecycleState;
    callId: string | null;
    retryCount: number;
    lastError: string | null;
    isStartingCall: boolean;
    isEndingCall: boolean;
    isRetrying: boolean;
}

type Listener = (s: CallControllerSnapshot) => void;

// Audit fix #8a: rtcOfferAnswer was 10s — too tight on WAN with TURN
// relay or cross-server federation hops. The downstream
// _waitForActive() also had a 25s ceiling that fired before slow
// federation paths could promote the call to active. Bumped both to
// account for: TURN (1-2s), federation RPC forward (1-3s), client-
// side getUserMedia prompt latency (1-5s), and SDP renegotiation on
// topology switch (2-3s). Conservative ceilings prevent the
// "phantom failed call that's actually still connecting" UX bug.
const TIMEOUTS = {
    permissions:  8_000,
    socketConnect: 10_000,
    rtcOfferAnswer: 15_000,
    iceGathering: 20_000,
    mediaInit: 10_000,
};

class _CallController {
    private _state: CallLifecycleState = 'idle';
    private _callId: string | null = null;
    private _retryCount = 0;
    private _lastError: string | null = null;
    private _isStartingCall = false;
    private _isEndingCall = false;
    private _isRetrying = false;
    private _listeners = new Set<Listener>();

    // ── Public observable state ─────────────────────────────────────

    get snapshot(): CallControllerSnapshot {
        return {
            state: this._state,
            callId: this._callId,
            retryCount: this._retryCount,
            lastError: this._lastError,
            isStartingCall: this._isStartingCall,
            isEndingCall: this._isEndingCall,
            isRetrying: this._isRetrying,
        };
    }

    subscribe(listener: Listener): () => void {
        this._listeners.add(listener);
        return () => this._listeners.delete(listener);
    }

    // ── Public API: non-blocking ────────────────────────────────────

    /**
     * Fire and forget. Returns immediately; the UI navigates to the call
     * view at once and watches state transitions update.
     *
     * If the user spams the button, second+ calls are no-ops while the
     * first is in flight.
     */
    start(request: CallStartRequest): void {
        if (this._isStartingCall) {
            callErrorLog.warn('CallController', 'start() ignored — already starting');
            return;
        }
        if (this._state === 'connected' || this._state === 'connecting') {
            callErrorLog.warn(
                'CallController',
                `start() ignored — call already in ${this._state}`,
            );
            return;
        }

        this._isStartingCall = true;
        this._retryCount = 0;
        this._lastError = null;
        this._emit();

        // Detached promise — never awaited by the caller, so the UI thread
        // is free to update immediately.
        this._runStart(request).finally(() => {
            this._isStartingCall = false;
            this._emit();
        });
    }

    /**
     * End the active call. Idempotent — safe to call from cleanup paths.
     */
    endCall(reason: string = 'user'): void {
        if (this._isEndingCall) return;
        if (this._state === 'idle' || this._state === 'ended') return;

        this._isEndingCall = true;
        this._emit();

        try {
            callErrorLog.info('CallController', `endCall(${reason})`);
            useCallStore.getState().hangup();
            this._transition('ended');
        } catch (err) {
            callErrorLog.error('CallController', 'endCall failed', err);
        } finally {
            this._isEndingCall = false;
            this._callId = null;
            this._retryCount = 0;
            // Hand off to the native side: stop the Android foreground
            // service that was keeping the WebView alive across backgrounding.
            // No-op on desktop / web.
            try { window.electronAPI?.call?.stopActive?.(); } catch { /* ignore */ }
            this._emit();
        }
    }

    /** Force a retry (DebugPanel button). */
    forceRetry(request: CallStartRequest): void {
        callErrorLog.info('CallController', 'forceRetry requested');
        this._isRetrying = true;
        this._emit();
        this.start(request);
    }

    /** Reset to idle from a failed state (DebugPanel button). */
    reset(): void {
        callErrorLog.info('CallController', 'reset to idle');
        this._state = 'idle';
        this._callId = null;
        this._retryCount = 0;
        this._lastError = null;
        this._isStartingCall = false;
        this._isEndingCall = false;
        this._isRetrying = false;
        this._emit();
    }

    // ── Internals ───────────────────────────────────────────────────

    private async _runStart(request: CallStartRequest) {
        const onError = (where: string, err: unknown) => {
            const msg = err instanceof Error ? err.message : String(err);
            this._lastError = `${where}: ${msg}`;
            callErrorLog.error('CallController', this._lastError, err);
        };

        try {
            this._transition('preparing');

            // 1. Permissions — bounded so a stuck OS prompt can't hang us.
            this._transition('requestingPermissions');
            await retryWithBackoff(
                () =>
                    withTimeout(
                        () =>
                            navigator.mediaDevices.getUserMedia({
                                audio: true,
                                video: request.media === 'video',
                            }).then((s) => {
                                // Release immediately — engine acquires its own.
                                s.getTracks().forEach((t) => t.stop());
                            }),
                        TIMEOUTS.permissions,
                        'getUserMedia (probe)',
                    ),
                {
                    operationName: 'permission probe',
                    maxRetries: 2,
                    timeoutMs: TIMEOUTS.permissions,
                    isRetryable: (e) =>
                        // Don't retry hard NotAllowed / NotFound — user has to fix.
                        !(e instanceof DOMException &&
                          ['NotAllowedError', 'NotFoundError', 'SecurityError']
                              .includes(e.name)),
                    onAttempt: (n) => { this._retryCount = n; this._emit(); },
                },
            );

            // 2. Real call init via the existing engine. Wrap once in
            //    retry+timeout so the UI never deadlocks.
            this._transition('connecting');
            await retryWithBackoff(
                () =>
                    request.targetUserId
                        ? useCallStore.getState().initiateCall(
                              request.targetUserId,
                              request.media,
                          )
                        : useCallStore.getState().initiateGroupCall(
                              request.channelId!,
                              request.media,
                          ),
                {
                    operationName: 'engine.initiateCall',
                    maxRetries: 5,
                    timeoutMs: TIMEOUTS.rtcOfferAnswer,
                    onAttempt: (n) => { this._retryCount = n; this._emit(); },
                },
            );

            // 3. Wait until the engine reports `active` (or a terminal state).
            await this._waitForActive();
            this._transition('connected');

            // Hand off to the native foreground service so Android doesn't
            // kill the WebView when the user backgrounds the app. Best-
            // effort — failure here doesn't fail the call. No-op on desktop.
            try {
                const cur = useCallStore.getState();
                await window.electronAPI?.call?.startActive?.({
                    channelId: request.channelId ?? request.targetUserId ?? cur.callId ?? 'unknown',
                    peerName:  request.targetUserId ?? 'Helen',
                    isVideo:   request.media === 'video',
                });
            } catch (e) {
                callErrorLog.warn('CallController', 'startActive native bridge failed', e);
            }
        } catch (err) {
            onError('start', err);
            this._transition('failed');
            // Best-effort cleanup so the engine doesn't sit in half-init.
            try { useCallStore.getState().hangup(); } catch { /* ignore */ }
        } finally {
            this._isRetrying = false;
            this._emit();
        }
    }

    /**
     * Subscribe to the existing call store and resolve when status hits
     * `active` (success), `idle` (rejected), or `ended`. Bounded by a
     * 25-second total deadline so we don't wait forever.
     */
    private _waitForActive(): Promise<void> {
        return withTimeout(
            () =>
                new Promise<void>((resolve, reject) => {
                    const off = useCallStore.subscribe((s) => {
                        if (s.status === 'active') {
                            this._callId = s.callId;
                            off();
                            resolve();
                        } else if (s.status === 'ended') {
                            off();
                            reject(new Error('Call ended before connecting'));
                        } else if (s.status === 'idle' && this._callId) {
                            off();
                            reject(new Error('Call returned to idle unexpectedly'));
                        }
                    });
                    // Initial check — store may already be active.
                    const cur = useCallStore.getState();
                    if (cur.status === 'active') {
                        this._callId = cur.callId;
                        off();
                        resolve();
                    }
                }),
            // 35s (was 25): give cross-server federation paths time to
            // route accept → call_accepted → active. The previous 25s
            // ceiling fired before bob.accept's federation RPC could
            // round-trip on a slow WAN.
            35_000,
            'wait-for-active',
        );
    }

    private _transition(next: CallLifecycleState) {
        if (this._state === next) return;
        callErrorLog.info(
            'CallController',
            `CALL STATE: ${this._state} -> ${next}`,
        );
        this._state = next;
        this._emit();
    }

    private _emit() {
        const snap = this.snapshot;
        // Defer to a microtask so a state change triggered *during* a
        // React render (e.g. an `_emit()` fired synchronously in
        // response to a setState upstream) doesn't cause "Cannot update
        // a component while rendering a different component" warnings.
        queueMicrotask(() => {
            for (const l of this._listeners) {
                try { l(snap); } catch { /* listener errors are not our problem */ }
            }
        });
    }
}

export const callController = new _CallController();

// Expose for the DebugCallPanel and devtools.
declare global {
    interface Window {
        __callController?: _CallController;
    }
}
if (typeof window !== 'undefined') {
    window.__callController = callController;
}

export { TimeoutError };
