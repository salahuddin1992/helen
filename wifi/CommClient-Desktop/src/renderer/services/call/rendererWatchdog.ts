/**
 * Renderer watchdog — auto-recovery for the "blank window" failure mode.
 *
 * If the React tree fails to mount (or unmounts unexpectedly because of
 * an uncaught error that bypasses the ErrorBoundary), the window is
 * left painting a single background colour with no UI to interact with.
 *
 * The watchdog detects this state from the OS layer's perspective:
 *   • Document body has rendered for at least `STABILIZE_MS`.
 *   • The `#root` element exists but has zero height/zero text.
 *   • `window.onerror` has fired with an unrecoverable error since boot.
 *
 * When all signals agree, it reloads the renderer. To prevent a reload
 * loop on a real bug, it tracks reload attempts in `sessionStorage` and
 * stops after `MAX_RELOADS` — at which point the ErrorBoundary view
 * becomes the user-visible landing page.
 */

import { callErrorLog } from './CallErrorLog';

const STABILIZE_MS = 5_000;       // grace period for first render
const CHECK_INTERVAL_MS = 4_000;  // re-check cadence after stabilization
const MAX_RELOADS = 3;            // per session

const KEY_RELOAD_COUNT = 'helen.watchdog.reloadCount';
const KEY_LAST_RELOAD  = 'helen.watchdog.lastReloadAt';

let installed = false;
let bootedAt = 0;

export function installRendererWatchdog() {
    if (installed) return;
    installed = true;
    bootedAt = Date.now();

    // Reset the counter when the user successfully sees the UI for a while.
    setTimeout(() => {
        if (looksHealthy()) {
            sessionStorage.setItem(KEY_RELOAD_COUNT, '0');
            callErrorLog.info('Watchdog', 'Renderer healthy — reload counter reset');
        }
    }, 30_000);

    // Periodic check.
    setInterval(check, CHECK_INTERVAL_MS);

    callErrorLog.info('Watchdog', 'Renderer watchdog installed');
}

function check() {
    if (Date.now() - bootedAt < STABILIZE_MS) return;       // still booting
    if (looksHealthy()) return;

    const count = Number(sessionStorage.getItem(KEY_RELOAD_COUNT) || '0');
    if (count >= MAX_RELOADS) {
        callErrorLog.error(
            'Watchdog',
            `Renderer still blank after ${count} reload attempts — giving up`,
            'The ErrorBoundary view should now be visible. Check the log for the underlying cause.',
        );
        return;
    }

    const lastReload = Number(sessionStorage.getItem(KEY_LAST_RELOAD) || '0');
    if (Date.now() - lastReload < 8_000) return;            // already reloading

    sessionStorage.setItem(KEY_RELOAD_COUNT, String(count + 1));
    sessionStorage.setItem(KEY_LAST_RELOAD,  String(Date.now()));
    callErrorLog.warn(
        'Watchdog',
        `Renderer appears blank — reloading (attempt ${count + 1}/${MAX_RELOADS})`,
    );
    // Defer one tick so the log entry actually flushes.
    setTimeout(() => window.location.reload(), 50);
}

/**
 * Healthy if `#root` has measurable layout AND any rendered text.
 * This is a heuristic; it lets a legitimate empty splash render briefly
 * (the STABILIZE_MS grace window) but catches a permanently blank tree.
 */
function looksHealthy(): boolean {
    const root = document.getElementById('root');
    if (!root) return false;

    const rect = root.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return false;

    // Either some element rendered children, or text content exists.
    const text = (root.textContent || '').trim();
    if (text.length > 0) return true;

    // Some splash screens are pure SVG with no text — accept those too.
    if (root.querySelector('svg, canvas, img, video')) return true;

    return false;
}
