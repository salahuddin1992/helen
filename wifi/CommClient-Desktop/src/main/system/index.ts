/**
 * System integration aggregator.
 *
 * Single entry-point wired into src/main/index.ts. Installs:
 *   - Single-instance lock      (singleInstance.ts)
 *   - Protocol / deep links      (protocolHandler.ts)
 *   - Windows auto-start         (autoStart.ts)
 *   - Native notifications       (notifications.ts)
 *   - Power monitor events       (powerMonitor.ts)
 *   - System tray                (tray.ts)
 *   - Firewall runtime gate      (firewall.ts)
 *
 * Usage in src/main/index.ts:
 *     import { installSystemIntegrations } from './system';
 *     ...
 *     installSystemIntegrations({ getMainWindow: () => mainWindow });
 *
 * The main window reference is provided via a getter so modules can
 * re-resolve it if the window is recreated (e.g. after close-to-tray).
 */

import { BrowserWindow, app, ipcMain } from 'electron';
import { installSingleInstanceLock } from './singleInstance';
import { installProtocolHandler } from './protocolHandler';
import { installAutoStart, launchedHidden } from './autoStart';
import { installNotifications } from './notifications';
import { installPowerMonitor } from './powerMonitor';
import { installTray } from './tray';
import { ensureFirewall, requestElevatedFirewallRepair } from './firewall';

export interface SystemIntegrationOptions {
  getMainWindow: () => BrowserWindow | null;
  /** Override default AUMID. */
  aumid?: string;
  /** Skip singleInstanceLock (caller handles it elsewhere). */
  skipSingleInstance?: boolean;
  /** Skip tray (caller already has a tray impl). */
  enableTray?: boolean;
  /** Skip auto-start setup on dev. */
  enableAutoStart?: boolean;
  /** Skip firewall probe on dev. */
  enableFirewall?: boolean;
  /** Hide-to-tray instead of quit on main-window close. */
  hideOnClose?: boolean;
}

export interface SystemIntegrationResult {
  locked: boolean;
  launchedHidden: boolean;
}

export function installSystemIntegrations(
  opts: SystemIntegrationOptions
): SystemIntegrationResult {
  // 1. Single-instance lock first — protects against double-launch from
  //    deep links, taskbar pins, and desktop shortcuts all at once.
  //    Skipped when the caller's entry file already owns the lock
  //    (e.g. legacy src/main/index.ts).
  let locked = true;
  if (!opts.skipSingleInstance) {
    locked = installSingleInstanceLock({
      getMainWindow: opts.getMainWindow,
    });
    if (!locked) {
      return { locked: false, launchedHidden: false };
    }
  }

  // 2. Protocol / deep-link handler registers scheme + listens for
  //    open-url / second-instance / argv on launch.
  installProtocolHandler({ getMainWindow: opts.getMainWindow });

  // 3. Notifications + AUMID — must be set before first toast.
  installNotifications({
    getMainWindow: opts.getMainWindow,
    aumid: opts.aumid,
  });

  // 4. Power events.
  installPowerMonitor({ getMainWindow: opts.getMainWindow });

  // 5. Auto-start (packaged builds only, unless COMMCLIENT_AUTOSTART_DEV=1).
  if (opts.enableAutoStart !== false) {
    installAutoStart({ defaultEnabled: true, bootArgs: ['--hidden'] });
  }

  // 6. Tray (after whenReady to avoid icon-before-display races).
  if (opts.enableTray !== false) {
    app.whenReady().then(() => {
      try {
        installTray({
          getMainWindow: opts.getMainWindow,
          hideOnClose: opts.hideOnClose !== false,
        });
      } catch (err) {
        console.warn('[system] tray install failed:', (err as Error).message);
      }
    });
  }

  // 7. Firewall rules — verify on startup, silently skip in dev.
  if (opts.enableFirewall !== false && app.isPackaged && process.platform === 'win32') {
    app.whenReady().then(async () => {
      try {
        const status = await ensureFirewall();
        if (status.missing.length > 0 && !status.elevated) {
          console.log('[system] firewall rules missing and not elevated — spawning elevated repair');
          requestElevatedFirewallRepair();
        }
      } catch (err) {
        console.warn('[system] firewall check failed:', (err as Error).message);
      }
    });
  }

  // Diagnostic IPC — renderer Settings page uses this.
  ipcMain.handle('system:info', () => ({
    version: app.getVersion(),
    packaged: app.isPackaged,
    platform: process.platform,
    arch: process.arch,
    launchedHidden: launchedHidden(),
    userData: app.getPath('userData'),
    exe: app.getPath('exe'),
    pid: process.pid,
  }));

  console.log('[system] integrations installed');
  return { locked: true, launchedHidden: launchedHidden() };
}

export { launchedHidden } from './autoStart';
export { dispatchDeepLink } from './protocolHandler';
export { showNotification } from './notifications';
export { updateBadge } from './tray';
export { ensureFirewall } from './firewall';
