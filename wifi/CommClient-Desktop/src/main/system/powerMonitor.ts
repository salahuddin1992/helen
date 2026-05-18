/**
 * Power / session events — renderer reconnect driver.
 *
 * Listens for Electron's `powerMonitor` signals and forwards them to
 * the focused renderer so socket.io / mediasoup transports can
 * reconnect cleanly after sleep/resume, lock/unlock, and AC↔battery
 * transitions.
 *
 * Why the renderer cares:
 *   - `suspend` / `resume`: sockets may still look "open" after resume
 *     but the TCP state is dead. We proactively tear them down.
 *   - `lock-screen` / `unlock-screen`: used to pause/resume media capture
 *     (camera/mic) without dropping the SFU producer entirely.
 *   - `on-ac` / `on-battery`: media quality policy (higher bitrate on
 *     AC, lower on battery) decided by renderer logic.
 *
 * This module is side-effect free until `installPowerMonitor()` is
 * called. It must be called AFTER `app.whenReady()` because
 * `powerMonitor` is only reachable post-ready on Linux.
 */

import { BrowserWindow, powerMonitor, app, ipcMain } from 'electron';

export interface PowerEventPayload {
  type:
    | 'suspend'
    | 'resume'
    | 'lock-screen'
    | 'unlock-screen'
    | 'on-ac'
    | 'on-battery'
    | 'shutdown'
    | 'user-did-become-active'
    | 'user-did-resign-active';
  at: number; // epoch ms
  idleSeconds?: number; // only set for suspend/resume
  systemIdleTimeSec?: number;
}

export interface PowerMonitorOptions {
  getMainWindow: () => BrowserWindow | null;
  /** Minimum idle seconds between two suspend→resume signals before
   *  we issue a "long sleep" flag to the renderer. Default 30s. */
  longSleepThresholdSec?: number;
}

let getWin: () => BrowserWindow | null = () => null;
let lastSuspendAt: number | null = null;
let longSleepThreshold = 30;

function send(payload: PowerEventPayload): void {
  const w = getWin();
  if (!w) {
    console.log(`[powerMonitor] ${payload.type} — no window (dropped)`);
    return;
  }
  try {
    w.webContents.send('system:power', payload);
    console.log(`[powerMonitor] -> renderer: ${payload.type}`);
  } catch (err) {
    console.warn('[powerMonitor] send failed:', (err as Error).message);
  }
}

export function installPowerMonitor(opts: PowerMonitorOptions): void {
  getWin = opts.getMainWindow;
  longSleepThreshold = opts.longSleepThresholdSec ?? 30;

  // app.whenReady() guards required on Linux; safe elsewhere
  app.whenReady().then(() => {
    powerMonitor.on('suspend', () => {
      lastSuspendAt = Date.now();
      send({ type: 'suspend', at: lastSuspendAt });
    });

    powerMonitor.on('resume', () => {
      const now = Date.now();
      const idleSeconds = lastSuspendAt ? Math.round((now - lastSuspendAt) / 1000) : undefined;
      const isLong = idleSeconds !== undefined && idleSeconds >= longSleepThreshold;
      send({
        type: 'resume',
        at: now,
        idleSeconds,
        systemIdleTimeSec: powerMonitor.getSystemIdleTime(),
      });
      if (isLong) {
        console.log(`[powerMonitor] long sleep detected (${idleSeconds}s) — renderer should hard-reconnect`);
      }
      lastSuspendAt = null;
    });

    powerMonitor.on('lock-screen', () => send({ type: 'lock-screen', at: Date.now() }));
    powerMonitor.on('unlock-screen', () => send({ type: 'unlock-screen', at: Date.now() }));

    // AC / battery — emitted on Windows/macOS. Older Electron versions
    // gate this behind a feature check; we guard so it's not fatal.
    try {
      powerMonitor.on('on-ac', () => send({ type: 'on-ac', at: Date.now() }));
      powerMonitor.on('on-battery', () => send({ type: 'on-battery', at: Date.now() }));
    } catch {
      /* not available on this platform */
    }

    try {
      powerMonitor.on('shutdown' as any, () => send({ type: 'shutdown', at: Date.now() }));
    } catch {
      /* ignore */
    }

    // macOS foreground/background — harmless on Windows/Linux.
    try {
      powerMonitor.on('user-did-become-active' as any, () =>
        send({ type: 'user-did-become-active', at: Date.now() })
      );
      powerMonitor.on('user-did-resign-active' as any, () =>
        send({ type: 'user-did-resign-active', at: Date.now() })
      );
    } catch {
      /* ignore */
    }

    console.log('[powerMonitor] hooks installed');
  });

  // Renderer pull API — lets the client ask "am I on battery right now?"
  ipcMain.handle('system:power:state', () => {
    try {
      return {
        systemIdleTimeSec: powerMonitor.getSystemIdleTime(),
        systemIdleState: powerMonitor.getSystemIdleState(60),
        onBatteryPower: powerMonitor.isOnBatteryPower?.() ?? false,
      };
    } catch (err) {
      return { error: (err as Error).message };
    }
  });
}
