/**
 * electron-updater wrapper with LAN-mirror + signature verification.
 *
 * Behaviour:
 *   1. On startup (packaged only) resolve the best feed:
 *        LAN mirror  →  internet feed  →  disabled
 *   2. Configure autoUpdater with that feed URL.
 *   3. Probe the channel manifest; if a newer version exists, verify
 *      the Ed25519 signature attached to the sha512.
 *   4. Download in the background (unless `autoDownload=false`).
 *   5. Install on next quit (or via IPC `updater:install-now`).
 *
 * LAN-mirror semantics:
 *   The server exposes a directory listing that matches electron-updater's
 *   "generic" provider layout:
 *
 *     /api/updates/channel-stable.json    ← YAML or JSON manifest
 *     /api/updates/CommClient-Setup-1.2.3.exe
 *     /api/updates/CommClient-Setup-1.2.3.exe.blockmap
 *
 *   That lets clients that lose internet still receive updates from the
 *   LAN server — we relay every release there.
 *
 * The module is resilient to electron-updater not being installed
 * (catches the import). In that state we fall back to manifest-only
 * polling so the renderer can still surface "update available" to the
 * user, who can then install manually.
 */

import { app, BrowserWindow, ipcMain } from 'electron';
import { resolveFeed } from './feedResolver';
import { verifyEd25519 } from './signatureVerify';
import type {
  UpdateChannel,
  UpdateStatus,
  UpdaterOptions,
  UpdateManifestEntry,
} from './types';

// ─── state ──────────────────────────────────────────────────────────────

let status: UpdateStatus = {
  state: 'idle',
  channel: 'stable',
  source: 'lan-mirror',
  currentVersion: '0.0.0',
};

let getWin: () => BrowserWindow | null = () => null;
let activeOptions: UpdaterOptions = {};
let autoUpdaterModule: any | null = null;
let pollTimer: NodeJS.Timeout | null = null;

// ─── helpers ────────────────────────────────────────────────────────────

function emit(): void {
  const w = getWin();
  if (w) w.webContents.send('updater:status', status);
}

function patchStatus(patch: Partial<UpdateStatus>): void {
  status = { ...status, ...patch };
  emit();
}

function requireSigRuntime(): boolean {
  if (activeOptions.requireSignature === false) return false;
  return app.isPackaged; // enforce in packaged, skip in dev
}

async function fetchJson(url: string): Promise<any> {
  const { net } = await import('electron');
  return new Promise((resolve, reject) => {
    try {
      const req = net.request({ method: 'GET', url });
      let body = '';
      req.on('response', (resp) => {
        resp.on('data', (chunk) => (body += chunk.toString('utf-8')));
        resp.on('end', () => {
          if (resp.statusCode < 200 || resp.statusCode >= 300) {
            reject(new Error(`HTTP ${resp.statusCode} for ${url}`));
          } else {
            try {
              resolve(JSON.parse(body));
            } catch (err) {
              reject(err);
            }
          }
        });
      });
      req.on('error', reject);
      req.end();
    } catch (err) {
      reject(err);
    }
  });
}

function cmpSemver(a: string, b: string): number {
  const toParts = (v: string) =>
    v
      .replace(/^v/, '')
      .split(/[.-]/)
      .map((p) => (/^\d+$/.test(p) ? Number(p) : p));
  const av = toParts(a);
  const bv = toParts(b);
  const len = Math.max(av.length, bv.length);
  for (let i = 0; i < len; i++) {
    const x = av[i] ?? 0;
    const y = bv[i] ?? 0;
    if (x === y) continue;
    if (typeof x === 'number' && typeof y === 'number') return x - y;
    return String(x) < String(y) ? -1 : 1;
  }
  return 0;
}

async function loadElectronUpdater(): Promise<any | null> {
  if (autoUpdaterModule) return autoUpdaterModule;
  try {
    // electron-updater is a runtime-optional dep; the package.json lists it,
    // but we deliberately keep the import dynamic so:
    //   1. builds without the native dep (e.g. portable ZIP) still compile;
    //   2. typecheck passes on CI images where node_modules is skipped.
    // @ts-ignore — resolved at runtime after `npm install`.
    const mod: any = await import('electron-updater').catch(() => null);
    if (!mod) return null;
    autoUpdaterModule = mod.autoUpdater;
    console.log('[updater] electron-updater loaded');
    return autoUpdaterModule;
  } catch (err) {
    console.warn(
      '[updater] electron-updater not installed — manifest-only polling',
      (err as Error).message
    );
    return null;
  }
}

async function configureElectronUpdater(
  baseUrl: string,
  channel: UpdateChannel
): Promise<boolean> {
  const au = await loadElectronUpdater();
  if (!au) return false;

  try {
    au.setFeedURL({
      provider: 'generic',
      url: baseUrl,
      channel,
    });
    au.channel = channel;
    au.autoDownload = activeOptions.autoDownload !== false;
    au.autoInstallOnAppQuit = activeOptions.autoInstallOnAppQuit !== false;
    au.allowDowngrade = !!activeOptions.allowDowngrade;
    au.fullChangelog = true;
    au.logger = {
      info: (m: string) => console.log('[electron-updater]', m),
      warn: (m: string) => console.warn('[electron-updater]', m),
      error: (m: string) => console.error('[electron-updater]', m),
      debug: (_m: string) => {
        /* silence */
      },
    };

    au.removeAllListeners();

    au.on('checking-for-update', () => patchStatus({ state: 'checking' }));

    au.on('update-available', (info: any) => {
      patchStatus({
        state: 'available',
        target: {
          version: info.version,
          channel,
          releasedAt: info.releaseDate,
          url: '',
          sha512: info.sha512 ?? '',
          signature: undefined,
          size: 0,
          notes: typeof info.releaseNotes === 'string' ? info.releaseNotes : undefined,
        },
        checkedAt: Date.now(),
      });
    });

    au.on('update-not-available', () => {
      patchStatus({ state: 'not-available', checkedAt: Date.now() });
    });

    au.on('download-progress', (p: any) => {
      patchStatus({
        state: 'downloading',
        progress: {
          bytesPerSecond: p.bytesPerSecond,
          percent: p.percent,
          transferred: p.transferred,
          total: p.total,
        },
      });
    });

    au.on('update-downloaded', () => {
      patchStatus({ state: 'downloaded' });
    });

    au.on('error', (err: Error) => {
      patchStatus({ state: 'error', error: err.message });
    });

    console.log(`[updater] electron-updater configured: ${baseUrl} (channel=${channel})`);
    return true;
  } catch (err) {
    console.warn('[updater] configure failed:', (err as Error).message);
    return false;
  }
}

async function manifestBasedCheck(manifestUrl: string): Promise<UpdateManifestEntry | null> {
  const json = await fetchJson(manifestUrl);
  // Support both shapes: {versions: [...], latest: "x"} and electron-updater
  // generic provider's yml-shaped JSON {version, path, sha512}.
  let latest: UpdateManifestEntry | null = null;

  if (Array.isArray(json?.versions)) {
    const channel = activeOptions.channel || 'stable';
    const matches = json.versions.filter((v: UpdateManifestEntry) => v.channel === channel);
    if (matches.length === 0) return null;
    matches.sort((a: UpdateManifestEntry, b: UpdateManifestEntry) => cmpSemver(b.version, a.version));
    latest = matches[0];
  } else if (json?.version) {
    latest = {
      version: json.version,
      channel: activeOptions.channel || 'stable',
      releasedAt: json.releaseDate || '',
      url: json.path || json.url || '',
      sha512: json.sha512 || '',
      signature: json.signature,
      size: json.size || 0,
      notes: json.releaseNotes,
    };
  }

  if (!latest) return null;
  if (cmpSemver(latest.version, status.currentVersion) <= 0) return null;

  // Signature check (only if provided OR required).
  // Audit fix C6: previously the signature payload was just sha512.
  // That meant a valid signature for v1.0.0 stayed valid for ANY
  // future build because it didn't bind version/channel/size.
  // Bind the full release tuple so old signatures cannot replay-
  // downgrade onto new builds. The CI signing pipeline must produce
  // signatures over the same canonical form (sha512|version|channel|size).
  if (latest.signature) {
    if (!activeOptions.publicKeyBase64) {
      console.warn('[updater] manifest signed but no public key configured — rejecting');
      return null;
    }
    const channel = (activeOptions.channel || 'stable').toLowerCase();
    const canonicalPayload = `${latest.sha512}|${latest.version}|${channel}|${latest.size || 0}`;
    let res = verifyEd25519(canonicalPayload, latest.signature, activeOptions.publicKeyBase64);

    if (!res.ok) {
      // Backwards-compat: some manifests signed before C6 only signed
      // the bare sha512. Accept those for one major version, log a
      // deprecation warning so the CI pipeline knows to upgrade.
      const legacy = verifyEd25519(latest.sha512, latest.signature, activeOptions.publicKeyBase64);
      if (legacy.ok) {
        console.warn(
          '[updater] DEPRECATED legacy signature shape (sha512-only). ' +
          'CI must upgrade to sha512|version|channel|size before next major.',
        );
        res = legacy;
      }
    }

    if (!res.ok) {
      console.warn('[updater] signature verification failed:', res.reason);
      return null;
    }
    console.log('[updater] signature verified OK for', latest.version);
  } else if (requireSigRuntime()) {
    console.warn('[updater] manifest unsigned and signature required — skipping');
    return null;
  }

  return latest;
}

// ─── public API ─────────────────────────────────────────────────────────

export interface InstallUpdaterArgs {
  getMainWindow: () => BrowserWindow | null;
  options: UpdaterOptions;
}

export async function installUpdater(args: InstallUpdaterArgs): Promise<void> {
  getWin = args.getMainWindow;
  activeOptions = args.options;

  status = {
    state: 'idle',
    channel: args.options.channel || 'stable',
    source: 'lan-mirror',
    currentVersion: app.getVersion(),
  };

  // IPC — renderer controls.
  ipcMain.handle('updater:get-status', () => status);

  ipcMain.handle('updater:check', async () => {
    await checkForUpdates();
    return status;
  });

  ipcMain.handle('updater:install-now', async () => {
    const au = autoUpdaterModule;
    if (!au) return { ok: false, reason: 'electron-updater unavailable' };
    try {
      au.quitAndInstall(false, true);
      return { ok: true };
    } catch (err) {
      return { ok: false, reason: (err as Error).message };
    }
  });

  ipcMain.handle('updater:set-channel', async (_evt, channel: UpdateChannel) => {
    activeOptions.channel = channel;
    patchStatus({ channel });
    if (autoUpdaterModule) autoUpdaterModule.channel = channel;
    return { ok: true, channel };
  });

  if (!app.isPackaged) {
    console.log('[updater] skipped in dev build');
    return;
  }

  await checkForUpdates();

  const intervalMin = Math.max(5, activeOptions.checkIntervalMinutes ?? 60);
  pollTimer = setInterval(() => {
    checkForUpdates().catch((err) =>
      console.warn('[updater] periodic check failed:', (err as Error).message)
    );
  }, intervalMin * 60 * 1000);
}

export async function checkForUpdates(): Promise<void> {
  try {
    patchStatus({ state: 'checking' });

    const feed = await resolveFeed(activeOptions);
    if (!feed) {
      patchStatus({ state: 'not-available', checkedAt: Date.now() });
      return;
    }
    patchStatus({ source: feed.source });

    const configured = await configureElectronUpdater(feed.baseUrl, status.channel);
    if (configured && autoUpdaterModule) {
      try {
        await autoUpdaterModule.checkForUpdatesAndNotify();
      } catch (err) {
        console.warn('[updater] checkForUpdatesAndNotify failed:', (err as Error).message);
        // Fall through to manifest-only path below.
      }
    }

    // Manifest-only path: emits "available" with the parsed entry so
    // the renderer can display the version/notes even when
    // electron-updater is absent.
    try {
      const entry = await manifestBasedCheck(feed.manifestUrl);
      if (entry) {
        patchStatus({ state: 'available', target: entry, checkedAt: Date.now() });
      } else if (!configured) {
        patchStatus({ state: 'not-available', checkedAt: Date.now() });
      }
    } catch (err) {
      if (!configured) {
        patchStatus({ state: 'error', error: (err as Error).message });
      }
    }
  } catch (err) {
    patchStatus({ state: 'error', error: (err as Error).message });
  }
}

export function stopUpdater(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

export function getUpdateStatus(): UpdateStatus {
  return status;
}
