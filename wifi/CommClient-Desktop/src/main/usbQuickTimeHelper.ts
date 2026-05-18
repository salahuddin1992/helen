/**
 * Electron Main Process — QuickTime-over-USB iPhone helper (SCAFFOLD).
 *
 * This module is the foundation for *direct* iPhone-as-webcam ingestion over
 * USB — bypassing Safari entirely. Apple's QuickTime USB Streaming protocol
 * (reverse-engineered by the ``quicktime_video_hack`` project) works like
 * this:
 *
 *   1. Windows' Apple Mobile Device driver exposes the iPhone as a libusb
 *      device with interface class ``0xFE`` (vendor-specific) on newer iOS.
 *   2. A control transfer with ``bRequest=0x52, wValue=0x01`` flips the
 *      phone into streaming mode, which exposes two additional bulk
 *      endpoints (one IN for A/V, one OUT for config).
 *   3. A "ping" handshake over the OUT endpoint triggers the phone to start
 *      sending H.264 video + AAC audio wrapped in a custom frame format.
 *   4. We demux, hand H.264 NALUs to a decoder (ffmpeg.wasm or a native
 *      bridge), and expose the raw frames as a MediaStreamTrack back to
 *      the renderer.
 *
 * Why this is a *scaffold* for now:
 *   - Step 2 requires the user to have Apple Mobile Device Support installed
 *     (ships with iTunes/Apple Devices on Windows) and trust dialog cleared.
 *   - The ``usb`` npm package needs WinUSB binding or a userspace driver; we
 *     lazy-load it here so the app still boots on installs without native
 *     build tools.
 *   - The ffmpeg decode pipeline is non-trivial — the Wi-Fi/tether path
 *     via Safari is the pragmatic default. This helper is wired in but
 *     gated behind feature detection so it activates only when available.
 *
 * The IPC surface is stable even when the helper is a no-op: callers always
 * get ``{ supported: false }`` back and can fall back to the tether path.
 */

import { ipcMain, BrowserWindow } from 'electron';

// ── Types ──────────────────────────────────────────────

export interface UsbPhoneDevice {
  /** Unique iPhone UDID as reported by the USB serial descriptor. */
  udid: string;
  /** Human-readable product string (e.g. "iPhone"). */
  product: string;
  /** Vendor ID (Apple = 0x05AC). */
  vendorId: number;
  /** Product ID — varies by model/mode. */
  productId: number;
  /** True when the phone is currently in QuickTime streaming mode. */
  streaming: boolean;
}

export interface UsbHelperStatus {
  /** True when the native ``usb`` module loaded successfully. */
  supported: boolean;
  /** Non-null when initialization failed — suitable for UI diagnostics. */
  error: string | null;
  /** All Apple devices enumerated on the bus (may be empty). */
  devices: UsbPhoneDevice[];
  /** Epoch ms of the last enumeration pass. */
  lastScan: number;
}

// ── Constants ──────────────────────────────────────────

/** Apple's USB vendor ID — the same across every iPhone/iPad. */
const APPLE_VID = 0x05ac;

/** ``bmRequestType`` for a vendor-to-device OUT control transfer:
 *  Type=Vendor (0x40) | Recipient=Device (0x00) | Direction=OUT (0x00). */
const QT_BM_REQUEST_TYPE = 0x40;
/** ``bRequest`` for the QuickTime config endpoint. Discovered by the
 *  quicktime_video_hack project — Apple never documented this publicly. */
const QT_ACTIVATE_REQUEST = 0x52;
/** ``wValue=1`` enables streaming; ``wValue=0`` disables and restores the
 *  pre-streaming interface layout. We use 0x01 during activation. */
const QT_ACTIVATE_VALUE = 0x01;
/** Short control-transfer timeout — the phone answers instantly or never. */
const CONTROL_TIMEOUT_MS = 2_000;

/** USB interface class/subclass pair Apple assigns to the QuickTime
 *  streaming interface that appears *after* the 0x52/0x01 activation.
 *  The regular configuration interface stays class 0xFE. */
const QT_IFACE_CLASS = 0xff;
const QT_IFACE_SUBCLASS = 0x2a;

/** Bulk endpoint ring-buffer: the read pump allocates a fixed window of
 *  requests so libusb can saturate the USB bus without per-frame
 *  alloc/free overhead. */
const BULK_READ_TRANSFERS = 4;
const BULK_READ_SIZE = 64 * 1024;

// ── State ──────────────────────────────────────────────

// We deliberately use ``any`` for the usb module because it's optional at
// install time — pulling in its types would force every consumer to install
// the package too.
let _usbModule: any | null = null;
let _loadAttempted = false;
let _loadError: string | null = null;

let current: UsbHelperStatus = {
  supported: false,
  error: null,
  devices: [],
  lastScan: 0,
};

// ── Lazy native load ───────────────────────────────────

function tryLoadUsb(): any | null {
  if (_loadAttempted) return _usbModule;
  _loadAttempted = true;
  try {
     
    _usbModule = require('usb');
    return _usbModule;
  } catch (err: any) {
    // Not installed, no native build tools, or the binary is incompatible
    // with this Electron ABI — all expected on a default install. Fail
    // quiet; the tether path still works.
    _loadError = err?.message || String(err);
    _usbModule = null;
    return null;
  }
}

// ── Enumeration ────────────────────────────────────────

/** Read the UTF-16LE string descriptor at ``index`` from an *opened* device.
 *  Falls back to ``null`` on any failure — real UDID resolution is best-effort
 *  and only used for presentation. */
async function readStringDescriptor(
  device: any,
  index: number | undefined,
): Promise<string | null> {
  if (!index) return null;
  return new Promise((resolve) => {
    try {
      device.getStringDescriptor(index, (err: any, data: any) => {
        if (err || !data) return resolve(null);
        resolve(typeof data === 'string' ? data : null);
      });
    } catch {
      resolve(null);
    }
  });
}

/** Cache of UDIDs keyed by ``bus.addr`` so we don't re-open a device every
 *  scan. The cache is invalidated when enumeration no longer lists the
 *  device (unplug). */
const _udidCache: Map<string, string> = new Map();

async function enumerate(): Promise<UsbPhoneDevice[]> {
  const usb = tryLoadUsb();
  if (!usb) return [];
  try {
    const raw: any[] = usb.getDeviceList?.() || [];
    const out: UsbPhoneDevice[] = [];
    const seenKeys = new Set<string>();
    for (const d of raw) {
      const desc = d?.deviceDescriptor;
      if (!desc || desc.idVendor !== APPLE_VID) continue;
      const key = `bus${d.busNumber}.${d.deviceAddress}`;
      seenKeys.add(key);

      let udid = _udidCache.get(key) || null;
      if (!udid) {
        // Best-effort: open briefly to read the iSerialNumber descriptor.
        // On Windows without Apple Mobile Device Support this fails; we
        // fall back to the bus-address key, which still uniquely
        // identifies the device for this session.
        try {
          d.open();
          udid = await readStringDescriptor(d, desc.iSerialNumber);
          try { d.close(); } catch { /* ignore */ }
          if (udid) _udidCache.set(key, udid);
        } catch {
          try { d.close(); } catch { /* ignore */ }
        }
      }

      out.push({
        udid: udid || key,
        product: 'iPhone',
        vendorId: desc.idVendor,
        productId: desc.idProduct,
        streaming: false,
      });
    }
    // Drop cache entries for devices that vanished.
    for (const cached of Array.from(_udidCache.keys())) {
      if (!seenKeys.has(cached)) _udidCache.delete(cached);
    }
    return out;
  } catch (err) {
    console.warn('[usbQuickTimeHelper] enumerate failed:', (err as Error).message);
    return [];
  }
}

async function scanOnce(): Promise<UsbHelperStatus> {
  const usb = tryLoadUsb();
  return {
    supported: !!usb,
    error: _loadError,
    devices: await enumerate(),
    lastScan: Date.now(),
  };
}

function broadcast(status: UsbHelperStatus): void {
  for (const win of BrowserWindow.getAllWindows()) {
    try {
      win.webContents.send('phone:qt-status', status);
    } catch {
      /* window torn down — harmless */
    }
  }
}

// ── Activation stub ────────────────────────────────────

/** Resolve an opened device handle for a given UDID (or the bus-address
 *  fallback key). Returns null if no live device matches. The caller owns
 *  ``close()`` on success. */
function openDeviceByUdid(usb: any, udid: string): any | null {
  const raw: any[] = usb.getDeviceList?.() || [];
  for (const d of raw) {
    const desc = d?.deviceDescriptor;
    if (!desc || desc.idVendor !== APPLE_VID) continue;
    const key = `bus${d.busNumber}.${d.deviceAddress}`;
    const matches = udid === key || _udidCache.get(key) === udid;
    if (!matches) continue;
    try {
      d.open();
      return d;
    } catch (err) {
      console.warn('[usbQuickTimeHelper] device.open failed:', (err as Error).message);
      return null;
    }
  }
  return null;
}

/** Perform the QT activation control transfer. Resolves with the number of
 *  bytes written (0 is fine — this OUT transfer carries no data payload). */
function sendActivateControl(device: any): Promise<number> {
  return new Promise((resolve, reject) => {
    try {
      // node-usb signature:
      // controlTransfer(bmRequestType, bRequest, wValue, wIndex, data_or_length, cb)
      // ``data_or_length`` for an OUT transfer must be a Buffer.
      device.timeout = CONTROL_TIMEOUT_MS;
      device.controlTransfer(
        QT_BM_REQUEST_TYPE,
        QT_ACTIVATE_REQUEST,
        QT_ACTIVATE_VALUE,
        0x0000,
        Buffer.alloc(0),
        (err: any, data: any) => {
          if (err) return reject(err);
          resolve(Array.isArray(data) || Buffer.isBuffer(data) ? data.length : 0);
        },
      );
    } catch (err) {
      reject(err);
    }
  });
}

// ── Streaming pipeline (post-activation) ──────────────

interface StreamingSession {
  udid: string;
  device: any;
  iface: any;
  inEndpoint: any;
  outEndpoint: any;
  // Cleanup closures registered during setup, flushed by stopStreaming().
  teardown: Array<() => void>;
}

const _streaming: Map<string, StreamingSession> = new Map();

/** Locate the QT streaming interface on a re-enumerated device. After the
 *  0x52/0x01 activation the phone exposes a second configuration with an
 *  interface whose ``bInterfaceClass`` is 0xFF and ``bInterfaceSubClass``
 *  is 0x2A — that's the one carrying the audio/video bulk endpoints. */
function findQtInterface(device: any): any | null {
  try {
    const interfaces: any[] = device.interfaces || [];
    for (const iface of interfaces) {
      const desc = iface.descriptor;
      if (!desc) continue;
      if (desc.bInterfaceClass === QT_IFACE_CLASS && desc.bInterfaceSubClass === QT_IFACE_SUBCLASS) {
        return iface;
      }
    }
  } catch (err) {
    console.warn('[usbQuickTimeHelper] findQtInterface failed:', (err as Error).message);
  }
  return null;
}

/** Pick the IN/OUT bulk endpoints on a claimed interface. Apple's QT
 *  layout has exactly one of each; ``direction === 'in'`` carries the
 *  multiplexed A/V payload, OUT carries the ping/config frames. */
function pickBulkEndpoints(iface: any): { inEp: any; outEp: any } | null {
  try {
    let inEp: any = null;
    let outEp: any = null;
    for (const ep of iface.endpoints || []) {
      if (ep.transferType !== 2 /* LIBUSB_TRANSFER_TYPE_BULK */) continue;
      if (ep.direction === 'in' && !inEp) inEp = ep;
      else if (ep.direction === 'out' && !outEp) outEp = ep;
    }
    if (inEp && outEp) return { inEp, outEp };
  } catch (err) {
    console.warn('[usbQuickTimeHelper] pickBulkEndpoints failed:', (err as Error).message);
  }
  return null;
}

/** Begin the bulk-IN read pump. Each incoming packet is forwarded to the
 *  renderer over ``phone:qt-frame`` (base64 payload) so a worker there can
 *  demux the QT frame format, extract NALUs, and feed them to the decoder
 *  without blocking the main thread. */
function startBulkReadPump(session: StreamingSession): void {
  const ep = session.inEndpoint;
  try {
    ep.startPoll(BULK_READ_TRANSFERS, BULK_READ_SIZE);
  } catch (err) {
    console.warn('[usbQuickTimeHelper] startPoll failed:', (err as Error).message);
    return;
  }
  const onData = (buf: Buffer) => {
    if (!buf || buf.length === 0) return;
    for (const win of BrowserWindow.getAllWindows()) {
      try {
        // Node Buffers are structured-cloneable, so the renderer receives
        // a Uint8Array directly. No base64 round-trip needed.
        win.webContents.send('phone:qt-frame', {
          udid: session.udid,
          data: buf,
        });
      } catch {
        /* window gone — harmless */
      }
    }
  };
  const onError = (err: any) => {
    // EAGAIN-ish errors are fine — just transient. Log louder ones.
    if (err?.errno !== -7 /* LIBUSB_ERROR_TIMEOUT */) {
      console.warn('[usbQuickTimeHelper] bulk read error:', err?.message || err);
    }
  };
  ep.on('data', onData);
  ep.on('error', onError);
  session.teardown.push(() => {
    try { ep.off('data', onData); } catch { /* ignore */ }
    try { ep.off('error', onError); } catch { /* ignore */ }
    try { ep.stopPoll(); } catch { /* ignore */ }
  });
}

/** Claim the QT interface on an already-activated phone and wire up the
 *  bulk pump. Must be called after the device has re-enumerated. */
async function claimAndPumpStream(device: any, udid: string): Promise<StreamingSession | null> {
  const iface = findQtInterface(device);
  if (!iface) return null;
  try {
    if (iface.isKernelDriverActive()) {
      try { iface.detachKernelDriver(); } catch { /* Windows has none */ }
    }
  } catch { /* ignore */ }
  try {
    iface.claim();
  } catch (err) {
    console.warn('[usbQuickTimeHelper] interface.claim failed:', (err as Error).message);
    return null;
  }
  const endpoints = pickBulkEndpoints(iface);
  if (!endpoints) {
    try { iface.release(() => { /* noop */ }); } catch { /* ignore */ }
    return null;
  }
  const session: StreamingSession = {
    udid,
    device,
    iface,
    inEndpoint: endpoints.inEp,
    outEndpoint: endpoints.outEp,
    teardown: [],
  };
  startBulkReadPump(session);
  session.teardown.push(() => {
    try { iface.release(true, () => { /* noop */ }); } catch { /* ignore */ }
    try { device.close(); } catch { /* ignore */ }
  });
  _streaming.set(udid, session);
  return session;
}

function stopStreaming(udid: string): void {
  const session = _streaming.get(udid);
  if (!session) return;
  _streaming.delete(udid);
  for (const fn of session.teardown.reverse()) {
    try { fn(); } catch { /* ignore */ }
  }
}

/** Write a buffer to the bulk-OUT endpoint for an active streaming session.
 *  Used by the renderer's QT protocol state machine to reply to pings and
 *  drive the async/sync handshake dance that keeps A/V flowing. */
function sendBulkOut(udid: string, data: Uint8Array): Promise<number> {
  return new Promise((resolve, reject) => {
    const session = _streaming.get(udid);
    if (!session) return reject(new Error('no_session'));
    try {
      // node-usb expects a Buffer for OUT transfers. When the renderer sends
      // a Uint8Array over IPC we re-wrap it without copying.
      const buf = Buffer.isBuffer(data) ? data : Buffer.from(data.buffer, data.byteOffset, data.byteLength);
      session.outEndpoint.transfer(buf, (err: any) => {
        if (err) return reject(err);
        resolve(buf.length);
      });
    } catch (err) {
      reject(err);
    }
  });
}

/**
 * Flip a specific iPhone into QuickTime streaming mode.
 *
 * Phase 1 (this implementation): send the 0x52/0x01 vendor control transfer.
 * The phone responds by re-enumerating with an extra vendor-specific
 * interface (class 0xFF, subclass 0x2A) that exposes the A/V bulk
 * endpoints. After that, QT-aware userspace would claim the interface and
 * start the bulk-read pump.
 *
 * Phase 2 (TODO — decode pipeline): find the new interface after
 * re-enumeration, claim it, drive the ping handshake on the OUT endpoint,
 * demux the H.264 NALUs + AAC packets coming out of the IN endpoint, and
 * hand decoded frames to a ``MediaStreamTrackGenerator`` so the renderer
 * can consume them through the normal virtual-source path.
 *
 * We return ``true`` as soon as the activation control transfer is
 * acknowledged — the pipeline buildout in phase 2 is asynchronous and
 * reports its own progress over ``phone:qt-status`` broadcasts.
 */
/** Wait for a device matching ``udid`` to reappear on the bus with the QT
 *  interface present. Polls at 250 ms for up to 5 s — matches the typical
 *  re-enumeration window observed by the quicktime_video_hack project. */
async function waitForReEnumeration(usb: any, udid: string, timeoutMs = 5_000): Promise<any | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const dev = openDeviceByUdid(usb, udid);
    if (dev) {
      if (findQtInterface(dev)) return dev;
      try { dev.close(); } catch { /* ignore */ }
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return null;
}

async function activateQuickTimeStreaming(udid: string): Promise<boolean> {
  const usb = tryLoadUsb();
  if (!usb) return false;
  if (_streaming.has(udid)) {
    // Already streaming — treat as idempotent success so the renderer's
    // retry logic doesn't tear down a working session.
    return true;
  }
  const device = openDeviceByUdid(usb, udid);
  if (!device) {
    console.warn('[usbQuickTimeHelper] activate: device not found', { udid });
    return false;
  }

  // 1) Fire the vendor control transfer; close our pre-activation handle
  //    immediately so the kernel can reconfigure the device cleanly.
  try {
    await sendActivateControl(device);
  } catch (err) {
    try { device.close(); } catch { /* ignore */ }
    console.warn('[usbQuickTimeHelper] activate control transfer failed:',
      (err as Error).message);
    return false;
  }
  try { device.close(); } catch { /* ignore */ }

  // 2) Wait for the phone to reappear with the QT interface, then claim it
  //    and start the bulk-in read pump. The decoder wiring happens in the
  //    renderer — the main process just forwards packets.
  const reopened = await waitForReEnumeration(usb, udid);
  if (!reopened) {
    console.warn('[usbQuickTimeHelper] activation ack but no re-enumeration', { udid });
    return false;
  }
  const session = await claimAndPumpStream(reopened, udid);
  if (!session) {
    try { reopened.close(); } catch { /* ignore */ }
    return false;
  }

  // 3) Refresh the renderer-visible status.
  scanOnce().then((status) => {
    // Mark streaming=true for this UDID so the UI knows not to re-offer.
    for (const d of status.devices) {
      if (d.udid === udid) d.streaming = true;
    }
    current = status;
    broadcast(current);
  }).catch(() => {});

  return true;
}

// ── Lifecycle ──────────────────────────────────────────

let scanTimer: NodeJS.Timeout | null = null;
const SCAN_INTERVAL_MS = 5_000;

export function installUsbQuickTimeHelper(): void {
  // Kick off the first scan immediately but don't block install() on it —
  // renderers that query before the promise settles get the initial empty
  // status and then the real snapshot via the ``phone:qt-status`` event.
  scanOnce().then((status) => {
    current = status;
    broadcast(current);
  }).catch((err) => {
    console.warn('[usbQuickTimeHelper] initial scan failed:', (err as Error).message);
  });

  scanTimer = setInterval(async () => {
    try {
      const next = await scanOnce();
      const changed =
        next.supported !== current.supported ||
        next.devices.length !== current.devices.length ||
        next.devices.some((d, i) => d.udid !== current.devices[i]?.udid);
      current = next;
      if (changed) broadcast(current);
    } catch (err) {
      console.warn('[usbQuickTimeHelper] scan tick failed:', (err as Error).message);
    }
  }, SCAN_INTERVAL_MS);
  scanTimer.unref?.();

  ipcMain.handle('phone:qt-get-status', () => current);
  ipcMain.handle('phone:qt-activate', async (_ev, udid: string) => {
    if (typeof udid !== 'string' || !udid) return { ok: false, error: 'bad_udid' };
    try {
      const ok = await activateQuickTimeStreaming(udid);
      return { ok };
    } catch (err) {
      return { ok: false, error: (err as Error).message };
    }
  });
  ipcMain.handle('phone:qt-stop', (_ev, udid: string) => {
    if (typeof udid !== 'string' || !udid) return { ok: false };
    stopStreaming(udid);
    return { ok: true };
  });
  ipcMain.handle('phone:qt-send', async (_ev, udid: string, data: Uint8Array | ArrayBuffer) => {
    if (typeof udid !== 'string' || !udid) return { ok: false, error: 'bad_udid' };
    try {
      const bytes = data instanceof Uint8Array
        ? data
        : new Uint8Array(data as ArrayBuffer);
      const n = await sendBulkOut(udid, bytes);
      return { ok: true, bytes: n };
    } catch (err) {
      return { ok: false, error: (err as Error).message };
    }
  });
}

export function shutdownUsbQuickTimeHelper(): void {
  if (scanTimer) {
    clearInterval(scanTimer);
    scanTimer = null;
  }
  // Cleanly stop every active bulk pump before we let the interface/device
  // handles fall out of scope — otherwise the OS holds the resources until
  // the process dies and the phone's streaming mode never gets a clean
  // shutdown, forcing the user to replug.
  for (const udid of Array.from(_streaming.keys())) {
    stopStreaming(udid);
  }
  ipcMain.removeHandler('phone:qt-get-status');
  ipcMain.removeHandler('phone:qt-activate');
  ipcMain.removeHandler('phone:qt-stop');
  ipcMain.removeHandler('phone:qt-send');
}

export function getUsbQuickTimeStatus(): UsbHelperStatus {
  return current;
}
