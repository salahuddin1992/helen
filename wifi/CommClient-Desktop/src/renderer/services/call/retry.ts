/**
 * Retry helpers for the call subsystem.
 *
 * `withTimeout` wraps any promise so a hung operation doesn't lock the UI.
 * `retryWithBackoff` retries failed operations with the schedule mandated
 * by the call-reliability spec: 1s → 2s → 4s → 8s, max 5 attempts.
 */

import { callErrorLog } from './CallErrorLog';

export class TimeoutError extends Error {
    constructor(public readonly operation: string, public readonly ms: number) {
        super(`${operation} timed out after ${ms}ms`);
        this.name = 'TimeoutError';
    }
}

/**
 * Race a promise against a timer. If the timer wins, rejects with
 * `TimeoutError`. Cancellation of the underlying op is the caller's
 * responsibility — JS can't abort an arbitrary Promise.
 */
export function withTimeout<T>(
    op: () => Promise<T>,
    ms: number,
    operationName: string,
): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            const err = new TimeoutError(operationName, ms);
            callErrorLog.error('Timeout', `${operationName} timed out`, err);
            reject(err);
        }, ms);

        op().then(
            (value) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            },
            (err) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(err);
            },
        );
    });
}

export interface RetryOptions {
    /** Human label for logs. */
    operationName: string;
    /** Max attempts. The spec asks for 3–5; default 5. */
    maxRetries?: number;
    /** Per-attempt timeout in ms. Default 10s. */
    timeoutMs?: number;
    /** Base delay for exponential backoff (1s → 2s → 4s → 8s). */
    baseDelayMs?: number;
    /** Hook fired after each attempt with attempt number + error. */
    onAttempt?: (attempt: number, error: unknown | null) => void;
    /** Optional predicate — return false to stop retrying (e.g. permission denied). */
    isRetryable?: (error: unknown) => boolean;
}

/**
 * Run `op` up to `maxRetries` times with exponential backoff between
 * attempts. Each attempt is wrapped in `withTimeout` so a hang on any
 * single try counts as that try's failure (and triggers a backoff).
 */
export async function retryWithBackoff<T>(
    op: () => Promise<T>,
    opts: RetryOptions,
): Promise<T> {
    const {
        operationName,
        maxRetries  = 5,
        timeoutMs   = 10_000,
        baseDelayMs = 1_000,
        onAttempt,
        isRetryable = () => true,
    } = opts;

    let lastError: unknown = null;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        callErrorLog.info('Retry', `${operationName} attempt ${attempt}/${maxRetries}`);
        try {
            const result = await withTimeout(op, timeoutMs, operationName);
            onAttempt?.(attempt, null);
            return result;
        } catch (err) {
            lastError = err;
            onAttempt?.(attempt, err);
            callErrorLog.warn(
                'Retry',
                `${operationName} failed attempt ${attempt}/${maxRetries}`,
                err,
            );

            if (!isRetryable(err)) {
                callErrorLog.error('Retry', `${operationName} non-retryable; giving up`, err);
                break;
            }
            if (attempt === maxRetries) break;

            // Exponential: 1, 2, 4, 8, 16 (capped at 16)
            const delay = baseDelayMs * Math.min(2 ** (attempt - 1), 16);
            await sleep(delay);
        }
    }

    callErrorLog.error(
        'Retry',
        `${operationName} failed after ${maxRetries} retries`,
        lastError,
    );
    throw lastError ?? new Error(`${operationName} failed`);
}

function sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
}
