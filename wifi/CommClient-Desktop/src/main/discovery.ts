/**
 * LAN Server Discovery — Electron Main Process Module
 *
 * Listens for CommClient server UDP broadcasts on port 41234 and provides
 * discovered server list to the renderer process via IPC.
 *
 * Discovery Protocol:
 *   1. UDP Listener: binds to 0.0.0.0:41234, receives JSON broadcast packets
 *   2. HTTP Verification: validates each discovered server via GET /api/discovery
 *   3. Deduplication: servers tracked by server_id, stale entries expire after 15s
 *   4. Multi-server: supports multiple servers on one LAN, ranked by uptime + users
 *
 * Architecture:
 *   Main process owns the UDP socket (Node.js dgram — not available in renderer).
 *   Results are pushed to renderer via IPC event 'discovery:servers-updated'.
 *   Renderer can also pull via IPC invoke 'discovery:get-servers'.
 */

import { createSocket, Socket } from 'dgram';
import * as net from 'net';
import { ipcMain, BrowserWindow } from 'electron';
import * as http from 'http';
import * as dns from 'dns';
import { networkInterfaces } from 'os';
import { LanConnectivityOrchestrator, MethodId } from './lan-orchestrator';

// ── Types ─────────────────────────────────────────────────

export interface DiscoveredServer {
  server_id: string;
  name: string;
  host: string;
  port: number;
  version: string;
  uptime: number;
  users_online: number;
  protocol: string;
  url: string;           // computed: `${protocol}://${host}:${port}`
  verified: boolean;     // true after HTTP /api/discovery confirms reachability
  last_seen: number;     // Date.now() timestamp
  discovery_method: 'udp' | 'mdns' | 'manual' | 'active_scan';
  // Round-trip time of the most recent /api/discovery probe, in ms.
  // null = never measured or last probe failed. Used to rank "nearest" server.
  rtt_ms: number | null;
}

// ── Constants ─────────────────────────────────────────────

const DISCOVERY_PORT = 41234;
const STALE_TIMEOUT_MS = 15_000;       // Remove server if no broadcast for 15s
const VERIFY_INTERVAL_MS = 10_000;     // Re-verify servers every 10s
const CLEANUP_INTERVAL_MS = 5_000;     // Check for stale entries every 5s
const VERIFY_TIMEOUT_MS = 3_000;       // HTTP verification request timeout
const NETWORK_POLL_MS = 3_000;         // Poll network interfaces every 3s for changes
const RECONNECT_DELAY_MS = 2_000;      // Delay before restarting discovery after network change

// Active-scan fallback tuning. Triggered manually or after no UDP broadcast
// arrives within AUTO_ESCALATE_MS.
// Port set scanned on every TCP probe. Helen-Server's run.py auto-picks
// a free port from 3000–3010, but operators frequently override to 3088
// (the documented "production" LAN port that doesn't clash with React/
// Vite dev servers). Without 3088 here, a server bound there is invisible
// to discovery — symptom: client shows "disconnected" forever.
const SCAN_PORTS = [3088, 3000, 3001, 3002, 3003, 3010, 8080, 8088] as const;
// Bumped from 64 → 256: a /24 × 8 ports = 2032 targets; at 64 we
// completed in ~10 s, at 256 we complete in ~3 s on the same hardware
// and the OS handles the extra ephemeral sockets without trouble.
const SCAN_CONCURRENCY = 256;
// Bumped down 300 → 200 ms: most LAN RTTs are <50 ms; 200 ms is enough
// margin for slow hosts but cuts the worst-case wait time by 1/3.
const SCAN_PROBE_TIMEOUT_MS = 200;
// Don't re-scan more than once every COOLDOWN_MS — repeated triggers
// during transient UDP failures used to trigger overlapping scans.
const SCAN_COOLDOWN_MS = 15_000;
let _lastScanFinishedAt = 0;
const AUTO_ESCALATE_MS = 5_000;

// ── State ─────────────────────────────────────────────────

const servers = new Map<string, DiscoveredServer>();
let udpSocket: Socket | null = null;
let cleanupTimer: NodeJS.Timeout | null = null;
let verifyTimer: NodeJS.Timeout | null = null;
let networkWatchTimer: NodeJS.Timeout | null = null;
let isListening = false;
let lastNetworkFingerprint = '';        // Tracks network interface changes
let reconnectAttempts = 0;

// ── UDP Broadcast Listener ────────────────────────────────

// Audit fix C3: UDP discovery HMAC verification.
//
// Without this, anyone on the LAN can spoof a Helen-Server announcement
// and the client treats them as a trusted server (auto-connect ships
// the bearer token there).
//
// Server signs each broadcast with HMAC-SHA256 of (timestamp + server_id
// + host + port) using the shared `HELEN_DISCOVERY_SECRET`. Client
// verifies before accepting the announcement.
//
// `HELEN_DISCOVERY_SECRET` is read from the central client config —
// operators provision the same secret on every server in their fleet.
// When the secret is empty, we accept unsigned packets but emit a
// console warning. This keeps single-server LAN deploys frictionless
// while letting fleets opt into authenticated discovery.
import { createHmac, timingSafeEqual } from 'crypto';

let _discoverySecret: string | null = null;
let _warnedNoSecret = false;

export function setDiscoverySecret(secret: string | null | undefined): void {
  _discoverySecret = (secret && secret.length >= 16) ? secret : null;
}

function _verifyDiscoveryHmac(data: any): boolean {
  if (!_discoverySecret) {
    if (!_warnedNoSecret) {
      _warnedNoSecret = true;
      console.warn(
        '[Discovery] HELEN_DISCOVERY_SECRET not configured — accepting ' +
        'unsigned UDP announcements. Multi-server LAN deployments should ' +
        'set the same secret on every Helen instance via clientConfig.'
      );
    }
    return true;
  }
  const sig = String(data.sig || '');
  const ts = Number(data.ts || 0);
  if (!sig || !ts) return false;
  // Reject packets with timestamps more than 60s skewed from now —
  // protects against replay of captured signed announcements.
  const skew = Math.abs(Date.now() / 1000 - ts);
  if (skew > 60) return false;

  const payload = `${ts}|${data.server_id}|${data.host}|${data.port}`;
  const expected = createHmac('sha256', _discoverySecret)
    .update(payload)
    .digest('hex');
  try {
    return timingSafeEqual(
      Buffer.from(sig, 'hex'),
      Buffer.from(expected, 'hex'),
    );
  } catch {
    return false;
  }
}

function parseDiscoveryPacket(msg: Buffer, rinfo: { address: string }): DiscoveredServer | null {
  try {
    const raw = msg.toString('utf-8');
    const data = JSON.parse(raw);

    if (data.type !== 'commclient-server') return null;
    if (!data.server_id || !data.host || !data.port) return null;
    if (!_verifyDiscoveryHmac(data)) {
      console.warn('[Discovery] rejected UDP packet — invalid signature', {
        from: rinfo.address, server_id: String(data.server_id).slice(0, 12),
      });
      return null;
    }

    // Use the source IP from the UDP packet if the broadcast claims localhost
    const host = (data.host === '127.0.0.1' || data.host === '0.0.0.0')
      ? rinfo.address
      : data.host;

    const protocol = data.protocol || 'http';

    return {
      server_id: data.server_id,
      name: data.name || 'CommClient Server',
      host,
      port: data.port,
      version: data.version || '1.0.0',
      uptime: data.uptime || 0,
      users_online: data.users_online || 0,
      protocol,
      url: `${protocol}://${host}:${data.port}`,
      verified: false,
      last_seen: Date.now(),
      discovery_method: 'udp',
      rtt_ms: null,
    };
  } catch {
    return null;
  }
}

function onUDPMessage(msg: Buffer, rinfo: { address: string; port: number }): void {
  const server = parseDiscoveryPacket(msg, rinfo);
  if (!server) return;

  const existing = servers.get(server.server_id);
  if (existing) {
    // Update with fresh data, keep verified status
    existing.host = server.host;
    existing.port = server.port;
    existing.url = server.url;
    existing.uptime = server.uptime;
    existing.users_online = server.users_online;
    existing.last_seen = Date.now();
    existing.name = server.name;
  } else {
    servers.set(server.server_id, server);
    // Immediately verify new server
    verifyServer(server);
  }

  // Wake any findByServerCode() caller waiting on this exact id.
  const matched = pendingCodeWaiters.filter((w) => w.code === server.server_id);
  if (matched.length > 0) {
    const entry = servers.get(server.server_id)!;
    for (const w of matched) w.resolve(entry);
  }

  broadcastToRenderer();
}

// ── HTTP Verification ─────────────────────────────────────

function verifyServer(server: DiscoveredServer): Promise<void> {
  return new Promise((resolve) => {
    const url = `${server.url}/api/discovery`;
    const startedAt = Date.now();
    const req = http.get(url, { timeout: VERIFY_TIMEOUT_MS }, (res) => {
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try {
          const data = JSON.parse(body);
          if (data.type === 'commclient-server' && data.server_id === server.server_id) {
            server.verified = true;
            server.name = data.name || server.name;
            server.users_online = data.users_online ?? server.users_online;
            server.uptime = data.uptime ?? server.uptime;
            // Smooth RTT a bit so a single spike doesn't flip "nearest".
            const sample = Date.now() - startedAt;
            server.rtt_ms = server.rtt_ms == null
              ? sample
              : Math.round(server.rtt_ms * 0.6 + sample * 0.4);
            broadcastToRenderer();
          }
        } catch {}
        resolve();
      });
    });

    req.on('error', () => {
      server.verified = false;
      server.rtt_ms = null;
      resolve();
    });

    req.on('timeout', () => {
      req.destroy();
      server.verified = false;
      server.rtt_ms = null;
      resolve();
    });
  });
}

function verifyAllServers(): void {
  for (const server of servers.values()) {
    verifyServer(server);
  }
}

// ── 64-char Server Code Lookup ────────────────────────────
// The server broadcasts its 64-char alphanumeric server_id on the UDP bus.
// This helper lets the renderer paste that code and get back a reachable
// server entry: we first scan already-known servers, then (if nothing
// matches) we subscribe for a short window waiting for a broadcast whose
// server_id matches. LAN-only by design — there's no global registry.

const SERVER_CODE_RE = /^[A-Za-z0-9]{64}$/;
const pendingCodeWaiters: Array<{
  code: string;
  resolve: (server: DiscoveredServer) => void;
}> = [];

export function isValidServerCode(code: string): boolean {
  return SERVER_CODE_RE.test(code);
}

async function findByServerCode(
  code: string,
  timeoutMs = 8_000,
): Promise<DiscoveredServer | null> {
  if (!isValidServerCode(code)) return null;

  // 1) Already discovered?
  const existing = servers.get(code);
  if (existing) {
    if (!existing.verified) await verifyServer(existing);
    return existing;
  }

  // 2) Wait for a matching broadcast.
  return new Promise<DiscoveredServer | null>((resolve) => {
    const waiter = {
      code,
      resolve: (server: DiscoveredServer) => {
        clearTimeout(timer);
        const idx = pendingCodeWaiters.indexOf(waiter);
        if (idx >= 0) pendingCodeWaiters.splice(idx, 1);
        resolve(server);
      },
    };
    pendingCodeWaiters.push(waiter);
    const timer = setTimeout(() => {
      const idx = pendingCodeWaiters.indexOf(waiter);
      if (idx >= 0) pendingCodeWaiters.splice(idx, 1);
      resolve(null);
    }, timeoutMs);
  });
}

// ── Manual Server Addition ────────────────────────────────

async function addManualServer(url: string): Promise<DiscoveredServer | null> {
  try {
    const parsed = new URL(url);
    // Defense-in-depth: only http(s) URLs are reachable Helen servers.
    // Without this guard, a malicious caller (or a typo) could pass
    // file://, javascript:, or data: schemes through to downstream
    // probes. Helen's HTTP probe wouldn't actually fetch them, but
    // rejecting upfront keeps the surface clean.
    const proto = parsed.protocol.replace(':', '').toLowerCase();
    if (proto !== 'http' && proto !== 'https') {
      console.warn('[Discovery] addManualServer rejected non-http(s) protocol:', proto);
      return null;
    }
    const host = parsed.hostname;
    if (!host) {
      console.warn('[Discovery] addManualServer rejected URL without host');
      return null;
    }
    const port = parseInt(parsed.port || '3000', 10);
    const protocol = proto;

    // Check if we already have this host:port
    for (const s of servers.values()) {
      if (s.host === host && s.port === port) {
        return s;
      }
    }

    const manual: DiscoveredServer = {
      server_id: `manual-${host}-${port}`,
      name: 'CommClient Server',
      host,
      port,
      version: '1.0.0',
      uptime: 0,
      users_online: 0,
      protocol,
      url: `${protocol}://${host}:${port}`,
      verified: false,
      last_seen: Date.now(),
      discovery_method: 'manual',
      rtt_ms: null,
    };

    // Verify via HTTP
    await verifyServer(manual);

    if (manual.verified) {
      servers.set(manual.server_id, manual);
      broadcastToRenderer();
      return manual;
    }

    return null;
  } catch {
    return null;
  }
}

// ── Stale Entry Cleanup ───────────────────────────────────

function cleanupStaleServers(): void {
  const now = Date.now();
  let changed = false;

  for (const [id, server] of servers.entries()) {
    // Manual entries don't expire from lack of UDP broadcasts
    if (server.discovery_method === 'manual') continue;

    if (now - server.last_seen > STALE_TIMEOUT_MS) {
      servers.delete(id);
      changed = true;
    }
  }

  if (changed) broadcastToRenderer();
}

// ── IPC Bridge to Renderer ────────────────────────────────

function broadcastToRenderer(): void {
  const list = getServerList();
  for (const win of BrowserWindow.getAllWindows()) {
    try {
      win.webContents.send('discovery:servers-updated', list);
    } catch {}
  }
}

function getServerList(): DiscoveredServer[] {
  const list = Array.from(servers.values());

  // Sort: verified first, then "nearest" by RTT (lowest wins — this is what
  // makes the client auto-pick the closest server on the LAN), then uptime
  // as a tie-breaker when two servers are within 5 ms of each other.
  list.sort((a, b) => {
    if (a.verified !== b.verified) return a.verified ? -1 : 1;
    const ar = a.rtt_ms == null ? Number.POSITIVE_INFINITY : a.rtt_ms;
    const br = b.rtt_ms == null ? Number.POSITIVE_INFINITY : b.rtt_ms;
    if (Math.abs(ar - br) > 5) return ar - br;
    if (a.uptime !== b.uptime) return b.uptime - a.uptime;
    return b.users_online - a.users_online;
  });

  return list;
}

function getBestServer(): DiscoveredServer | null {
  const list = getServerList();
  return list.find((s) => s.verified) || list[0] || null;
}

// ── Network Change Detection ─────────────────────────────

/**
 * Compute a fingerprint of all active network interfaces.
 * When this changes, the network topology has changed (WiFi reconnect,
 * cable plugged/unplugged, VPN connected/disconnected, etc.).
 */
function getNetworkFingerprint(): string {
  const ifaces = networkInterfaces();
  const parts: string[] = [];

  for (const [name, addrs] of Object.entries(ifaces)) {
    if (!addrs) continue;
    for (const addr of addrs) {
      if (addr.internal) continue;
      if (addr.family === 'IPv4') {
        parts.push(`${name}:${addr.address}/${addr.netmask}`);
      }
    }
  }

  parts.sort();
  return parts.join('|');
}

/**
 * Poll network interfaces and detect changes.
 * When a change is detected:
 *   1. If we lost all interfaces → network is down, notify renderer
 *   2. If interfaces changed → WiFi switch or reconnect, restart discovery
 *   3. If interfaces returned after being empty → network is back, restart discovery
 */
function checkNetworkChange(): void {
  const current = getNetworkFingerprint();

  if (current === lastNetworkFingerprint) return;

  const hadNetwork = lastNetworkFingerprint.length > 0;
  const hasNetwork = current.length > 0;
  lastNetworkFingerprint = current;

  if (!hasNetwork) {
    // Network went down — all interfaces gone
    console.log('[Discovery] Network down — all interfaces lost');
    notifyNetworkStatus('offline');

    // Mark all servers as unverified
    for (const server of servers.values()) {
      server.verified = false;
    }
    broadcastToRenderer();
    return;
  }

  if (hadNetwork || !hadNetwork) {
    // Network changed or came back — restart discovery
    console.log('[Discovery] Network change detected, restarting discovery...');
    reconnectAttempts++;
    notifyNetworkStatus('reconnecting');

    // Close existing socket and restart after a brief delay
    // (allows the OS network stack to fully stabilize)
    if (udpSocket) {
      try { udpSocket.close(); } catch {}
      udpSocket = null;
      isListening = false;
    }

    // Clear stale server verifications
    for (const server of servers.values()) {
      server.verified = false;
    }
    broadcastToRenderer();

    setTimeout(() => {
      startUDPListener();
      // Immediately re-verify all known servers on the new network
      verifyAllServers();
      // If previously known servers respond, notify renderer
      setTimeout(() => {
        const best = getBestServer();
        if (best?.verified) {
          notifyNetworkStatus('online');
          reconnectAttempts = 0;
        }
      }, VERIFY_TIMEOUT_MS + 500);
    }, RECONNECT_DELAY_MS);
  }
}

/**
 * Push network status change to all renderer windows.
 * The renderer can use this to show/hide overlays.
 */
function notifyNetworkStatus(status: 'online' | 'offline' | 'reconnecting'): void {
  for (const win of BrowserWindow.getAllWindows()) {
    try {
      win.webContents.send('discovery:network-status', { status, attempt: reconnectAttempts });
    } catch {}
  }
}

function startNetworkWatcher(): void {
  if (networkWatchTimer) return;
  lastNetworkFingerprint = getNetworkFingerprint();
  networkWatchTimer = setInterval(checkNetworkChange, NETWORK_POLL_MS);
  console.log('[Discovery] Network watcher started');
}

function stopNetworkWatcher(): void {
  if (networkWatchTimer) {
    clearInterval(networkWatchTimer);
    networkWatchTimer = null;
  }
}

// ── UDP Listener (extracted for restart capability) ──────

function startUDPListener(): void {
  if (udpSocket) {
    try { udpSocket.close(); } catch {}
    udpSocket = null;
  }

  try {
    udpSocket = createSocket({ type: 'udp4', reuseAddr: true });

    udpSocket.on('message', onUDPMessage);

    udpSocket.on('error', (err) => {
      console.error('[Discovery] UDP socket error:', err.message);
      udpSocket?.close();
      udpSocket = null;
      isListening = false;
      // Auto-recover: try rebinding after delay
      setTimeout(() => {
        if (!udpSocket) startUDPListener();
      }, 3000);
    });

    udpSocket.on('listening', () => {
      const addr = udpSocket!.address();
      console.log(`[Discovery] Listening for broadcasts on ${addr.address}:${addr.port}`);
      isListening = true;
    });

    udpSocket.bind(DISCOVERY_PORT, '0.0.0.0');
  } catch (err: any) {
    console.error('[Discovery] Failed to bind UDP socket:', err.message);
    isListening = false;
  }
}

// ── Active LAN scan (mandatory-connection fallback) ──────────
//
// When UDP broadcast is dropped (corporate WiFi, guest networks, multicast
// filtering), passive discovery returns nothing. This scanner enumerates
// every host on each local /24 and probes TCP on the canonical Helen ports.
// Hits are then verified via /api/discovery and added to the server map
// as if they had been discovered over UDP.

function getLocalSubnets(): string[] {
  const prefixes = new Set<string>();
  const ifaces = networkInterfaces();
  for (const name of Object.keys(ifaces)) {
    const entries = ifaces[name];
    if (!entries) continue;
    for (const e of entries) {
      if (e.family !== 'IPv4' || e.internal) continue;
      if (e.address.startsWith('169.254.')) continue; // skip APIPA
      const parts = e.address.split('.');
      if (parts.length !== 4) continue;
      prefixes.add(parts.slice(0, 3).join('.') + '.');
    }
  }
  return [...prefixes];
}

/**
 * Verify a resolved IP is on the local network (RFC1918 + link-local +
 * loopback + CGNAT). Used to gate `helen.local` resolution so a hostile
 * mDNS/DNS responder can't redirect us to a public attacker host.
 */
function _isLanIp(addr: string): boolean {
  if (!addr) return false;
  if (addr === '127.0.0.1' || addr === '::1') return true;
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^169\.254\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  return false;
}

/**
 * Resolve `helen.local` via DNS and return the address ONLY if it's on
 * the local network. Returns null if resolution fails or yields a public
 * IP (which is treated as a hijack and logged).
 */
function _resolveHelenLocalLan(): Promise<string | null> {
  return new Promise((resolve) => {
    dns.lookup('helen.local', { family: 4 }, (err, address) => {
      if (err) { resolve(null); return; }
      if (!_isLanIp(address)) {
        console.warn(`[discovery] helen.local resolved to non-LAN address (${address}); rejecting.`);
        resolve(null);
        return;
      }
      resolve(address);
    });
  });
}

function probeTcp(host: string, port: number, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch {}
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => finish(true));
    sock.once('timeout', () => finish(false));
    sock.once('error', () => finish(false));
    sock.connect(port, host);
  });
}

function identifyHelen(host: string, port: number): Promise<any | null> {
  return new Promise((resolve) => {
    const req = http.get(
      { host, port, path: '/api/discovery', timeout: VERIFY_TIMEOUT_MS },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume(); resolve(null); return;
        }
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', () => {
          try {
            const payload = JSON.parse(body);
            if (payload.type !== 'commclient-server') { resolve(null); return; }
            resolve(payload);
          } catch { resolve(null); }
        });
      },
    );
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

async function runLimited<T>(items: T[], limit: number, fn: (x: T) => Promise<void>): Promise<void> {
  let i = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const idx = i++;
      if (idx >= items.length) return;
      try { await fn(items[idx]); } catch {}
    }
  });
  await Promise.all(workers);
}

export interface ActiveScanResult {
  scanned: number;
  found: number;
  liveTcpHits: number;
  subnets: string[];
  durationMs: number;
}

export async function activeLanScan(): Promise<ActiveScanResult> {
  const t0 = Date.now();
  // Cooldown — back-to-back triggers (e.g. UDP discovery briefly
  // dropping packets) should reuse the previous scan's results
  // instead of re-flooding the LAN with TCP probes.
  if (t0 - _lastScanFinishedAt < SCAN_COOLDOWN_MS) {
    return {
      scanned: 0, found: servers.size,
      liveTcpHits: 0, subnets: getLocalSubnets(),
      durationMs: 0,
    };
  }
  const subnets = getLocalSubnets();
  if (subnets.length === 0) {
    return { scanned: 0, found: 0, liveTcpHits: 0, subnets: [], durationMs: 0 };
  }

  const targets: Array<{ host: string; port: number }> = [];
  // Try the mDNS-advertised hostname first (registered by the server as
  // helen.local). Windows 10+, macOS, and Linux with avahi all resolve
  // .local names via the built-in mDNS stack. We pre-resolve through
  // dns.lookup and check the result is on the local network — otherwise
  // a hostile mDNS/DNS responder could point helen.local at a public IP
  // and the active scan would happily probe it.
  const helenAddr = await _resolveHelenLocalLan();
  if (helenAddr) {
    for (const port of SCAN_PORTS) {
      targets.push({ host: helenAddr, port });
    }
  }
  for (const prefix of subnets) {
    for (let octet = 1; octet < 255; octet++) {
      for (const port of SCAN_PORTS) {
        targets.push({ host: `${prefix}${octet}`, port });
      }
    }
  }

  const liveHosts: Array<{ host: string; port: number }> = [];
  await runLimited(targets, SCAN_CONCURRENCY, async ({ host, port }) => {
    if (await probeTcp(host, port, SCAN_PROBE_TIMEOUT_MS)) {
      liveHosts.push({ host, port });
    }
  });

  let found = 0;
  await runLimited(liveHosts, 16, async ({ host, port }) => {
    const payload = await identifyHelen(host, port);
    if (!payload) return;
    const sid = payload.server_id || `${host}:${port}`;
    const prev = servers.get(sid);
    servers.set(sid, {
      server_id: sid,
      name: payload.name || 'Helen Server',
      host: payload.host || host,
      port: Number(payload.port || port),
      version: payload.version || '?',
      uptime: Number(payload.uptime || 0),
      users_online: Number(payload.users_online || 0),
      protocol: 'http',
      url: `http://${host}:${port}`,
      verified: true,
      last_seen: Date.now(),
      discovery_method: 'active_scan',
      rtt_ms: prev?.rtt_ms ?? null,
    });
    found++;
  });

  if (found > 0) broadcastToRenderer();
  _lastScanFinishedAt = Date.now();
  return {
    scanned: targets.length,
    found,
    liveTcpHits: liveHosts.length,
    subnets,
    durationMs: Date.now() - t0,
  };
}

function scheduleAutoEscalate(): void {
  setTimeout(() => {
    // If UDP already delivered anything, no need to scan.
    for (const s of servers.values()) {
      if (s.discovery_method === 'udp' || s.discovery_method === 'manual') return;
    }
    console.log('[Discovery] No UDP broadcast after', AUTO_ESCALATE_MS, 'ms — escalating to active scan');
    activeLanScan().then((r) => {
      console.log('[Discovery] Active scan:', r);
    }).catch((e) => console.warn('[Discovery] Active scan failed:', e?.message));
  }, AUTO_ESCALATE_MS);
}

// ── Public API ────────────────────────────────────────────

export function startDiscovery(): void {
  if (isListening) return;

  startUDPListener();

  // Periodic cleanup of stale entries
  if (!cleanupTimer) {
    cleanupTimer = setInterval(cleanupStaleServers, CLEANUP_INTERVAL_MS);
  }

  // Periodic re-verification
  if (!verifyTimer) {
    verifyTimer = setInterval(verifyAllServers, VERIFY_INTERVAL_MS);
  }

  // Start monitoring network interface changes (WiFi drop/reconnect)
  startNetworkWatcher();

  // Mandatory-connection guarantee: if no UDP broadcast arrives within a
  // grace period, automatically fall back to scanning the LAN over TCP.
  scheduleAutoEscalate();

  console.log('[Discovery] Service started');
}

export function stopDiscovery(): void {
  isListening = false;

  if (udpSocket) {
    try { udpSocket.close(); } catch {}
    udpSocket = null;
  }

  if (cleanupTimer) {
    clearInterval(cleanupTimer);
    cleanupTimer = null;
  }

  if (verifyTimer) {
    clearInterval(verifyTimer);
    verifyTimer = null;
  }

  stopNetworkWatcher();

  servers.clear();
  reconnectAttempts = 0;
  lastNetworkFingerprint = '';
  console.log('[Discovery] Service stopped');
}

// ── IPC Handlers ──────────────────────────────────────────

export function registerDiscoveryIPC(): void {
  ipcMain.handle('discovery:get-servers', () => getServerList());

  ipcMain.handle('discovery:get-best', () => getBestServer());

  ipcMain.handle('discovery:add-manual', async (_event, url: string) => {
    return addManualServer(url);
  });

  // Resolve a 64-char server code to a reachable DiscoveredServer. Waits up
  // to timeoutMs for a matching UDP broadcast before giving up.
  ipcMain.handle(
    'discovery:find-by-code',
    async (_event, code: string, timeoutMs?: number) => {
      return findByServerCode(code, timeoutMs ?? 8_000);
    },
  );

  ipcMain.handle('discovery:refresh', () => {
    verifyAllServers();
    return getServerList();
  });

  ipcMain.handle('discovery:is-listening', () => isListening);

  ipcMain.handle('discovery:get-network-status', () => {
    const fp = getNetworkFingerprint();
    return {
      hasNetwork: fp.length > 0,
      reconnectAttempts,
      isListening,
      serverCount: servers.size,
      verifiedCount: Array.from(servers.values()).filter((s) => s.verified).length,
    };
  });

  // Force restart discovery (e.g., user clicked "Retry" after network loss)
  ipcMain.handle('discovery:restart', () => {
    console.log('[Discovery] Manual restart requested');
    stopDiscovery();
    startDiscovery();
    return true;
  });

  // Force active TCP scan of the LAN — guaranteed fallback when UDP
  // broadcast is blocked by firewall/guest network.
  ipcMain.handle('discovery:active-scan', async () => {
    return await activeLanScan();
  });

  // ── LAN connectivity orchestrator (sequential fallback chain) ──
  // Single shared instance per Electron main process. The renderer never
  // talks to it directly — it goes through these IPCs.
  const lanOrch = new LanConnectivityOrchestrator(() =>
    getServerList().map((s) => ({ url: s.url, verified: s.verified }))
  );
  ipcMain.handle('lan-orch:run', async () => {
    return await lanOrch.runChain();
  });
  ipcMain.handle('lan-orch:snapshot', () => lanOrch.snapshot());
  ipcMain.handle('lan-orch:retry', async (_e, method: MethodId) => {
    return await lanOrch.retryMethod(method);
  });
  ipcMain.handle('lan-orch:abort', () => { lanOrch.abort(); return true; });
}
