/**
 * autoConnect — shared chain that locates a Helen server through every
 * available path. Used by both the boot orchestrator (which loops it
 * indefinitely) and the Settings → Server Info "Connect automatically"
 * button (one-shot).
 *
 * Order:
 *   1. local       — http://127.0.0.1:3000
 *   2. saved       — whatever URL is currently in localStorage (so a
 *                    returning user doesn't pay the LAN-scan tax when
 *                    their previous server is still up)
 *   3. lan         — discovery.lanOrch.runChain (mDNS → UDP →
 *                    SSDP → multicast → TCP scan → APIPA scan)
 *   4. tcp_scan    — discovery.activeScan as an explicit fallback
 *   5. rendezvous  — the configured public tunnel (last resort)
 *
 * The chain short-circuits at the first verified hit. Each step is
 * reported via the `onStep` callback so the UI can show live progress.
 */

export type AutoConnectStep =
    | 'local'
    | 'saved'
    | 'lan'
    | 'tcp_scan'
    | 'rendezvous';

export type AutoConnectStepState = 'idle' | 'running' | 'ok' | 'fail' | 'skipped';

export interface AutoConnectStepEvent {
    id:    AutoConnectStep;
    state: AutoConnectStepState;
    note?: string;
    foundUrl?: string;
}

export interface AutoConnectResult {
    ok:       boolean;
    via?:     AutoConnectStep;
    url?:     string;
    attempts: AutoConnectStepEvent[];
}

export interface AutoConnectOptions {
    savedUrl?:      string | null;
    rendezvousUrl?: string | null;
    onStep?:        (event: AutoConnectStepEvent) => void;
    /** Per-probe HTTP timeout. Defaults to 1500 ms. */
    probeTimeoutMs?: number;
}

// Unified default port: 3000. Matches:
//   - CommClient-Server/app/core/config.py:22  (PORT = 3000)
//   - CommClient-Desktop/src/main/config.ts    (DEFAULT_SERVER_URL)
//   - electron-builder.yml extraResources path expectation
// The audit flagged 3088 here as a third source-of-truth that produced
// false-fail "no local server" before the alt-port scan rescued it.
const DEFAULT_LOCAL_URL = 'http://127.0.0.1:3000';

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

export async function runAutoConnect(opts: AutoConnectOptions = {}): Promise<AutoConnectResult> {
    const probeTimeoutMs = opts.probeTimeoutMs ?? 1500;
    const events: AutoConnectStepEvent[] = [];
    const emit = (id: AutoConnectStep, state: AutoConnectStepState, extra?: Partial<AutoConnectStepEvent>) => {
        const ev = { id, state, ...extra };
        events.push(ev);
        opts.onStep?.(ev);
    };
    const finish = (id: AutoConnectStep, url: string): AutoConnectResult => {
        emit(id, 'ok', { foundUrl: url });
        return { ok: true, via: id, url, attempts: events };
    };

    const discovery = (window as any).electronAPI?.discovery;

    // 1. Localhost — server bundled with the desktop installer.
    emit('local', 'running');
    if (await probeUrl(DEFAULT_LOCAL_URL, probeTimeoutMs)) return finish('local', DEFAULT_LOCAL_URL);
    emit('local', 'fail', { note: `no server on ${DEFAULT_LOCAL_URL}` });

    // 2. Saved URL — first the verbatim URL, then the same host on
    //    Helen's alternate default ports. This rescues the common
    //    "I saved 192.168.1.34:3000 last week, but the operator restarted
    //    the server on 3088" footgun: instead of a dead-end fail, we
    //    probe :3088, :3001, :3002 on the same host before moving on.
    const saved = (opts.savedUrl || '').trim();
    if (saved && saved !== DEFAULT_LOCAL_URL) {
        emit('saved', 'running');
        if (await probeUrl(saved, Math.max(probeTimeoutMs, 2500))) {
            return finish('saved', saved);
        }
        // Try alternate ports on the same host. The set spans every
        // common Helen-Server default plus a few "operator forgot what
        // port they picked" rescue ports (8080/8088), so a stale saved
        // URL on the wrong port doesn't dead-end the chain.
        try {
            const u = new URL(saved);
            const alt = [3088, 3000, 3001, 3002, 3003, 3010, 8080, 8088]
                .filter((p) => String(p) !== u.port);
            for (const port of alt) {
                u.port = String(port);
                const candidate = u.toString().replace(/\/+$/, '');
                if (await probeUrl(candidate, probeTimeoutMs)) {
                    return finish('saved', candidate);
                }
            }
        } catch { /* malformed saved URL — skip alt ports */ }
        emit('saved', 'fail', { note: 'saved URL + alt ports did not respond' });
    } else {
        emit('saved', 'skipped', { note: 'no saved URL' });
    }

    // 3. LAN orchestrator (multi-method: mDNS, UDP, SSDP, multicast, TCP, APIPA).
    if (discovery?.lanOrch?.run) {
        emit('lan', 'running');
        try {
            const snap = await discovery.lanOrch.run();
            const winner = snap?.winner ? snap.methods?.[snap.winner] : null;
            if (winner?.serverUrl && await probeUrl(winner.serverUrl, 2000)) {
                return finish('lan', winner.serverUrl);
            }
            emit('lan', 'fail', { note: 'no LAN server responded' });
        } catch (e: any) {
            emit('lan', 'fail', { note: e?.message || 'orchestrator error' });
        }
    } else {
        emit('lan', 'skipped', { note: 'lan-orch not available' });
    }

    // 4. Active TCP scan — chunkier fallback if broadcast/mdns blocked.
    if (discovery?.activeScan) {
        emit('tcp_scan', 'running');
        try {
            const r = await discovery.activeScan();
            const refreshed = await discovery.getServers?.();
            const verified = (refreshed || []).find((s: any) => s.verified);
            if (verified?.url) {
                return finish('tcp_scan', verified.url);
            }
            emit('tcp_scan', 'fail', {
                note: `scanned ${r?.scanned ?? 0} hosts, ${r?.found ?? 0} found, none verified`,
            });
        } catch (e: any) {
            emit('tcp_scan', 'fail', { note: e?.message || 'scan error' });
        }
    } else {
        emit('tcp_scan', 'skipped', { note: 'activeScan not available' });
    }

    // 5. Remote rendezvous tunnel.
    const rendezvous = (opts.rendezvousUrl || '').trim();
    if (rendezvous) {
        emit('rendezvous', 'running');
        if (await probeUrl(rendezvous, 4000)) return finish('rendezvous', rendezvous);
        emit('rendezvous', 'fail', { note: 'tunnel did not respond' });
    } else {
        emit('rendezvous', 'skipped', { note: 'no rendezvous URL configured' });
    }

    return { ok: false, attempts: events };
}
