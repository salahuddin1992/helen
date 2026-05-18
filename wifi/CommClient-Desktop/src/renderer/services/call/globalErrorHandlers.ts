/**
 * Install global error handlers exactly once at app boot.
 *
 * Catches:
 *   • Uncaught synchronous errors (`window.onerror`)
 *   • Unhandled promise rejections (`unhandledrejection`)
 *
 * Every captured error is mirrored into `callErrorLog` so the
 * DebugCallPanel can render it alongside call-subsystem events. The
 * handlers do not swallow or `preventDefault()` — the original console
 * surface and DevTools red-line are preserved.
 */

import { callErrorLog } from './CallErrorLog';
import { callController } from './CallController';

let installed = false;

export function installGlobalErrorHandlers() {
    if (installed) return;
    installed = true;

    window.addEventListener('error', (event) => {
        callErrorLog.error(
            'window.onerror',
            (event.error instanceof Error ? event.error.message : event.message)
                || 'unknown error',
            {
                error: event.error,
                source: event.filename,
                line: event.lineno,
                column: event.colno,
                callState: callController.snapshot.state,
                callId: callController.snapshot.callId,
            },
        );
    });

    window.addEventListener('unhandledrejection', (event) => {
        callErrorLog.error(
            'unhandledrejection',
            event.reason instanceof Error
                ? event.reason.message
                : String(event.reason),
            {
                reason: event.reason,
                callState: callController.snapshot.state,
                callId: callController.snapshot.callId,
            },
        );
    });

    callErrorLog.info('Bootstrap', 'Global error handlers installed');
}
