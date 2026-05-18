/**
 * Native Windows notifications — Action Center integration.
 *
 * Uses the Electron `Notification` class which on Windows renders as a
 * toast via the XML Toast API. For toasts to persist in Action Center
 * (and for click/action events to route back after the app has closed)
 * Windows requires an AUMID (Application User Model ID) that matches
 * the one registered in the Start Menu shortcut.
 *
 * AUMID registration sources (in priority order):
 *   1. electron-builder NSIS — writes the AUMID to the .lnk shortcut
 *      automatically when `appId` is set in electron-builder.yml. We
 *      mirror that here via `app.setAppUserModelId` at runtime so the
 *      in-app Notification calls match.
 *   2. Installer scripts (installer.system.nsh) — add
 *      System.AppUserModel.ID property on the .lnk via WScript.Shell.
 *
 * Toast actions: Windows 10+ supports up to 5 action buttons. Each
 * action's `type` must be `button` and `text` is visible; clicking
 * raises the `action` event with the index of the pressed button.
 * Inline reply (`type: 'text'`) is NOT supported by Electron's wrapper
 * — for reply boxes we'd need a native XML template via node-windows.
 * That's deferred — most flows here just need deep-link-on-click.
 */

import { app, BrowserWindow, Notification, ipcMain, nativeImage } from 'electron';
import { join } from 'path';
import { existsSync } from 'fs';
import { dispatchDeepLink } from './protocolHandler';

// MUST match electron-builder.yml `appId` so toasts persist in
// Action Center and clicks route back to a re-launched app instance.
// Mismatch silently breaks notification deep-links — the toast shows
// once but disappears from history and clicks no-op.
const AUMID = 'com.helen.desktop';

let focusedWindow: () => BrowserWindow | null = () => null;
let iconPath: string | null = null;

export interface NotifyAction {
  /** Label shown on the button. Keep short (Windows truncates ~~20 chars). */
  text: string;
  /** Deep link executed when the button is clicked. Runs through protocolHandler. */
  deepLink?: string;
  /** Arbitrary ID sent to renderer via IPC when button is clicked. */
  id?: string;
}

export interface NotifyOptions {
  title: string;
  body: string;
  /** Plays default sound unless true. */
  silent?: boolean;
  /** Up to 5. Windows Action Center shows them inline. */
  actions?: NotifyAction[];
  /** If set, clicking the toast body opens this deep link. */
  clickDeepLink?: string;
  /** Send a `notification:click` event with this ID to renderer. */
  clickId?: string;
  /** Optional image path/absolute file URL. */
  imagePath?: string;
  /** Raise to top when clicked (default true). */
  focusOnClick?: boolean;
  /** Reply payload tagging — emitted with click/action events. */
  tag?: string;
}

function resolveIcon(): string | null {
  if (iconPath) return iconPath;
  // Try packaged resource paths first, then dev layout.
  const candidates = [
    join(process.resourcesPath || '', 'build', 'icon.ico'),
    join(process.resourcesPath || '', 'icon.ico'),
    join(app.getAppPath(), 'build', 'icon.ico'),
    join(app.getAppPath(), 'resources', 'icon.ico'),
    join(app.getAppPath(), '..', 'build', 'icon.ico'),
  ];
  for (const p of candidates) {
    try {
      if (p && existsSync(p)) {
        iconPath = p;
        return p;
      }
    } catch {
      /* ignore */
    }
  }
  return null;
}

function bringToFront(): void {
  const w = focusedWindow();
  if (!w) return;
  if (w.isMinimized()) w.restore();
  w.show();
  w.focus();
}

export function showNotification(opts: NotifyOptions): Notification | null {
  if (!Notification.isSupported()) {
    console.warn('[notifications] platform does not support native notifications');
    return null;
  }

  const icon = opts.imagePath || resolveIcon() || undefined;

  const n = new Notification({
    title: opts.title,
    body: opts.body,
    silent: !!opts.silent,
    icon: icon ? nativeImage.createFromPath(icon) : undefined,
    toastXml: undefined, // default template
    actions: (opts.actions || []).slice(0, 5).map((a) => ({
      type: 'button',
      text: a.text,
    })),
  });

  n.on('click', () => {
    const win = focusedWindow();
    if (opts.focusOnClick !== false) bringToFront();

    if (win) {
      win.webContents.send('notification:click', {
        tag: opts.tag,
        clickId: opts.clickId,
        deepLink: opts.clickDeepLink,
      });
    }
    if (opts.clickDeepLink) dispatchDeepLink(opts.clickDeepLink);
  });

  n.on('action', (_event, index) => {
    const action = (opts.actions || [])[index];
    if (!action) return;

    const win = focusedWindow();
    if (win) {
      win.webContents.send('notification:action', {
        tag: opts.tag,
        index,
        id: action.id,
        deepLink: action.deepLink,
      });
    }
    if (action.deepLink) dispatchDeepLink(action.deepLink);
  });

  n.on('close', () => {
    const win = focusedWindow();
    if (win) {
      win.webContents.send('notification:close', { tag: opts.tag });
    }
  });

  n.on('failed', (_e, err) => {
    console.warn('[notifications] toast failed:', err);
  });

  n.show();
  return n;
}

// ─── public API ─────────────────────────────────────────────────────────

export interface NotificationsOptions {
  getMainWindow: () => BrowserWindow | null;
  aumid?: string;
}

export function installNotifications(opts: NotificationsOptions): void {
  focusedWindow = opts.getMainWindow;

  // AUMID must be set BEFORE the first Notification is shown. Electron
  // uses this value to route toast activations back to the running app.
  const aumid = opts.aumid || AUMID;
  try {
    app.setAppUserModelId(aumid);
    console.log(`[notifications] AUMID set to ${aumid}`);
  } catch (err) {
    console.warn('[notifications] setAppUserModelId failed:', (err as Error).message);
  }

  // IPC bridge — renderer-issued toasts.
  ipcMain.handle('notifications:show', (_evt, payload: NotifyOptions) => {
    try {
      showNotification(payload);
      return { ok: true };
    } catch (err) {
      return { ok: false, error: (err as Error).message };
    }
  });

  ipcMain.handle('notifications:supported', () => Notification.isSupported());
}
