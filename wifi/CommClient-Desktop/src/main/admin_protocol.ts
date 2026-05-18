/**
 * admin_protocol — Electron main-process IPC handlers used by the embedded
 * admin panels.
 *
 * Capabilities exposed
 * ────────────────────
 *   helen.admin.openExternal(url)          → shell.openExternal with allow-list
 *   helen.admin.pickPluginFile()           → showOpenDialog for plugin install
 *   helen.admin.saveExport(opts)           → showSaveDialog + writeFile for
 *                                            GDPR/eDiscovery exports
 *   helen.admin.showItemInFolder(path)     → reveal a downloaded artifact
 *   helen.admin.getDesktopMeta()           → small JSON with platform info so
 *                                            the panels can adapt their UI
 *   helen.admin.notifyOS(opts)             → native OS notification fallback
 *
 * Security model
 * ──────────────
 *   - URLs passed to openExternal go through an origin/scheme allow-list.
 *   - File picks return the path but never auto-execute.
 *   - This module must be required from src/main/index.ts AFTER `app.ready`.
 *
 * Wiring
 * ──────
 * Add `registerAdminProtocol()` from this file inside the main bootstrap.
 * The preload script must forward these handlers to the renderer under
 * `window.electronAPI.admin.*`.
 */

import { ipcMain, shell, dialog, Notification, app, BrowserWindow } from 'electron';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';

// Schemes that may be opened externally. Anything else is rejected.
const ALLOWED_EXTERNAL_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

// Optional origin allow-list — extend per deployment. Empty = allow any
// http/https origin (operators may host admin panels behind arbitrary
// internal URLs).
const EXTERNAL_ORIGIN_ALLOWLIST: ReadonlySet<string> = new Set([
  // 'https://docs.helen.example',
]);

function safeIsAllowedUrl(url: string): boolean {
  try {
    const u = new URL(url);
    if (!ALLOWED_EXTERNAL_SCHEMES.has(u.protocol)) return false;
    if (EXTERNAL_ORIGIN_ALLOWLIST.size === 0) return true;
    if (u.protocol === 'mailto:') return true;
    return EXTERNAL_ORIGIN_ALLOWLIST.has(u.origin);
  } catch { return false; }
}

let registered = false;

export interface AdminProtocolBindings {
  /** Unregister all handlers (used in tests / hot-reload). */
  dispose(): void;
}

export function registerAdminProtocol(): AdminProtocolBindings {
  if (registered) {
    return { dispose: () => disposeAdminProtocol() };
  }
  registered = true;

  // ── openExternal ───────────────────────────────────────────────────
  ipcMain.handle('helen.admin.openExternal', async (_ev, rawUrl: unknown) => {
    if (typeof rawUrl !== 'string' || !safeIsAllowedUrl(rawUrl)) {
      return { ok: false, error: 'URL rejected by allow-list' };
    }
    try {
      await shell.openExternal(rawUrl);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
  });

  // ── pickPluginFile ─────────────────────────────────────────────────
  ipcMain.handle('helen.admin.pickPluginFile', async (ev) => {
    const win = BrowserWindow.fromWebContents(ev.sender) || undefined;
    const result = await dialog.showOpenDialog(win as BrowserWindow, {
      title: 'Select plugin package',
      filters: [
        { name: 'Helen plugin', extensions: ['hpkg', 'zip', 'tar', 'gz', 'tgz'] },
        { name: 'All files', extensions: ['*'] },
      ],
      properties: ['openFile', 'showHiddenFiles'],
    });
    if (result.canceled || result.filePaths.length === 0) return { ok: false };
    return { ok: true, filePath: result.filePaths[0] };
  });

  // ── saveExport ─────────────────────────────────────────────────────
  ipcMain.handle(
    'helen.admin.saveExport',
    async (ev, opts: { suggestedName?: string; data?: string; base64?: string; mime?: string }) => {
      if (!opts || (opts.data == null && opts.base64 == null)) {
        return { ok: false, error: 'data or base64 required' };
      }
      const win = BrowserWindow.fromWebContents(ev.sender) || undefined;
      const def = path.join(app.getPath('downloads'), opts.suggestedName || 'helen_admin_export.json');
      const result = await dialog.showSaveDialog(win as BrowserWindow, {
        title: 'Save export',
        defaultPath: def,
      });
      if (result.canceled || !result.filePath) return { ok: false };
      try {
        const buf = opts.base64 ? Buffer.from(opts.base64, 'base64') : Buffer.from(opts.data!, 'utf8');
        await fs.writeFile(result.filePath, buf);
        return { ok: true, filePath: result.filePath };
      } catch (e) {
        return { ok: false, error: (e as Error).message };
      }
    },
  );

  // ── showItemInFolder ───────────────────────────────────────────────
  ipcMain.handle('helen.admin.showItemInFolder', async (_ev, p: unknown) => {
    if (typeof p !== 'string') return { ok: false };
    try {
      shell.showItemInFolder(p);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
  });

  // ── getDesktopMeta ─────────────────────────────────────────────────
  ipcMain.handle('helen.admin.getDesktopMeta', async () => ({
    ok: true,
    platform: process.platform,
    arch: process.arch,
    appVersion: app.getVersion(),
    electronVersion: process.versions.electron,
    nodeVersion: process.versions.node,
    locale: app.getLocale(),
    isPackaged: app.isPackaged,
  }));

  // ── notifyOS ───────────────────────────────────────────────────────
  ipcMain.handle(
    'helen.admin.notifyOS',
    async (_ev, opts: { title: string; body?: string; silent?: boolean }) => {
      if (!Notification.isSupported()) return { ok: false, error: 'notifications unsupported' };
      try {
        new Notification({
          title: String(opts?.title ?? 'Helen Admin'),
          body: typeof opts?.body === 'string' ? opts.body : undefined,
          silent: !!opts?.silent,
          urgency: 'normal',
        }).show();
        return { ok: true };
      } catch (e) {
        return { ok: false, error: (e as Error).message };
      }
    },
  );

  return { dispose: () => disposeAdminProtocol() };
}

export function disposeAdminProtocol(): void {
  if (!registered) return;
  registered = false;
  ipcMain.removeHandler('helen.admin.openExternal');
  ipcMain.removeHandler('helen.admin.pickPluginFile');
  ipcMain.removeHandler('helen.admin.saveExport');
  ipcMain.removeHandler('helen.admin.showItemInFolder');
  ipcMain.removeHandler('helen.admin.getDesktopMeta');
  ipcMain.removeHandler('helen.admin.notifyOS');
}
