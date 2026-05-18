/**
 * Windows auto-start manager — system-integration module.
 *
 * Registers the packaged CommClient .exe under the current user's Run
 * registry key so the app launches with Windows without requiring a
 * Scheduled Task or Service. Uses Electron's `app.setLoginItemSettings`
 * which on Windows wraps `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
 *
 * Strategy
 * --------
 *   * Respect persisted user choice (`%APPDATA%/CommClient/data/system.json`).
 *   * Default to ENABLED for packaged builds (first run only — we never
 *     flip it back on after the user disables it).
 *   * Launch minimized to tray (`--hidden`) so boot isn't noisy.
 *   * Expose IPC channel `system:autostart` so renderer Settings UI can
 *     toggle it without talking directly to the registry.
 *   * Dev builds are opt-out — we skip registration unless
 *     `COMMCLIENT_AUTOSTART_DEV=1`.
 */

import { app, ipcMain } from 'electron';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname, join } from 'path';

const STATE_DIR = join(app.getPath('appData'), 'CommClient', 'data');
const STATE_FILE = join(STATE_DIR, 'system.json');

interface SystemState {
  autoStartUserChoice?: boolean; // undefined = never set → use default
  autoStartFirstBootApplied?: boolean;
}

function readState(): SystemState {
  try {
    if (!existsSync(STATE_FILE)) return {};
    return JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
  } catch {
    return {};
  }
}

function writeState(patch: Partial<SystemState>): void {
  try {
    const cur = readState();
    const next = { ...cur, ...patch };
    mkdirSync(dirname(STATE_FILE), { recursive: true });
    writeFileSync(STATE_FILE, JSON.stringify(next, null, 2), 'utf-8');
  } catch (err) {
    console.warn('[autoStart] state write failed:', (err as Error).message);
  }
}

// ─── public API ─────────────────────────────────────────────────────────

export interface AutoStartOptions {
  /** Default behaviour on first ever launch. True for packaged LAN servers. */
  defaultEnabled?: boolean;
  /** Extra args to append to the boot command (default: ['--hidden']). */
  bootArgs?: string[];
}

export function getAutoStartStatus(): {
  enabled: boolean;
  registered: boolean;
  userChoice?: boolean;
} {
  const settings = app.getLoginItemSettings({
    args: ['--hidden'],
  });
  const state = readState();
  return {
    enabled: settings.openAtLogin,
    registered: settings.openAtLogin,
    userChoice: state.autoStartUserChoice,
  };
}

export function setAutoStart(enabled: boolean): void {
  // Dev builds don't touch the registry unless explicitly opted-in.
  if (!app.isPackaged && process.env.COMMCLIENT_AUTOSTART_DEV !== '1') {
    console.log('[autoStart] dev build — skipping registry write');
    writeState({ autoStartUserChoice: enabled });
    return;
  }

  app.setLoginItemSettings({
    openAtLogin: enabled,
    openAsHidden: true,
    args: ['--hidden'],
  });
  writeState({ autoStartUserChoice: enabled });
  console.log(`[autoStart] ${enabled ? 'enabled' : 'disabled'} for current user`);
}

export function installAutoStart(opts: AutoStartOptions = {}): void {
  const defaults: Required<AutoStartOptions> = {
    defaultEnabled: true,
    bootArgs: ['--hidden'],
    ...opts,
  } as Required<AutoStartOptions>;

  // First-run policy
  const state = readState();
  if (app.isPackaged && !state.autoStartFirstBootApplied) {
    const choice = state.autoStartUserChoice ?? defaults.defaultEnabled;
    try {
      app.setLoginItemSettings({
        openAtLogin: choice,
        openAsHidden: true,
        args: defaults.bootArgs,
      });
    } catch (err) {
      console.warn('[autoStart] initial setLoginItemSettings failed:', (err as Error).message);
    }
    writeState({ autoStartFirstBootApplied: true, autoStartUserChoice: choice });
    console.log(`[autoStart] first-boot policy applied (enabled=${choice})`);
  }

  // Expose IPC so the renderer's Settings UI can flip it.
  ipcMain.handle('system:autostart:get', () => getAutoStartStatus());
  ipcMain.handle('system:autostart:set', (_evt, enabled: boolean) => {
    setAutoStart(!!enabled);
    return getAutoStartStatus();
  });
}

/** True when the process was launched at Windows login with --hidden. */
export function launchedHidden(): boolean {
  return process.argv.some((a) => a === '--hidden' || a === '/hidden');
}
