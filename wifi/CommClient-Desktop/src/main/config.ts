/**
 * Central client config — single source of truth for connection behavior.
 *
 * Loaded from `%APPDATA%/CommClient/config.json` (Windows) or
 * `~/.config/commclient/config.json` (POSIX). Created with safe defaults
 * on first run.
 *
 * Why this file exists
 * --------------------
 * Before this module, the desktop client would:
 *   1. Always spawn its own bundled Helen-Server.exe.
 *   2. Probe localhost ports 3088/3000/3001 in arbitrary order.
 *   3. Switch serverUrl mid-session via LAN discovery callbacks.
 * Result: split brain — the renderer might end up talking to a different
 * server than the admin panel, with different DBs and JWT secrets, and
 * the user saw "disconnected" with no diagnostic.
 *
 * Now: every connection decision goes through this config. Production
 * defaults disable embedded server, LAN discovery, and auto-switching.
 * The user can opt in explicitly for development or LAN-host scenarios.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { app } from 'electron';
import { randomBytes } from 'node:crypto';
import { execSync } from 'node:child_process';

export type ClientMode = 'production' | 'development' | 'standalone';

export interface ClientConfig {
  /** Operating mode — determines which feature flags are honored. */
  mode: ClientMode;
  /** Single canonical server URL. Overrides probe lists, never auto-switched. */
  serverUrl: string;
  /** When true, Main may spawn Helen-Server.exe inside the app (standalone use). */
  allowEmbeddedServer: boolean;
  /** When true, renderer may use UDP/mDNS discovery to FIND a server (still won't switch after connection). */
  allowLanDiscovery: boolean;
  /** When true, renderer may swap serverUrl mid-session if discovery finds a different one. */
  allowAutoServerSwitch: boolean;
  /** Stable per-install identifier. Reported on every WebSocket HELLO.
   *  On Windows we replace this with the SMBIOS Machine UUID
   *  (Win32_ComputerSystemProduct.UUID) — that's the same hardware ID
   *  shown in the Settings → System → About panel, so users can recognise
   *  it. On other OSes (or if WMI fails) we fall back to a random hex
   *  string that's persisted to config.json on first launch. */
  deviceId: string;
  /** Short, human-readable suffix derived from `deviceId` — used as the
   *  Discord-style discriminator after the username (e.g. `helen#0B222F2F85`)
   *  so that two people on different machines but the same username are
   *  distinguishable to peers and searchable across federated servers. */
  deviceTag: string;
  /** Optional HMAC secret for authenticated UDP discovery (audit fix C3).
   *  When set (≥16 chars), discovery rejects unsigned UDP packets.
   *  All Helen-Server instances in the same fleet must share the same
   *  secret. Empty = accept unsigned (single-server / LAN-only mode). */
  discoverySecret?: string;
}

const DEFAULT_SERVER_URL = 'http://127.0.0.1:3000';
const CONFIG_FILENAME = 'config.json';

/** Resolve the config dir: %APPDATA%/CommClient or platform default. */
export function getConfigDir(): string {
  const appData = process.env.APPDATA
    || (process.platform === 'win32' ? null : join(process.env.HOME || '', '.config'));
  if (!appData) {
    // Last-resort fallback when APPDATA isn't set (rare; e.g. service contexts).
    return app.getPath('userData');
  }
  const dir = join(appData, 'CommClient');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return dir;
}

export function getConfigPath(): string {
  return join(getConfigDir(), CONFIG_FILENAME);
}

/**
 * Read the SMBIOS Machine UUID from Windows so the device identifier
 * survives reinstalls and matches what the user sees in
 * `Settings → System → About → Device ID`. We try wmic first because
 * it's stable across every Windows 10/11 build, then fall back to a
 * PowerShell CIM query if wmic was disabled. If both fail (non-Windows,
 * sandbox, etc.) we return null and the caller falls back to a random
 * hex string.
 */
function readWindowsMachineUUID(): string | null {
  if (process.platform !== 'win32') return null;
  // wmic — present on every supported Windows desktop
  try {
    const out = execSync('wmic csproduct get UUID /value', {
      encoding: 'utf8',
      timeout: 3000,
      windowsHide: true,
    });
    const m = out.match(/UUID=([0-9A-Fa-f-]{32,})/);
    if (m && m[1]) return m[1].toUpperCase();
  } catch { /* fall through to powershell */ }
  // PowerShell CIM — works on slim/Server SKUs that disabled wmic
  try {
    const out = execSync(
      'powershell -NoProfile -Command "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID"',
      { encoding: 'utf8', timeout: 5000, windowsHide: true },
    ).trim();
    if (/^[0-9A-Fa-f-]{32,}$/.test(out)) return out.toUpperCase();
  } catch { /* give up */ }
  return null;
}

/** Compute the 10-char tag suffix shown after the username. */
function deviceTagFromId(id: string): string {
  return id.replace(/[^0-9A-Fa-f]/g, '').slice(-10).toUpperCase();
}

function makeDefaults(): ClientConfig {
  // App is packaged → production defaults.
  // App is in dev (electron .) → development defaults.
  //
  // ``allowLanDiscovery`` defaults to TRUE in production now — without
  // it, a freshly-installed Desktop only probes 127.0.0.1:3000 and
  // shows "Local probe NOT reachable" forever when the operator runs
  // Helen-Server on a separate machine. LAN discovery uses the same
  // mDNS browse + UDP probe channels the server already advertises,
  // so it stays 100% LAN-bound.
  //
  // ``allowEmbeddedServer`` stays OFF in production: spawning a
  // server inside Desktop is appropriate for the dev workflow only
  // and would leak credentials in a packaged build.
  const isPackaged = app?.isPackaged ?? true;
  const mode: ClientMode = isPackaged ? 'production' : 'development';
  // Hardware UUID first — gives us a stable identifier matching the
  // OS-reported Device ID. Random fallback only when we genuinely can't
  // read it.
  const hwUuid = readWindowsMachineUUID();
  const id = hwUuid || randomBytes(16).toString('hex');
  return {
    mode,
    serverUrl: process.env.COMMCLIENT_SERVER_URL || DEFAULT_SERVER_URL,
    allowEmbeddedServer: !isPackaged,    // dev: yes, prod: no
    allowLanDiscovery: true,             // both modes — discover Helen-Server on LAN
    allowAutoServerSwitch: false,        // never — even in dev — auto-switching is the split-brain root cause
    deviceId: id,
    deviceTag: deviceTagFromId(id),
  };
}

/** Validate + fill in any missing keys from defaults. Returns the merged config. */
function normalize(raw: Partial<ClientConfig>): ClientConfig {
  const defaults = makeDefaults();
  const merged: ClientConfig = {
    mode: (raw.mode === 'development' || raw.mode === 'standalone') ? raw.mode : defaults.mode,
    serverUrl: typeof raw.serverUrl === 'string' && /^https?:\/\//.test(raw.serverUrl)
      ? raw.serverUrl
      : defaults.serverUrl,
    allowEmbeddedServer: typeof raw.allowEmbeddedServer === 'boolean'
      ? raw.allowEmbeddedServer
      : defaults.allowEmbeddedServer,
    allowLanDiscovery: typeof raw.allowLanDiscovery === 'boolean'
      ? raw.allowLanDiscovery
      : defaults.allowLanDiscovery,
    allowAutoServerSwitch: typeof raw.allowAutoServerSwitch === 'boolean'
      ? raw.allowAutoServerSwitch
      : defaults.allowAutoServerSwitch,
    deviceId: typeof raw.deviceId === 'string' && raw.deviceId.length >= 16
      ? raw.deviceId
      : defaults.deviceId,
    // Always recompute the tag from whichever deviceId we end up using,
    // so a hand-edited config.json can't desync the suffix from the id.
    deviceTag: '',
  };
  merged.deviceTag = deviceTagFromId(merged.deviceId);
  return merged;
}

let cached: ClientConfig | null = null;

/** Load (or create) the client config. Cached for the process lifetime. */
export function loadClientConfig(): ClientConfig {
  if (cached) return cached;

  const path = getConfigPath();
  let raw: Partial<ClientConfig> = {};
  if (existsSync(path)) {
    try {
      raw = JSON.parse(readFileSync(path, 'utf-8'));
    } catch (err) {
      console.error(`[config] failed to parse ${path}:`, (err as Error).message);
      // Fall through with empty raw → defaults applied.
    }
  }

  const config = normalize(raw);

  // Persist back so the user can see/edit the file (and so deviceId is stable).
  try {
    writeFileSync(path, JSON.stringify(config, null, 2) + '\n', 'utf-8');
  } catch (err) {
    console.error(`[config] failed to write ${path}:`, (err as Error).message);
  }

  cached = config;
  return config;
}

/** Override one field and persist. Used by setup wizards / diagnostics. */
export function updateClientConfig(patch: Partial<ClientConfig>): ClientConfig {
  const current = loadClientConfig();
  const next = normalize({ ...current, ...patch });
  cached = next;
  writeFileSync(getConfigPath(), JSON.stringify(next, null, 2) + '\n', 'utf-8');
  return next;
}

/** Parse host+port out of serverUrl. */
export function parseServerUrl(url: string): { host: string; port: number; protocol: 'http' | 'https' } {
  const m = url.match(/^(https?):\/\/([^:/]+)(?::(\d+))?/);
  if (!m) throw new Error(`Invalid serverUrl: ${url}`);
  const protocol = m[1] as 'http' | 'https';
  return {
    protocol,
    host: m[2],
    port: m[3] ? Number(m[3]) : (protocol === 'https' ? 443 : 80),
  };
}
