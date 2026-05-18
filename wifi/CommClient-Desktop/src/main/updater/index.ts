/**
 * Updater public surface — imported by src/main/index.ts.
 *
 * Default options read from environment / Electron's userData config:
 *   * COMMCLIENT_UPDATE_LAN_URL    → LAN mirror base URL
 *   * COMMCLIENT_UPDATE_FEED_URL   → internet fallback feed
 *   * COMMCLIENT_UPDATE_CHANNEL    → stable | beta | canary
 *   * COMMCLIENT_UPDATE_PUBKEY     → Base64 Ed25519 public key
 *
 * The update channel is also persisted to %APPDATA%/CommClient/data/
 * updater.json so a user setting persists across upgrades.
 */

import { BrowserWindow, app } from 'electron';
import { existsSync, readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname, join } from 'path';
import { installUpdater, checkForUpdates, stopUpdater, getUpdateStatus } from './electronUpdater';
import type { UpdaterOptions, UpdateChannel } from './types';

const STATE_FILE = join(app.getPath('appData'), 'CommClient', 'data', 'updater.json');

function readState(): Partial<UpdaterOptions> {
  try {
    if (!existsSync(STATE_FILE)) return {};
    return JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
  } catch {
    return {};
  }
}

function writeState(patch: Partial<UpdaterOptions>): void {
  try {
    const next = { ...readState(), ...patch };
    mkdirSync(dirname(STATE_FILE), { recursive: true });
    writeFileSync(STATE_FILE, JSON.stringify(next, null, 2), 'utf-8');
  } catch (err) {
    console.warn('[updater] state write failed:', (err as Error).message);
  }
}

export interface InstallArgs {
  getMainWindow: () => BrowserWindow | null;
  /** Overrides (programmatic setup). */
  overrides?: Partial<UpdaterOptions>;
}

export async function installUpdateSystem(args: InstallArgs): Promise<void> {
  const state = readState();
  const env = process.env;

  const options: UpdaterOptions = {
    channel:
      (args.overrides?.channel as UpdateChannel) ||
      (state.channel as UpdateChannel) ||
      (env.COMMCLIENT_UPDATE_CHANNEL as UpdateChannel) ||
      'stable',
    lanMirrorUrl:
      args.overrides?.lanMirrorUrl || state.lanMirrorUrl || env.COMMCLIENT_UPDATE_LAN_URL,
    internetFeedUrl:
      args.overrides?.internetFeedUrl || state.internetFeedUrl || env.COMMCLIENT_UPDATE_FEED_URL,
    publicKeyBase64:
      args.overrides?.publicKeyBase64 || state.publicKeyBase64 || env.COMMCLIENT_UPDATE_PUBKEY,
    checkIntervalMinutes: args.overrides?.checkIntervalMinutes || 60,
    requireSignature: args.overrides?.requireSignature ?? true,
    autoDownload: args.overrides?.autoDownload ?? true,
    // Default OFF: silently overwriting the working binary on quit
    // had no rollback path. The updater now downloads the new
    // version and surfaces an "update available" UI; the user
    // confirms before install. Set COMMCLIENT_UPDATE_SILENT=1 to
    // restore the legacy behaviour for unattended deployments.
    autoInstallOnAppQuit:
      args.overrides?.autoInstallOnAppQuit
        ?? (env.COMMCLIENT_UPDATE_SILENT === '1'),
    allowDowngrade:
      !!args.overrides?.allowDowngrade
      || env.COMMCLIENT_UPDATE_ALLOW_DOWNGRADE === '1',
  };

  // Persist any newly supplied values so next boot has them.
  writeState({
    channel: options.channel,
    lanMirrorUrl: options.lanMirrorUrl,
    internetFeedUrl: options.internetFeedUrl,
  });

  await installUpdater({ getMainWindow: args.getMainWindow, options });
}

export { checkForUpdates, stopUpdater, getUpdateStatus };
export type { UpdateChannel, UpdaterOptions, UpdateStatus } from './types';
