/**
 * settingsStore — persistent renderer settings on the main process side.
 *
 * Why:
 *   The renderer's zustand `useSettingsStore` writes to localStorage,
 *   which is ephemeral storage scoped to the WebContents. When the
 *   user uninstalls + reinstalls the desktop app the localStorage is
 *   wiped (Electron treats it as part of the app's session data) and
 *   all preferences (theme, server URL, device choices, …) are lost.
 *
 *   Persisting to %APPDATA%/CommClient/settings.json instead survives
 *   uninstall + reinstall + portable copies AND lets us surface a
 *   "settings" panel in the future for ops to inspect / patch.
 *
 *   The renderer keeps localStorage as a write-through cache so reads
 *   stay synchronous; the IPC round-trip happens on writes only.
 *
 * IPC contract:
 *   settings:load   () -> object          read full settings dict
 *   settings:save   (settings) -> void    overwrite full settings dict
 *   settings:reset  () -> void            delete settings file
 *   settings:patch  (partial) -> object   merge + write, return new full
 */
import { app, ipcMain } from 'electron';
import { join } from 'path';
import { existsSync, readFileSync, writeFileSync, mkdirSync, unlinkSync, renameSync } from 'fs';

const SETTINGS_FILENAME = 'settings.json';

function getSettingsDir(): string {
  // Prefer the OS app-data location (survives uninstall/reinstall in
  // the typical case); fall back to Electron's userData if APPDATA is
  // unavailable (e.g. service contexts).
  const appData = process.env.APPDATA
    || (process.platform === 'win32' ? null : join(process.env.HOME || '', '.config'));
  if (!appData) return app.getPath('userData');
  const dir = join(appData, 'CommClient');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return dir;
}

function getSettingsPath(): string {
  return join(getSettingsDir(), SETTINGS_FILENAME);
}

function loadSettings(): Record<string, unknown> {
  const path = getSettingsPath();
  if (!existsSync(path)) return {};
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>;
  } catch (err) {
    console.error(`[settings] failed to parse ${path}:`, (err as Error).message);
    return {};
  }
}

function saveSettings(data: Record<string, unknown>): void {
  const path = getSettingsPath();
  // Atomic write: tmp file + rename so a crash mid-write doesn't leave
  // a half-written settings file that fails to parse on next boot.
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
  try {
    renameSync(tmp, path);
  } catch {
    // Some Windows AV products briefly lock the destination during
    // rename; fall back to direct write.
    writeFileSync(path, JSON.stringify(data, null, 2), 'utf-8');
    try { unlinkSync(tmp); } catch { /* tmp may not exist */ }
  }
}

function resetSettings(): void {
  const path = getSettingsPath();
  if (existsSync(path)) {
    try { unlinkSync(path); } catch { /* race / lock */ }
  }
}

let registered = false;

/** Wire the IPC handlers. Call once from main.ts after `app.whenReady()`. */
export function installSettingsStore(): void {
  if (registered) return;
  registered = true;

  ipcMain.handle('settings:load', () => loadSettings());

  ipcMain.handle('settings:save', (_evt, data: Record<string, unknown>) => {
    saveSettings(data || {});
  });

  ipcMain.handle('settings:reset', () => {
    resetSettings();
  });

  ipcMain.handle('settings:patch', (_evt, partial: Record<string, unknown>) => {
    const current = loadSettings();
    const merged = { ...current, ...(partial || {}) };
    saveSettings(merged);
    return merged;
  });

  ipcMain.handle('settings:path', () => getSettingsPath());
}
