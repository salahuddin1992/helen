/**
 * Electron Main Process — USB-tethered iPhone detector.
 *
 * Watches the host's network interfaces for the tell-tale signature of an
 * iPhone sharing its network over USB (Personal Hotspot → USB). When the
 * user plugs in their phone and enables the hotspot, Windows enumerates a
 * new Ethernet-class adapter that hands the PC an address in the
 * ``172.20.10.0/24`` reservation Apple uses for USB tethering; the iPhone
 * itself is reachable at ``172.20.10.1``.
 *
 * We poll ``os.networkInterfaces()`` every few seconds — this is deliberate
 * over a native ``usb-detection`` dependency: it works on any Windows
 * install, doesn't need node-gyp, and captures *exactly* the state we care
 * about (a working IP route to the phone, not merely a cable inserted).
 *
 * Consumers in the renderer subscribe to the IPC event ``phone:usb-status``
 * and also pull the current snapshot via ``phone:get-usb-status``. The
 * PairPhoneDialog uses the phone-reachable host IP to regenerate its QR so
 * the pair flow works over USB without any Wi-Fi at all.
 */

import { ipcMain, BrowserWindow } from 'electron';
import { networkInterfaces, NetworkInterfaceInfo } from 'os';

// ── Types ──────────────────────────────────────────────

export interface UsbPhoneStatus {
  /** True when an interface in the iPhone USB-tether subnet is currently up. */
  connected: boolean;
  /** Windows-side IPv4 on the tether subnet (e.g. 172.20.10.2). */
  hostAddress: string | null;
  /** Presumed iPhone address — always .1 on the detected subnet. */
  phoneAddress: string | null;
  /** OS-level interface name (e.g. "Ethernet 4"). */
  interfaceName: string | null;
  /** MAC address of the adapter, useful for audit/debug. */
  mac: string | null;
  /** Epoch ms of the last state change — UI uses it to debounce flicker. */
  since: number;
}

// ── Constants ──────────────────────────────────────────

/** Apple's fixed subnet for iPhone Personal Hotspot over USB. */
const TETHER_SUBNET_PREFIX = '172.20.10.';
const POLL_INTERVAL_MS = 2_000;

// Apple's OUI space is enormous (hundreds of prefixes) and we don't need
// perfect identification — the subnet alone is a near-zero-false-positive
// signal. The MAC is captured purely for diagnostics / audit logs.

// ── State ──────────────────────────────────────────────

let current: UsbPhoneStatus = {
  connected: false,
  hostAddress: null,
  phoneAddress: null,
  interfaceName: null,
  mac: null,
  since: Date.now(),
};

let pollTimer: NodeJS.Timeout | null = null;

// ── Detection ──────────────────────────────────────────

function scanOnce(): UsbPhoneStatus {
  const ifaces = networkInterfaces();
  for (const [name, entries] of Object.entries(ifaces)) {
    if (!entries) continue;
    for (const e of entries as NetworkInterfaceInfo[]) {
      if (e.family !== 'IPv4') continue;
      if (e.internal) continue;
      if (!e.address.startsWith(TETHER_SUBNET_PREFIX)) continue;
      return {
        connected: true,
        hostAddress: e.address,
        // iPhone always assigns itself .1 on this subnet.
        phoneAddress: `${TETHER_SUBNET_PREFIX}1`,
        interfaceName: name,
        mac: e.mac || null,
        since: Date.now(),
      };
    }
  }
  return {
    connected: false,
    hostAddress: null,
    phoneAddress: null,
    interfaceName: null,
    mac: null,
    since: Date.now(),
  };
}

function statusChanged(a: UsbPhoneStatus, b: UsbPhoneStatus): boolean {
  return (
    a.connected !== b.connected ||
    a.hostAddress !== b.hostAddress ||
    a.interfaceName !== b.interfaceName
  );
}

function broadcast(status: UsbPhoneStatus): void {
  for (const win of BrowserWindow.getAllWindows()) {
    try {
      win.webContents.send('phone:usb-status', status);
    } catch {
      // Window may be torn down during send — harmless.
    }
  }
}

function tick(): void {
  const next = scanOnce();
  if (!statusChanged(current, next)) return;
  // Preserve the prior ``since`` timestamp when flags match; update on edge.
  current = next;
  broadcast(current);
}

// ── Lifecycle ──────────────────────────────────────────

export function installUsbPhoneDetect(): void {
  // Prime the state with a synchronous first scan so any renderer that
  // queries the status immediately after startup gets a real answer.
  current = scanOnce();

  pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  // Unref so this timer never prevents app quit — we're happy to be torn
  // down alongside everything else.
  pollTimer.unref?.();

  ipcMain.handle('phone:get-usb-status', () => current);
}

export function shutdownUsbPhoneDetect(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  ipcMain.removeHandler('phone:get-usb-status');
}

export function getUsbPhoneStatus(): UsbPhoneStatus {
  return current;
}
