/**
 * ServerPicker — interactive replacement for the old read-only Server
 * Info panel. Wraps the LAN-discovery IPC exposed by `electronAPI.discovery`
 * and the auth-store `setServerUrl` so the user can:
 *
 *   • see the live list of discovered Helen servers (UDP broadcast,
 *     mDNS, manually added, or active-scan-found)
 *   • watch each server's RTT, verified badge, and method
 *   • toggle auto-rescan (default: every 30 s)
 *   • trigger a manual UDP refresh (fast) or active TCP scan (slow,
 *     full LAN sweep — fallback when broadcast is firewalled)
 *   • paste a custom URL and try it directly
 *   • reconnect the socket against the currently selected server
 *
 * The component is fail-soft: if `electronAPI.discovery` is missing
 * (running outside Electron, or in an old build) it degrades to a
 * plain "edit URL + reconnect" form so the user is never stuck.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    RefreshCw, Search, Server, Check, Globe, Wifi, WifiOff, Plus, X, Sparkles,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { socketManager } from '@/services/socket.manager';

interface DiscoveredServer {
    server_id: string;
    name: string;
    host: string;
    port: number;
    version?: string;
    uptime?: number;
    users_online?: number;
    protocol?: string;
    url: string;
    verified: boolean;
    last_seen: number;
    discovery_method?: 'udp' | 'mdns' | 'manual' | 'active_scan';
    rtt_ms?: number | null;
}

const AUTO_RESCAN_MS = 30_000;

function rttBadge(rtt: number | null | undefined): string {
    if (rtt == null) return '—';
    if (rtt < 50) return `${rtt} ms`;
    if (rtt < 200) return `${rtt} ms`;
    return `${rtt} ms`;
}

function rttClass(rtt: number | null | undefined): string {
    if (rtt == null) return 'text-text-500';
    if (rtt < 80) return 'text-green-400';
    if (rtt < 250) return 'text-yellow-400';
    return 'text-red-400';
}

// ── Auto-connect step model ──────────────────────────────────────────
//
// The chain runs in this order, short-circuiting on the first hit:
//   1. local       — probe http://127.0.0.1:3000/api/health (server on
//                    the same machine, e.g. running from Helen Setup
//                    that bundles the server)
//   2. lan         — discovery.lanOrch.runChain (mDNS → UDP broadcast
//                    → SSDP → multicast query → TCP scan → APIPA scan)
//   3. tcp_scan    — explicit fallback if lan-orch returns nothing
//   4. rendezvous  — public tunnel if rendezvousUrl is configured
type StepId    = 'local' | 'lan' | 'tcp_scan' | 'rendezvous';
type StepState = 'idle' | 'running' | 'ok' | 'fail' | 'skipped';

interface AutoStep {
    id:    StepId;
    label: string;
    sub:   string;
    state: StepState;
    note?: string;
    foundUrl?: string;
}

const INITIAL_STEPS: AutoStep[] = [
    { id: 'local',      label: 'Same computer',     sub: '127.0.0.1:3000',         state: 'idle' },
    { id: 'lan',        label: 'LAN router',        sub: 'mDNS · UDP · multicast', state: 'idle' },
    { id: 'tcp_scan',   label: 'Deep LAN scan',     sub: 'TCP fallback',           state: 'idle' },
    { id: 'rendezvous', label: 'Remote rendezvous', sub: 'public tunnel',          state: 'idle' },
];

async function probeUrl(url: string, timeoutMs = 1500): Promise<boolean> {
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), timeoutMs);
        const r = await fetch(url.replace(/\/+$/, '') + '/api/health',
            { signal: ctrl.signal, cache: 'no-store' });
        clearTimeout(t);
        return r.ok;
    } catch { return false; }
}

export const ServerPicker: React.FC = () => {
    const serverUrl     = useAuthStore((s) => s.serverUrl);
    const setServerUrl  = useAuthStore((s) => s.setServerUrl);
    const tokens        = useAuthStore((s) => s.tokens);
    const rendezvousUrl = useAuthStore((s) => s.rendezvousUrl);

    const [servers,        setServers]        = useState<DiscoveredServer[]>([]);
    const [connected,      setConnected]      = useState<boolean>(false);
    const [scanning,       setScanning]       = useState<boolean>(false);
    const [activeScanning, setActiveScanning] = useState<boolean>(false);
    const [autoRescan,     setAutoRescan]     = useState<boolean>(true);
    const [customUrl,      setCustomUrl]      = useState<string>('');
    const [statusMsg,      setStatusMsg]      = useState<string>('');
    const [scanResult,     setScanResult]     = useState<string>('');
    const [autoSteps,      setAutoSteps]      = useState<AutoStep[]>(INITIAL_STEPS);
    const [autoRunning,    setAutoRunning]    = useState<boolean>(false);

    // ── Connection state mirror ─────────────────────────────────────
    useEffect(() => {
        setConnected(socketManager.isConnected());
        const offC = socketManager.on('connect',    () => setConnected(true));
        const offD = socketManager.on('disconnect', () => setConnected(false));
        return () => { offC(); offD(); };
    }, []);

    // ── Live server list ─────────────────────────────────────────────
    const discovery = (window as any).electronAPI?.discovery;
    useEffect(() => {
        if (!discovery) return;
        let cancelled = false;
        const seed = async () => {
            try {
                const list = await discovery.getServers();
                if (!cancelled && Array.isArray(list)) setServers(list);
            } catch { /* discovery may not have started yet */ }
        };
        seed();
        const off = discovery.onServersUpdated?.((list: DiscoveredServer[]) => {
            if (!cancelled) setServers(Array.isArray(list) ? list : []);
        });
        return () => { cancelled = true; off?.(); };
    }, [discovery]);

    // ── Auto-rescan timer ────────────────────────────────────────────
    const rescanTimerRef = useRef<number | null>(null);
    useEffect(() => {
        if (!autoRescan || !discovery) return;
        const tick = async () => {
            try {
                setScanning(true);
                await discovery.refresh();
            } finally {
                setScanning(false);
            }
        };
        rescanTimerRef.current = window.setInterval(tick, AUTO_RESCAN_MS);
        return () => {
            if (rescanTimerRef.current) window.clearInterval(rescanTimerRef.current);
        };
    }, [autoRescan, discovery]);

    // ── Sorted view: verified first, then by RTT ascending, then name ─
    const sortedServers = useMemo(() => {
        return [...servers].sort((a, b) => {
            if (a.verified !== b.verified) return a.verified ? -1 : 1;
            const ar = a.rtt_ms ?? Number.POSITIVE_INFINITY;
            const br = b.rtt_ms ?? Number.POSITIVE_INFINITY;
            if (ar !== br) return ar - br;
            return (a.name || a.host).localeCompare(b.name || b.host);
        });
    }, [servers]);

    // ── Actions ──────────────────────────────────────────────────────
    const onManualRefresh = async () => {
        if (!discovery) return;
        setScanning(true); setStatusMsg('Refreshing…');
        try {
            const list = await discovery.refresh();
            if (Array.isArray(list)) setServers(list);
            setStatusMsg(`Refreshed — ${list?.length ?? 0} server(s) on the LAN`);
        } catch (e: any) {
            setStatusMsg('Refresh failed: ' + (e?.message || 'unknown'));
        } finally {
            setScanning(false);
        }
    };

    const onActiveScan = async () => {
        if (!discovery?.activeScan) return;
        setActiveScanning(true); setScanResult('');
        try {
            const r = await discovery.activeScan();
            setScanResult(`Scanned ${r.scanned} hosts on ${r.subnets.length} subnet(s) in ${r.durationMs}ms — found ${r.found} server(s) (${r.liveTcpHits} live TCP)`);
        } catch (e: any) {
            setScanResult('Scan failed: ' + (e?.message || 'unknown'));
        } finally {
            setActiveScanning(false);
        }
    };

    const onPick = async (url: string) => {
        if (!url) return;
        setServerUrl(url);
        setStatusMsg(`Selected ${url}`);

        // Audit fix #6: a token issued by the OLD server is invalid on
        // the new one (different JWT_SECRET, unless the operator runs
        // a federated cluster with shared secret). Reusing it produces
        // a misleading "connected" UI followed by silent rejections
        // on every authenticated emit.
        //
        // Behaviour now: if the user is signed in, drop the session
        // entirely and route them to the login screen against the new
        // server. The auth.store handles the actual disconnect /
        // credential-clear / state-reset.
        if (tokens?.access_token) {
            setStatusMsg(`Switching to ${url} — please sign in again.`);
            try {
                await useAuthStore.getState().logout();
            } catch {
                // swallow — the user might be partly signed-out already
            }
            try { socketManager.disconnect(); } catch {}
            // The renderer's bootstrap screen reads serverUrl and
            // routes back to the login flow.
        } else {
            // Not signed in — just remember the URL so the next login
            // hits the right server.
            try { socketManager.disconnect(); } catch {}
        }
    };

    // ── Auto-connect chain ──────────────────────────────────────────
    //
    // One-click "find a Helen anywhere": try the local machine first,
    // then sweep the LAN, then fall back to a configured remote
    // rendezvous tunnel. Each step's state is rendered live so the
    // user can see exactly what we tried.
    const setStep = (id: StepId, patch: Partial<AutoStep>) =>
        setAutoSteps((prev) => prev.map((s) => s.id === id ? { ...s, ...patch } : s));

    const onAutoConnect = async () => {
        if (autoRunning) return;
        setAutoRunning(true);
        setAutoSteps(INITIAL_STEPS.map((s) => ({ ...s })));
        setStatusMsg('');

        const finish = (url: string, via: StepId, note?: string) => {
            setStep(via, { state: 'ok', foundUrl: url, note });
            // Mark anything still idle as skipped — short-circuited by hit.
            setAutoSteps((prev) => prev.map((s) =>
                s.state === 'idle' ? { ...s, state: 'skipped', note: 'short-circuited' } : s,
            ));
            onPick(url);
            setStatusMsg(`Connected via ${via}: ${url}`);
            setAutoRunning(false);
        };

        // 1. Same computer.
        setStep('local', { state: 'running' });
        const localUrl = 'http://127.0.0.1:3000';
        if (await probeUrl(localUrl, 1500)) {
            return finish(localUrl, 'local', 'reachable on same host');
        }
        setStep('local', { state: 'fail', note: 'no server on 127.0.0.1:3000' });

        // 2. LAN orchestrator (mDNS → UDP → SSDP → multicast → TCP → APIPA).
        setStep('lan', { state: 'running' });
        if (discovery?.lanOrch?.run) {
            try {
                const snap = await discovery.lanOrch.run();
                const winner = snap?.winner ? snap.methods?.[snap.winner] : null;
                if (winner?.serverUrl && await probeUrl(winner.serverUrl, 2000)) {
                    return finish(winner.serverUrl, 'lan', `winner: ${snap.winner}`);
                }
                setStep('lan', { state: 'fail', note: 'no LAN server responded' });
            } catch (e: any) {
                setStep('lan', { state: 'fail', note: e?.message || 'orchestrator error' });
            }
        } else {
            setStep('lan', { state: 'skipped', note: 'lan-orch not available' });
        }

        // 3. Active TCP scan — chunkier fallback when broadcast/mdns blocked.
        setStep('tcp_scan', { state: 'running' });
        if (discovery?.activeScan) {
            try {
                const r = await discovery.activeScan();
                const refreshed = await discovery.getServers();
                const verified = (refreshed || []).find((s: any) => s.verified);
                if (verified?.url) {
                    return finish(verified.url, 'tcp_scan',
                        `scanned ${r.scanned} hosts in ${r.durationMs}ms`);
                }
                setStep('tcp_scan', {
                    state: 'fail',
                    note: `scanned ${r.scanned} hosts, ${r.found} found, none verified`,
                });
            } catch (e: any) {
                setStep('tcp_scan', { state: 'fail', note: e?.message || 'scan error' });
            }
        } else {
            setStep('tcp_scan', { state: 'skipped', note: 'activeScan not available' });
        }

        // 4. Remote rendezvous tunnel (last resort, requires prior config).
        setStep('rendezvous', { state: 'running' });
        if (rendezvousUrl) {
            if (await probeUrl(rendezvousUrl, 4000)) {
                return finish(rendezvousUrl, 'rendezvous', 'public tunnel reachable');
            }
            setStep('rendezvous', { state: 'fail', note: 'tunnel did not respond' });
        } else {
            setStep('rendezvous', { state: 'skipped', note: 'no rendezvous URL configured' });
        }

        setStatusMsg('Auto-connect failed — no Helen server reachable on any path.');
        setAutoRunning(false);
    };

    const onAddCustom = async () => {
        const raw = customUrl.trim();
        if (!raw) return;
        const url = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
        try { new URL(url); } catch { setStatusMsg('Invalid URL'); return; }
        if (discovery?.addManual) {
            try {
                const added = await discovery.addManual(url);
                if (added) {
                    setStatusMsg(`Added ${url} — verifying…`);
                    setCustomUrl('');
                    return;
                }
            } catch { /* fall through to direct pick */ }
        }
        // No discovery layer or addManual rejected — just point at it.
        onPick(url);
        setCustomUrl('');
    };

    const onReconnect = () => {
        if (!serverUrl || !tokens?.access_token) return;
        try { socketManager.disconnect(); } catch {}
        socketManager.connect(serverUrl, tokens.access_token);
        setStatusMsg('Reconnecting…');
    };

    // ── Render ───────────────────────────────────────────────────────
    return (
        <div className="space-y-4">
            {/* Current server + connection status */}
            <div className="flex items-center justify-between gap-3 p-3 rounded-lg bg-surface-900 border border-surface-800">
                <div className="min-w-0 flex-1">
                    <div className="text-xs text-text-500">Active server</div>
                    <div className="text-sm font-medium text-text-100 truncate">
                        {serverUrl ? serverUrl.replace(/^https?:\/\//, '') : '— none —'}
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    {connected ? (
                        <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-green-500/15 text-green-400">
                            <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
                            connected
                        </span>
                    ) : (
                        <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-red-500/15 text-red-400">
                            <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
                            disconnected
                        </span>
                    )}
                    <button
                        onClick={onReconnect}
                        disabled={!serverUrl}
                        title="Reconnect to active server"
                        className="p-2 rounded-md bg-surface-800 hover:bg-surface-700 disabled:opacity-40 transition-colors"
                    >
                        <RefreshCw size={16} />
                    </button>
                </div>
            </div>

            {/* Auto-connect — one-click chain (local → LAN → TCP → remote) */}
            <div className="rounded-lg bg-surface-900 border border-surface-800 overflow-hidden">
                <div className="p-3 flex items-center gap-3">
                    <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium text-text-100">Auto-connect</div>
                        <div className="text-xs text-text-500">
                            Find a Helen server anywhere — same machine, your LAN, or a remote rendezvous.
                        </div>
                    </div>
                    <button
                        onClick={onAutoConnect}
                        disabled={autoRunning}
                        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white transition-colors"
                    >
                        <Sparkles size={14} className={autoRunning ? 'animate-pulse' : ''} />
                        {autoRunning ? 'Searching…' : 'Connect automatically'}
                    </button>
                </div>
                {(autoRunning || autoSteps.some((s) => s.state !== 'idle')) && (
                    <ul className="border-t border-surface-800 divide-y divide-surface-800">
                        {autoSteps.map((s, i) => (
                            <li key={s.id} className="flex items-center gap-3 p-3">
                                <span className="text-text-500 text-xs w-4 tabular-nums">{i + 1}.</span>
                                <span className={
                                    s.state === 'ok'      ? 'text-green-400'
                                    : s.state === 'fail'    ? 'text-red-400'
                                    : s.state === 'running' ? 'text-yellow-300'
                                    : 'text-text-500'
                                }>
                                    {s.state === 'ok'      ? <Check  size={14} /> :
                                     s.state === 'fail'    ? <X      size={14} /> :
                                     s.state === 'running' ? <RefreshCw size={14} className="animate-spin" /> :
                                                              <span className="inline-block w-3.5 h-3.5 rounded-full border border-current" />}
                                </span>
                                <div className="flex-1 min-w-0">
                                    <div className="text-sm text-text-200">{s.label}</div>
                                    <div className="text-xs text-text-500 truncate">
                                        {s.note ? s.note : s.sub}
                                    </div>
                                </div>
                                {s.foundUrl && (
                                    <span className="text-xs text-green-400 font-mono truncate max-w-[160px]">
                                        {s.foundUrl.replace(/^https?:\/\//, '')}
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            {/* Discovery controls */}
            <div className="flex flex-wrap items-center gap-2">
                <button
                    onClick={onManualRefresh}
                    disabled={!discovery || scanning}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-surface-800 hover:bg-surface-700 disabled:opacity-40 transition-colors"
                >
                    <RefreshCw size={14} className={scanning ? 'animate-spin' : ''} />
                    Rescan now
                </button>
                <button
                    onClick={onActiveScan}
                    disabled={!discovery?.activeScan || activeScanning}
                    title="Full TCP scan of the local subnet — slower, used when UDP broadcast is blocked."
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-surface-800 hover:bg-surface-700 disabled:opacity-40 transition-colors"
                >
                    <Search size={14} className={activeScanning ? 'animate-pulse' : ''} />
                    Deep scan
                </button>
                <label className="inline-flex items-center gap-2 text-sm text-text-300 ml-auto">
                    <input
                        type="checkbox"
                        checked={autoRescan}
                        onChange={(e) => setAutoRescan(e.target.checked)}
                        className="h-4 w-4 accent-blue-500"
                    />
                    Auto-rescan every 30 s
                </label>
            </div>

            {/* Discovered server list */}
            <div className="rounded-lg bg-surface-900 border border-surface-800 overflow-hidden">
                {sortedServers.length === 0 ? (
                    <div className="p-4 text-sm text-text-500 text-center">
                        {discovery
                            ? (scanning ? 'Scanning the LAN…' : 'No servers discovered yet. Try Rescan or Deep scan.')
                            : 'Discovery unavailable in this build — use the custom URL below.'}
                    </div>
                ) : (
                    <ul className="divide-y divide-surface-800">
                        {sortedServers.map((s) => {
                            const isActive = serverUrl === s.url;
                            return (
                                <li key={s.server_id || s.url} className="flex items-center gap-3 p-3 hover:bg-surface-800 transition-colors">
                                    <span className={s.verified ? 'text-green-400' : 'text-yellow-400'}>
                                        {s.verified ? <Wifi size={16} /> : <WifiOff size={16} />}
                                    </span>
                                    <div className="min-w-0 flex-1">
                                        <div className="flex items-center gap-2">
                                            <span className="text-sm font-medium text-text-100 truncate">
                                                {s.name || s.host}
                                            </span>
                                            {isActive && (
                                                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] rounded bg-blue-500/15 text-blue-400">
                                                    <Check size={10} /> active
                                                </span>
                                            )}
                                        </div>
                                        <div className="flex items-center gap-3 text-xs text-text-500">
                                            <span>{s.host}:{s.port}</span>
                                            {s.discovery_method && (
                                                <span className="px-1 py-0.5 rounded bg-surface-800 text-text-400">
                                                    {s.discovery_method}
                                                </span>
                                            )}
                                            <span className={rttClass(s.rtt_ms)}>
                                                RTT: {rttBadge(s.rtt_ms)}
                                            </span>
                                            {typeof s.users_online === 'number' && (
                                                <span>· {s.users_online} online</span>
                                            )}
                                        </div>
                                    </div>
                                    <button
                                        onClick={() => onPick(s.url)}
                                        disabled={isActive}
                                        className="px-3 py-1.5 text-xs rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:hover:bg-blue-600 text-white transition-colors"
                                    >
                                        {isActive ? 'In use' : 'Connect'}
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                )}
            </div>

            {/* Custom URL */}
            <div className="rounded-lg bg-surface-900 border border-surface-800 p-3">
                <div className="text-xs text-text-500 mb-2 flex items-center gap-1.5">
                    <Globe size={12} /> Add a server manually
                </div>
                <div className="flex items-center gap-2">
                    <input
                        type="text"
                        value={customUrl}
                        onChange={(e) => setCustomUrl(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') onAddCustom(); }}
                        placeholder="192.168.1.34:3088 or https://helen.example.com"
                        className="flex-1 px-3 py-1.5 text-sm bg-surface-800 border border-surface-700 rounded-md text-text-100 placeholder:text-text-600 focus:outline-none focus:border-blue-500"
                    />
                    <button
                        onClick={onAddCustom}
                        disabled={!customUrl.trim()}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-surface-800 hover:bg-surface-700 disabled:opacity-40 transition-colors"
                    >
                        <Plus size={14} />
                        Add
                    </button>
                </div>
            </div>

            {/* Status / scan result line */}
            {(statusMsg || scanResult) && (
                <div className="text-xs text-text-500 flex flex-col gap-1">
                    {statusMsg  && <div className="inline-flex items-center gap-1.5"><Server size={12} />{statusMsg}</div>}
                    {scanResult && <div className="inline-flex items-center gap-1.5"><Search size={12} />{scanResult}</div>}
                </div>
            )}
        </div>
    );
};
