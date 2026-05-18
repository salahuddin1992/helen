/**
 * System tray — persistent Windows tray icon + context menu.
 *
 * Keeps the app alive when the window is closed (close → hide to tray,
 * quit via tray menu or explicit Cmd+Q). Exposes:
 *   * Show / Hide main window
 *   * Quick-join last call
 *   * Toggle Do-Not-Disturb (forwarded to renderer)
 *   * Open Settings
 *   * Quit
 *
 * The unread-count badge is updated via `updateBadge(count)`.
 * Windows taskbar overlay icons are handled separately by the Electron
 * renderer via `webContents.setBadgeCount` / `setOverlayIcon` — see
 * `taskbarOverlay.ts`.
 */

import { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain } from 'electron';
import { join } from 'path';
import { existsSync } from 'fs';

let tray: Tray | null = null;
let getWin: () => BrowserWindow | null = () => null;
let dnd = false;
let unread = 0;

function resolveIcon(): string {
  const candidates = [
    join(process.resourcesPath || '', 'build', 'tray.ico'),
    join(process.resourcesPath || '', 'tray.ico'),
    join(process.resourcesPath || '', 'build', 'icon.ico'),
    join(app.getAppPath(), 'build', 'tray.ico'),
    join(app.getAppPath(), 'build', 'icon.ico'),
    join(app.getAppPath(), 'resources', 'icon.ico'),
  ];
  for (const p of candidates) {
    try {
      if (p && existsSync(p)) return p;
    } catch {
      /* ignore */
    }
  }
  // Last-resort empty image — Electron still renders the tray.
  return '';
}

function buildMenu(): Menu {
  return Menu.buildFromTemplate([
    {
      label: `CommClient${unread > 0 ? `  •  ${unread} unread` : ''}`,
      enabled: false,
    },
    { type: 'separator' },
    {
      label: 'Show Window',
      click: () => {
        const w = getWin();
        if (!w) return;
        if (w.isMinimized()) w.restore();
        w.show();
        w.focus();
      },
    },
    {
      label: 'Hide Window',
      click: () => {
        const w = getWin();
        if (w && w.isVisible()) w.hide();
      },
    },
    { type: 'separator' },
    {
      label: dnd ? 'Disable Do Not Disturb' : 'Enable Do Not Disturb',
      click: () => {
        dnd = !dnd;
        const w = getWin();
        if (w) w.webContents.send('tray:dnd', { enabled: dnd });
        refreshTray();
      },
    },
    {
      label: 'Settings',
      click: () => {
        const w = getWin();
        if (!w) return;
        if (w.isMinimized()) w.restore();
        w.show();
        w.focus();
        w.webContents.send('tray:navigate', { target: 'settings' });
      },
    },
    { type: 'separator' },
    {
      label: 'Check for Updates…',
      click: () => {
        const w = getWin();
        if (w) w.webContents.send('tray:update-check', {});
      },
    },
    { type: 'separator' },
    {
      label: 'Quit CommClient',
      click: () => {
        (app as any).isQuittingFromTray = true;
        app.quit();
      },
    },
  ]);
}

function refreshTray(): void {
  if (!tray) return;
  try {
    tray.setToolTip(
      `CommClient${unread > 0 ? ` • ${unread} unread` : ''}${dnd ? ' (DND)' : ''}`
    );
    tray.setContextMenu(buildMenu());
  } catch (err) {
    console.warn('[tray] refresh failed:', (err as Error).message);
  }
}

export interface TrayOptions {
  getMainWindow: () => BrowserWindow | null;
  /** When true, intercept window.close and hide-to-tray instead. */
  hideOnClose?: boolean;
}

export function installTray(opts: TrayOptions): void {
  getWin = opts.getMainWindow;

  const iconPath = resolveIcon();
  const img = iconPath ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty();
  tray = new Tray(img);
  tray.setToolTip('CommClient');
  tray.setContextMenu(buildMenu());

  // Single-click (Windows) / double-click: toggle window visibility.
  tray.on('click', () => {
    const w = getWin();
    if (!w) return;
    if (w.isVisible() && !w.isMinimized()) w.hide();
    else {
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    }
  });

  tray.on('double-click', () => {
    const w = getWin();
    if (!w) return;
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
  });

  // Hide-on-close behaviour — wired on the main window elsewhere.
  if (opts.hideOnClose !== false) {
    app.on('browser-window-created', (_e, window) => {
      window.on('close', (event) => {
        if ((app as any).isQuittingFromTray) return;
        event.preventDefault();
        window.hide();
      });
    });
  }

  // IPC — renderer updates
  ipcMain.handle('tray:set-unread', (_evt, count: number) => {
    unread = Math.max(0, Number(count) | 0);
    refreshTray();
    return { ok: true, count: unread };
  });

  ipcMain.handle('tray:set-dnd', (_evt, enabled: boolean) => {
    dnd = !!enabled;
    refreshTray();
    return { ok: true, dnd };
  });

  ipcMain.handle('tray:flash', (_evt, reason: string) => {
    const w = getWin();
    try {
      if (w) w.flashFrame(true);
    } catch {
      /* ignore */
    }
    return { ok: true, reason };
  });

  console.log('[tray] installed');
}

export function updateBadge(count: number): void {
  unread = Math.max(0, Number(count) | 0);
  refreshTray();
}
