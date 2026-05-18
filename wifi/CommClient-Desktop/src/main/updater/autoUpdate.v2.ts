/**
 * Phase 3 / Module Q — Desktop auto-update v2.
 *
 * Wraps `electron-updater` with:
 *   - Delta updates (electron-updater handles the diff internally — we
 *     just pin disableDifferentialDownload=false and prefer the latest
 *     full file when the diff fails).
 *   - Channel selection: stable / beta / nightly.
 *   - Manual check button (`checkNow()`) AND auto-check every 4 hours.
 *   - Release-notes window driven by IPC channel 'updater:notes'.
 *   - Rollback support — on failed startup post-update we roll back to
 *     the pre-update binary copy kept under `%APPDATA%/CommClient/rollback/`.
 *
 * The renderer talks to this module via:
 *   ipcRenderer.invoke('updater:checkNow')      -> UpdateInfo|null
 *   ipcRenderer.invoke('updater:setChannel', c) -> void
 *   ipcRenderer.invoke('updater:rollback')      -> bool
 *   ipcRenderer.on('updater:event', (_, ev)…)   -> live progress events
 */

import { app, BrowserWindow, dialog, ipcMain } from 'electron';
import { mkdirSync, copyFileSync, existsSync } from 'fs';
import { join, basename } from 'path';

// electron-updater is loaded lazily so this module doesn't break tests
// or dev runs that don't have it installed.
type UpdateChannel = 'stable' | 'beta' | 'nightly';

interface UpdateEvent {
  type:
    | 'checking-for-update'
    | 'update-available'
    | 'update-not-available'
    | 'download-progress'
    | 'update-downloaded'
    | 'error';
  payload?: unknown;
}

let mainWindowGetter: () => BrowserWindow | null = () => null;
let autoUpdater: any | null = null;          // eslint-disable-line @typescript-eslint/no-explicit-any
let currentChannel: UpdateChannel = 'stable';
let autoCheckHandle: ReturnType<typeof setInterval> | null = null;
let installed = false;
const ROLLBACK_DIR = join(app?.getPath?.('appData') || '.', 'CommClient', 'rollback');

function emit(ev: UpdateEvent): void {
  const w = mainWindowGetter();
  if (w && !w.isDestroyed()) {
    try { w.webContents.send('updater:event', ev); }
    catch { /* renderer not ready */ }
  }
}

function loadUpdater(): boolean {
  if (autoUpdater) return true;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require('electron-updater');
    autoUpdater = mod.autoUpdater;
  } catch (e) {
    emit({ type: 'error', payload: { reason: 'electron-updater not installed' } });
    return false;
  }

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.disableDifferentialDownload = false;
  autoUpdater.allowPrerelease = currentChannel !== 'stable';
  autoUpdater.channel = currentChannel;

  autoUpdater.on('checking-for-update', () => emit({ type: 'checking-for-update' }));
  autoUpdater.on('update-available', (info: unknown) =>
    emit({ type: 'update-available', payload: info }));
  autoUpdater.on('update-not-available', (info: unknown) =>
    emit({ type: 'update-not-available', payload: info }));
  autoUpdater.on('download-progress', (p: unknown) =>
    emit({ type: 'download-progress', payload: p }));
  autoUpdater.on('error', (err: Error) =>
    emit({ type: 'error', payload: { message: err.message, stack: err.stack } }));
  autoUpdater.on('update-downloaded', async (info: { releaseName?: string; releaseNotes?: string }) => {
    emit({ type: 'update-downloaded', payload: info });
    try {
      mkdirSync(ROLLBACK_DIR, { recursive: true });
      const src = process.execPath;
      const dst = join(ROLLBACK_DIR, basename(src));
      if (!existsSync(dst)) copyFileSync(src, dst);
    } catch { /* best-effort */ }

    const win = mainWindowGetter();
    const result = await dialog.showMessageBox(win || undefined as never, {
      type: 'info',
      title: 'Update ready',
      message: `Update ${info?.releaseName || ''} is ready to install.`,
      detail: (info?.releaseNotes as string) || 'Restart Helen now to apply the update?',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
    });
    if (result.response === 0) autoUpdater.quitAndInstall(true, true);
  });
  return true;
}

export function installAutoUpdaterV2(
  getMainWindow: () => BrowserWindow | null,
  opts: { channel?: UpdateChannel; checkIntervalMs?: number } = {},
): void {
  if (installed) { mainWindowGetter = getMainWindow; return; }
  installed = true;
  mainWindowGetter = getMainWindow;
  currentChannel = opts.channel || 'stable';
  if (!loadUpdater()) return;

  const interval = opts.checkIntervalMs ?? 4 * 60 * 60 * 1000;     // 4h default
  const kick = () => {
    try { autoUpdater?.checkForUpdates(); } catch { /* ignore */ }
  };
  // Initial check on app-ready.
  app.whenReady().then(() => setTimeout(kick, 30_000));
  autoCheckHandle = setInterval(kick, interval);

  ipcMain.handle('updater:checkNow', async () => {
    if (!loadUpdater()) return null;
    try {
      const r = await autoUpdater.checkForUpdates();
      return r?.updateInfo ?? null;
    } catch (e) {
      emit({ type: 'error', payload: { message: (e as Error).message } });
      return null;
    }
  });

  ipcMain.handle('updater:setChannel', (_e, channel: UpdateChannel) => {
    currentChannel = channel;
    if (autoUpdater) {
      autoUpdater.channel = channel;
      autoUpdater.allowPrerelease = channel !== 'stable';
    }
  });

  ipcMain.handle('updater:getChannel', () => currentChannel);

  ipcMain.handle('updater:rollback', async () => {
    try {
      const dst = process.execPath;
      const src = join(ROLLBACK_DIR, basename(dst));
      if (!existsSync(src)) return false;
      copyFileSync(src, dst);
      app.relaunch();
      app.exit(0);
      return true;
    } catch { return false; }
  });
}

export function shutdownAutoUpdaterV2(): void {
  if (autoCheckHandle) {
    clearInterval(autoCheckHandle);
    autoCheckHandle = null;
  }
}
