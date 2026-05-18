/**
 * LanConnectivityOrchestrator — explicit, sequential LAN fallback chain.
 *
 * The user's requirement: "try method 1; if it fails, try method 2;
 * if method 2 works, stop — no more searching — unless manually
 * retried from the admin panel."
 *
 * This module wraps the existing discovery primitives (UDP broadcast,
 * active TCP scan, helen.local probe) and adds two new ones (SSDP
 * M-SEARCH, IPv4 multicast rendezvous), then runs them in a strict
 * priority order. Once a method yields a *verified* server (/api/health
 * returns 200), the chain halts and every subsequent method is marked
 * `skipped`. A manual retry re-runs any specific method on demand.
 *
 * Priority order (ascending = fastest / most LAN-friendly first):
 *
 *   1. mdns.local      — `helen.local` DNS lookup (system mDNS resolver)
 *   2. udp_broadcast   — passive listener on UDP 41234 (already running)
 *   3. ssdp            — multicast M-SEARCH on 239.255.255.250:1900
 *   4. multicast_query — custom Helen group 239.42.42.42:41235
 *   5. tcp_scan        — /24 subnet scan on ports 3000/3001
 *   6. apipa_scan      — 169.254.0.0/16 chunks when no DHCP subnet
 *
 * Each method has its own state: `idle | running | succeeded | failed
 * | skipped`. The admin UI reads this shape verbatim.
 */

import * as dgram from 'node:dgram';
import * as dns from 'node:dns';
import * as http from 'node:http';
import * as net from 'node:net';
import { networkInterfaces } from 'node:os';

// ── Types ─────────────────────────────────────────────────

export type MethodId =
  | 'mdns_local'
  | 'udp_broadcast'
  | 'ssdp'
  | 'multicast_query'
  | 'tcp_scan'
  | 'apipa_scan';

export type MethodState = 'idle' | 'running' | 'succeeded' | 'failed' | 'skipped';

export interface MethodReport {
  id: MethodId;
  state: MethodState;
  startedAt: number | null;
  durationMs: number | null;
  serverUrl: string | null;      // first verified server this method produced
  note: string | null;           // human-readable outcome or reason
}

export interface OrchestratorSnapshot {
  running: boolean;
  completedAt: number | null;
  winner: MethodId | null;
  methods: Record<MethodId, MethodReport>;
}

// ── Constants ─────────────────────────────────────────────

// Same port set the renderer-side discovery uses. Stays in lockstep
// with discovery.ts:SCAN_PORTS — 3088 first because that's what
// Helen Setup binds when bundled.
const HELEN_PORTS = [3088, 3000, 3001, 3002, 3003, 3010, 8080, 8088] as const;
const PROBE_TIMEOUT_MS = 1500;
const TCP_SCAN_PROBE_TIMEOUT_MS = 300;
const TCP_SCAN_CONCURRENCY = 64;
const APIPA_SAMPLE_SIZE = 128;     // APIPA /16 is too big to fully scan; sample
const MULTICAST_GROUP = '239.42.42.42';
const MULTICAST_QUERY_PORT = 41235;
const SSDP_GROUP = '239.255.255.250';
const SSDP_PORT = 1900;
const SSDP_ST = 'urn:helen-server:service:helen:1';

// ── Helpers ───────────────────────────────────────────────

function getLocalSubnets(): string[] {
  const prefixes = new Set<string>();
  const ifaces = networkInterfaces();
  for (const name of Object.keys(ifaces)) {
    const entries = ifaces[name];
    if (!entries) continue;
    for (const e of entries) {
      if (e.family !== 'IPv4' || e.internal) continue;
      if (e.address.startsWith('169.254.')) continue;
      const parts = e.address.split('.');
      if (parts.length !== 4) continue;
      prefixes.add(parts.slice(0, 3).join('.') + '.');
    }
  }
  return [...prefixes];
}

function hasApipaAddress(): boolean {
  const ifaces = networkInterfaces();
  for (const name of Object.keys(ifaces)) {
    const entries = ifaces[name];
    if (!entries) continue;
    for (const e of entries) {
      if (e.family === 'IPv4' && e.address.startsWith('169.254.')) return true;
    }
  }
  return false;
}

/**
 * Verify a resolved IP address is on the local network (RFC1918 +
 * link-local + loopback). The mDNS `helen.local` resolver typically
 * returns an LAN address, but a hijacked DNS server / hostile resolver
 * could resolve it to a public IP and trick the client into connecting
 * to an attacker-controlled server. Defense: gate `helen.local` probing
 * on the resolved address being LAN-only.
 */
function _isLanIp(addr: string): boolean {
  if (!addr) return false;
  if (addr === '127.0.0.1' || addr === '::1') return true;
  // RFC1918
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  if (/^172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  // Link-local IPv4 (APIPA)
  if (/^169\.254\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  // 100.64.0.0/10 — Carrier-grade NAT, used by some VPN/Tailscale-style
  // overlays for LAN-equivalent connectivity.
  if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}$/.test(addr)) return true;
  return false;
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

/** Confirm a candidate really is Helen by hitting /api/discovery. Returns
 *  the canonical URL (http://host:port) on success, null otherwise. */
function verifyHelen(host: string, port: number): Promise<string | null> {
  return new Promise((resolve) => {
    const req = http.get(
      { host, port, path: '/api/discovery', timeout: PROBE_TIMEOUT_MS },
      (res) => {
        if (res.statusCode !== 200) { res.resume(); resolve(null); return; }
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c) => { body += c; });
        res.on('end', () => {
          try {
            const p = JSON.parse(body);
            if (p?.type === 'commclient-server') {
              resolve(`http://${host}:${port}`);
            } else resolve(null);
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

// ── Individual method implementations ─────────────────────

async function methodMdnsLocal(signal: AbortSignal): Promise<string | null> {
  // Let the OS's mDNS resolver translate helen.local. Falls back through
  // LLMNR on Windows, avahi/systemd-resolved on Linux, mDNSResponder on macOS.
  const host = await new Promise<string | null>((resolve) => {
    dns.lookup('helen.local', { family: 4 }, (err, address) => {
      resolve(err ? null : address);
    });
  });
  if (!host || signal.aborted) return null;
  // Hostile mDNS / DNS responders could resolve `helen.local` to a public
  // IP and trick the client into connecting to an attacker-controlled
  // server. Reject any non-LAN resolution outright before we so much as
  // open a TCP socket.
  if (!_isLanIp(host)) {
    console.warn(`[lan-orchestrator] helen.local resolved to non-LAN address (${host}); rejecting.`);
    return null;
  }
  for (const port of HELEN_PORTS) {
    if (signal.aborted) return null;
    if (await probeTcp(host, port, PROBE_TIMEOUT_MS)) {
      const url = await verifyHelen(host, port);
      if (url) return url;
    }
  }
  return null;
}

async function methodUdpBroadcast(
  getDiscoveredServers: () => Array<{ url: string; verified: boolean }>,
  signal: AbortSignal,
): Promise<string | null> {
  // The UDP broadcast listener is always running in discovery.ts. Just
  // check its current result set; don't re-bind or duplicate work.
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline && !signal.aborted) {
    const list = getDiscoveredServers().filter((s) => s.verified);
    if (list.length > 0) return list[0].url;
    await new Promise((r) => setTimeout(r, 400));
  }
  return null;
}

async function methodSsdp(signal: AbortSignal): Promise<string | null> {
  return new Promise<string | null>((resolve) => {
    const sock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
    let settled = false;
    const finish = (url: string | null) => {
      if (settled) return;
      settled = true;
      try { sock.close(); } catch {}
      resolve(url);
    };
    const timeout = setTimeout(() => finish(null), 3500);
    signal.addEventListener('abort', () => { clearTimeout(timeout); finish(null); });

    sock.on('message', async (msg, _rinfo) => {
      const text = msg.toString('utf-8');
      const m = text.match(/LOCATION:\s*(http:\/\/([^:/]+):(\d+))/i);
      if (!m) return;
      const host = m[2];
      const port = parseInt(m[3], 10);
      const url = await verifyHelen(host, port);
      if (url) { clearTimeout(timeout); finish(url); }
    });

    sock.bind(0, () => {
      try { sock.setBroadcast(true); } catch {}
      const msg = Buffer.from(
        `M-SEARCH * HTTP/1.1\r\n` +
        `HOST: ${SSDP_GROUP}:${SSDP_PORT}\r\n` +
        `MAN: "ssdp:discover"\r\n` +
        `MX: 2\r\n` +
        `ST: ${SSDP_ST}\r\n` +
        `\r\n`, 'ascii',
      );
      sock.send(msg, 0, msg.length, SSDP_PORT, SSDP_GROUP, (err) => {
        if (err) { clearTimeout(timeout); finish(null); }
      });
    });
  });
}

async function methodMulticastQuery(signal: AbortSignal): Promise<string | null> {
  // Send a small JSON ping to our custom multicast group. Servers listening
  // on that group (future work — currently just advertised via UDP broadcast)
  // reply with their /api/discovery URL. For now this is a no-op that fails
  // fast so operators can see the slot exists when we add the responder.
  // Signals abort fast to keep the chain responsive.
  void signal;
  return null;
}

async function methodTcpScan(signal: AbortSignal): Promise<string | null> {
  const prefixes = getLocalSubnets();
  if (prefixes.length === 0) return null;
  const targets: Array<{ host: string; port: number }> = [];
  // Prepend helen.local again in case the first method was interrupted before
  // probing both ports — cheap redundancy. Resolve through dns.lookup so we
  // can run the same RFC1918 / link-local guard as methodMdnsLocal; otherwise
  // a hostile mDNS/DNS responder would let us TCP-connect to a public IP.
  const helenAddr = await new Promise<string | null>((resolve) => {
    dns.lookup('helen.local', { family: 4 }, (err, address) => {
      resolve(err ? null : address);
    });
  });
  if (helenAddr && _isLanIp(helenAddr)) {
    for (const port of HELEN_PORTS) targets.push({ host: helenAddr, port });
  } else if (helenAddr) {
    console.warn(`[lan-orchestrator] tcp_scan: helen.local resolved to non-LAN address (${helenAddr}); skipping.`);
  }
  for (const prefix of prefixes) {
    for (let o = 1; o < 255; o++) {
      for (const port of HELEN_PORTS) targets.push({ host: `${prefix}${o}`, port });
    }
  }
  let winner: string | null = null;
  await runLimited(targets, TCP_SCAN_CONCURRENCY, async ({ host, port }) => {
    if (winner || signal.aborted) return;
    if (!(await probeTcp(host, port, TCP_SCAN_PROBE_TIMEOUT_MS))) return;
    const url = await verifyHelen(host, port);
    if (url && !winner) winner = url;
  });
  return winner;
}

async function methodApipaScan(signal: AbortSignal): Promise<string | null> {
  if (!hasApipaAddress()) return null;
  // APIPA /16 = 65,534 hosts. Too many to TCP-probe exhaustively. Sample the
  // /24 neighborhood of each of our own APIPA addresses, which is where the
  // other device would have landed given classic APIPA collision avoidance.
  const ifaces = networkInterfaces();
  const targets: Array<{ host: string; port: number }> = [];
  for (const name of Object.keys(ifaces)) {
    const entries = ifaces[name];
    if (!entries) continue;
    for (const e of entries) {
      if (e.family !== 'IPv4' || !e.address.startsWith('169.254.')) continue;
      const parts = e.address.split('.');
      const prefix = parts.slice(0, 3).join('.') + '.';
      for (let o = 1; o < 255 && targets.length < APIPA_SAMPLE_SIZE * HELEN_PORTS.length; o++) {
        for (const port of HELEN_PORTS) targets.push({ host: `${prefix}${o}`, port });
      }
    }
  }
  let winner: string | null = null;
  await runLimited(targets, TCP_SCAN_CONCURRENCY, async ({ host, port }) => {
    if (winner || signal.aborted) return;
    if (!(await probeTcp(host, port, TCP_SCAN_PROBE_TIMEOUT_MS))) return;
    const url = await verifyHelen(host, port);
    if (url && !winner) winner = url;
  });
  return winner;
}

// ── Orchestrator ───────────────────────────────────────────

export class LanConnectivityOrchestrator {
  private state: OrchestratorSnapshot;
  private abortCtrl: AbortController | null = null;
  private order: MethodId[] = [
    'mdns_local',
    'udp_broadcast',
    'ssdp',
    'multicast_query',
    'tcp_scan',
    'apipa_scan',
  ];
  private getDiscoveredServers: () => Array<{ url: string; verified: boolean }>;
  private onWinner: ((url: string, method: MethodId) => void) | null = null;

  constructor(getDiscoveredServers: () => Array<{ url: string; verified: boolean }>) {
    this.getDiscoveredServers = getDiscoveredServers;
    this.state = this.initialSnapshot();
  }

  onServerPicked(cb: (url: string, method: MethodId) => void): void {
    this.onWinner = cb;
  }

  snapshot(): OrchestratorSnapshot {
    // Return a defensive copy; admin UI mutates nothing directly.
    return JSON.parse(JSON.stringify(this.state));
  }

  private initialSnapshot(): OrchestratorSnapshot {
    const methods: Record<MethodId, MethodReport> = {} as any;
    for (const id of this.order) {
      methods[id] = {
        id, state: 'idle', startedAt: null, durationMs: null,
        serverUrl: null, note: null,
      };
    }
    return { running: false, completedAt: null, winner: null, methods };
  }

  /** Run the full chain. Short-circuits on first success. Safe to call
   *  multiple times; cancels any in-flight run first. */
  async runChain(): Promise<OrchestratorSnapshot> {
    this.abort();
    this.abortCtrl = new AbortController();
    this.state = this.initialSnapshot();
    this.state.running = true;
    const signal = this.abortCtrl.signal;

    for (const id of this.order) {
      if (signal.aborted || this.state.winner) {
        if (this.state.methods[id].state === 'idle') {
          this.state.methods[id].state = 'skipped';
          this.state.methods[id].note = this.state.winner ? 'short-circuited' : 'aborted';
        }
        continue;
      }
      const url = await this.runOne(id, signal);
      if (url) {
        this.state.winner = id;
        if (this.onWinner) {
          try { this.onWinner(url, id); } catch {}
        }
      }
    }
    this.state.running = false;
    this.state.completedAt = Date.now();
    return this.snapshot();
  }

  /** Re-run a specific method on demand. Does not reset others. */
  async retryMethod(id: MethodId): Promise<MethodReport> {
    if (!this.order.includes(id)) {
      throw new Error(`unknown method: ${id}`);
    }
    const ctrl = new AbortController();
    const url = await this.runOne(id, ctrl.signal);
    if (url && !this.state.winner) {
      this.state.winner = id;
      if (this.onWinner) try { this.onWinner(url, id); } catch {}
    }
    return { ...this.state.methods[id] };
  }

  private async runOne(id: MethodId, signal: AbortSignal): Promise<string | null> {
    const report = this.state.methods[id];
    report.state = 'running';
    report.startedAt = Date.now();
    report.note = null;
    report.serverUrl = null;
    try {
      let url: string | null = null;
      switch (id) {
        case 'mdns_local':       url = await methodMdnsLocal(signal); break;
        case 'udp_broadcast':    url = await methodUdpBroadcast(this.getDiscoveredServers, signal); break;
        case 'ssdp':             url = await methodSsdp(signal); break;
        case 'multicast_query':  url = await methodMulticastQuery(signal); break;
        case 'tcp_scan':         url = await methodTcpScan(signal); break;
        case 'apipa_scan':       url = await methodApipaScan(signal); break;
      }
      report.durationMs = Date.now() - (report.startedAt || Date.now());
      if (url) {
        report.state = 'succeeded';
        report.serverUrl = url;
        report.note = 'verified /api/discovery';
      } else {
        report.state = 'failed';
        report.note = 'no matching server';
      }
      return url;
    } catch (e: any) {
      report.durationMs = Date.now() - (report.startedAt || Date.now());
      report.state = 'failed';
      report.note = 'error: ' + (e?.message || e);
      return null;
    }
  }

  abort(): void {
    if (this.abortCtrl) {
      try { this.abortCtrl.abort(); } catch {}
      this.abortCtrl = null;
    }
  }
}
