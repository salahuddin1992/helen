/**
 * LAN-server hardening helpers for the Electron launcher (Task #5).
 *
 * This module is additive — it does NOT replace the existing
 * `startBackendServer` flow in `index.ts`. Instead it exposes helpers
 * that the launcher can opt into on a per-build basis:
 *
 *   import { buildLanServerEnv, detectPrimaryLanIp, printLanBanner }
 *     from './lanServerEnv';
 *
 *   serverProcess = spawn(exePath, [], {
 *     env: { ...process.env, ...buildLanServerEnv({ dataDir, logsDir, port: serverPort }) },
 *     ...
 *   });
 *
 *   printLanBanner(serverPort);
 *
 * Why a separate module
 * ---------------------
 *   * Keeps `index.ts` readable — the env block already has ~12 keys.
 *   * Lets the PyInstaller spec switch between `app.main:app` and
 *     `app.lan_server_app:app` without touching this file.
 *   * Isolates Node-side LAN IP enumeration (mirrors the Python
 *     `lan_ice_helper` so the two layers agree on announced IPs).
 */

import { networkInterfaces, hostname, release } from 'os';
import { join } from 'path';

// ─── LAN IP detection ───────────────────────────────────────────────────

const VIRTUAL_IFACE_HINTS = [
  'vethernet',
  'vmware',
  'virtualbox',
  'vbox',
  'hyper-v',
  'docker',
  'br-',
  'wsl',
  'loopback',
  'bluetooth',
  'teredo',
  'isatap',
  'tailscale',
  'zerotier',
  'openvpn',
  'wireguard',
  'tun',
  'tap',
];

function isVirtualIface(name: string): boolean {
  const lower = name.toLowerCase();
  return VIRTUAL_IFACE_HINTS.some((hint) => lower.includes(hint));
}

export interface LanAddress {
  iface: string;
  address: string;
  netmask: string;
  cidr: string | null;
  isVirtual: boolean;
  score: number;
}

export function enumerateLanAddresses(): LanAddress[] {
  const all: LanAddress[] = [];
  const ifs = networkInterfaces();
  for (const [name, addrs] of Object.entries(ifs)) {
    if (!addrs) continue;
    for (const addr of addrs) {
      if (addr.family !== 'IPv4') continue;
      if (addr.internal) continue;
      if (!addr.address || addr.address.startsWith('127.')) continue;

      const isVirtual = isVirtualIface(name);
      const isPrivate =
        addr.address.startsWith('10.') ||
        addr.address.startsWith('192.168.') ||
        /^172\.(1[6-9]|2[0-9]|3[0-1])\./.test(addr.address);
      const isLinkLocal = addr.address.startsWith('169.254.');

      const score =
        (isPrivate ? 10 : 5) +
        (isVirtual ? -7 : 0) +
        (isLinkLocal ? -9 : 0);

      all.push({
        iface: name,
        address: addr.address,
        netmask: addr.netmask,
        cidr: addr.cidr,
        isVirtual,
        score,
      });
    }
  }
  all.sort((a, b) => b.score - a.score);
  return all;
}

export function detectPrimaryLanIp(): string {
  const candidates = enumerateLanAddresses();
  return candidates[0]?.address ?? '127.0.0.1';
}

// ─── Env construction ───────────────────────────────────────────────────

export interface LanServerEnvOptions {
  dataDir: string;
  logsDir: string;
  port: number;
  /** Announced IP for ICE/SDP — defaults to auto-detected primary LAN IP. */
  announcedIp?: string;
  /** Disable SFU auto-launch (clients fall back to mesh-only). */
  disableSfu?: boolean;
  /** Use an external mediasoup (e.g. running on another host). */
  externalSfu?: boolean;
  /** Override for the mediasoup control port. */
  sfuControlPort?: number;
}

/**
 * Build the env overlay that the spawned Python backend inherits.
 * Keys are chosen to match what `app.core.persistent_secrets` and
 * `app.services.sfu_launcher` look at, so the two layers stay in sync.
 */
export function buildLanServerEnv(
  opts: LanServerEnvOptions,
): Record<string, string> {
  const announcedIp = opts.announcedIp || detectPrimaryLanIp();
  const env: Record<string, string> = {
    // Core
    HOST: '0.0.0.0',
    PORT: String(opts.port),
    COMMCLIENT_DATA_DIR: opts.dataDir,
    LOG_DIR: opts.logsDir,
    SQLITE_PATH: join(opts.dataDir, 'commclient.db'),
    UPLOAD_DIR: join(opts.dataDir, 'files'),

    // ICE / SDP
    ICE_ANNOUNCED_IP: announcedIp,

    // SFU
    MEDIASOUP_ANNOUNCED_IP: announcedIp,
    MEDIASOUP_CONTROL_HOST: '127.0.0.1',
    MEDIASOUP_CONTROL_PORT: String(opts.sfuControlPort ?? 4443),
  };

  if (opts.disableSfu) {
    env.COMMCLIENT_SFU_AUTOSTART_DISABLED = '1';
  }
  if (opts.externalSfu) {
    env.COMMCLIENT_SFU_EXTERNAL = '1';
  }

  return env;
}

// ─── Banner ─────────────────────────────────────────────────────────────

/**
 * Print a human-readable banner describing how remote clients should
 * connect to this server. Called once after the server is healthy.
 */
export function printLanBanner(port: number): void {
  const ips = enumerateLanAddresses().filter((a) => !a.isVirtual);
  if (ips.length === 0) ips.push({
    iface: '', address: '127.0.0.1', netmask: '',
    cidr: null, isVirtual: false, score: 0,
  });

  const banner = [
    '',
    '='.repeat(70),
    '  CommClient — LAN Server ready',
    '='.repeat(70),
    `  Host OS:  ${hostname()} (${release()})`,
    '  Clients on the same LAN can connect via:',
    ...ips.map((ip) => `     http://${ip.address}:${port}   (${ip.iface})`),
    '',
    '  Discovery: mDNS + UDP broadcast (no manual URL required on LAN).',
    '='.repeat(70),
    '',
  ].join('\n');
  console.log(banner);
}

// ─── Connect-string URL helpers ─────────────────────────────────────────

export function buildConnectUrls(port: number): string[] {
  const ips = enumerateLanAddresses().filter((a) => !a.isVirtual);
  if (ips.length === 0) return [`http://127.0.0.1:${port}`];
  return ips.map((a) => `http://${a.address}:${port}`);
}
