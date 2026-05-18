/**
 * HealthCheck — periodic client diagnostics for the four flows the
 * user asked us to verify continuously: the server is reachable,
 * the camera works, the microphone works, and chat (the
 * authenticated REST surface) responds.
 *
 * Each indicator is a colored pill:
 *   green  = passed last check
 *   red    = failed last check (with reason on hover)
 *   yellow = currently checking
 *   gray   = never run
 *
 * The whole panel auto-runs every 30 s by default and exposes a
 * "Run now" button + a per-row click target so the user can re-test
 * a single capability without waiting for the timer.
 *
 * Each check is independent — if the camera fails, audio and chat
 * still get their own result. We do not abort the panel on the
 * first failure.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
    Server, Video, Mic, MessageSquare, RefreshCw, Check, X, Loader, Globe,
} from 'lucide-react';
import { api } from '@/services/api.client';
import { useAuthStore } from '@/stores/auth.store';

type CheckId = 'server' | 'uplink' | 'video' | 'audio' | 'chat';

interface CheckResult {
    state: 'idle' | 'running' | 'ok' | 'fail';
    message?: string;
    latencyMs?: number;
    at?: number;
}

const INITIAL: Record<CheckId, CheckResult> = {
    server: { state: 'idle' },
    uplink: { state: 'idle' },
    video:  { state: 'idle' },
    audio:  { state: 'idle' },
    chat:   { state: 'idle' },
};

const AUTO_INTERVAL_MS = 30_000;

// Probe that grabs a track for ~250 ms then releases it. Using a long
// stream would compete with active calls; we just want to confirm the
// device is grantable + producing data right now.
async function probeMedia(constraints: MediaStreamConstraints): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    try {
        const tracks = stream.getTracks();
        if (!tracks.length) throw new Error('no tracks returned');
        // For audio, do a quick "is this stream actually producing samples?"
        // check by reading a few frames from an AnalyserNode. For video the
        // existence of an active track + readyState='live' is sufficient.
        if ('audio' in constraints && constraints.audio) {
            const ctx  = new (window.AudioContext || (window as any).webkitAudioContext)();
            try {
                const src  = ctx.createMediaStreamSource(stream);
                const an   = ctx.createAnalyser();
                an.fftSize = 256;
                src.connect(an);
                // Just ensure the graph is live — we don't fail on silence
                // because the user might be in a quiet room.
                const buf = new Uint8Array(an.frequencyBinCount);
                an.getByteFrequencyData(buf);
            } finally {
                try { await ctx.close(); } catch {}
            }
        }
        const live = tracks.some((t) => t.readyState === 'live');
        if (!live) throw new Error('track ended immediately');
    } finally {
        stream.getTracks().forEach((t) => { try { t.stop(); } catch {} });
    }
}

async function runCheck(id: CheckId): Promise<CheckResult> {
    const t0 = performance.now();
    try {
        switch (id) {
            case 'server':
                await api.health();
                break;
            case 'uplink': {
                // Reports whether the local Helen-Server is currently
                // linked to a parent rendezvous (so external clients can
                // reach this server from the public internet). Treat
                // "not configured" as a non-failure — the link is
                // optional. Only "configured but not connected" counts
                // as red.
                const r = await api.uplink();
                if (!r.configured) {
                    return {
                        state: 'ok',
                        message: 'no parent configured (LAN-only)',
                        latencyMs: Math.round(performance.now() - t0),
                        at: Date.now(),
                    };
                }
                if (!r.connected) {
                    throw new Error(`parent server unreachable${r.ws_url ? ` (${r.ws_url})` : ''}`);
                }
                return {
                    state: 'ok',
                    message: `linked via ${r.method ?? 'tunnel'}${r.public_id ? ` · id ${r.public_id.slice(0, 8)}` : ''}`,
                    latencyMs: Math.round(performance.now() - t0),
                    at: Date.now(),
                };
            }
            case 'video':
                await probeMedia({ video: true });
                break;
            case 'audio':
                await probeMedia({ audio: true });
                break;
            case 'chat':
                // Lightweight authenticated call — exercises the same JWT +
                // route prefix the chat UI uses without sending a message.
                await api.listChannels();
                break;
        }
        return {
            state: 'ok',
            latencyMs: Math.round(performance.now() - t0),
            at: Date.now(),
        };
    } catch (err: any) {
        return {
            state: 'fail',
            message: err?.message || String(err) || 'unknown error',
            latencyMs: Math.round(performance.now() - t0),
            at: Date.now(),
        };
    }
}

const ROWS: Array<{ id: CheckId; label: string; sub: string; icon: React.ReactNode }> = [
    { id: 'server', label: 'Server',        sub: '/api/health',         icon: <Server        size={16} /> },
    { id: 'uplink', label: 'Server uplink', sub: 'parent / rendezvous', icon: <Globe         size={16} /> },
    { id: 'video',  label: 'Camera',        sub: 'getUserMedia(video)', icon: <Video         size={16} /> },
    { id: 'audio',  label: 'Microphone',    sub: 'getUserMedia(audio)', icon: <Mic           size={16} /> },
    { id: 'chat',   label: 'Chat',          sub: 'GET /api/channels',   icon: <MessageSquare size={16} /> },
];

function Pill({ result }: { result: CheckResult }) {
    if (result.state === 'running') {
        return (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-yellow-500/15 text-yellow-300">
                <Loader size={12} className="animate-spin" />
                checking
            </span>
        );
    }
    if (result.state === 'ok') {
        return (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-green-500/15 text-green-400" title={`${result.latencyMs ?? '?'} ms`}>
                <Check size={12} />
                ok{typeof result.latencyMs === 'number' ? ` · ${result.latencyMs}ms` : ''}
            </span>
        );
    }
    if (result.state === 'fail') {
        return (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-red-500/15 text-red-400" title={result.message || ''}>
                <X size={12} />
                fail
            </span>
        );
    }
    return (
        <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-surface-800 text-text-500">
            —
        </span>
    );
}

export const HealthCheck: React.FC = () => {
    const isAuthed = useAuthStore((s) => s.isAuthenticated);
    const [results,    setResults]    = useState<Record<CheckId, CheckResult>>(INITIAL);
    // Manual-only by default — the user explicitly asked that the
    // diagnostics never fire on their own. They can opt into the
    // periodic timer via the checkbox if they want it back.
    const [autoRun,    setAutoRun]    = useState<boolean>(false);
    const [intervalMs, setIntervalMs] = useState<number>(AUTO_INTERVAL_MS);
    const [running,    setRunning]    = useState<boolean>(false);

    // ── Single-row run, awaitable ───────────────────────────────────
    const runOne = useCallback(async (id: CheckId) => {
        setResults((r) => ({ ...r, [id]: { ...r[id], state: 'running' } }));
        const res = await runCheck(id);
        setResults((r) => ({ ...r, [id]: res }));
    }, []);

    // ── All rows in parallel — `chat` is gated on auth, others always run
    const runAll = useCallback(async () => {
        if (running) return;
        setRunning(true);
        try {
            const ids: CheckId[] = isAuthed
                ? ['server', 'uplink', 'video', 'audio', 'chat']
                : ['server', 'uplink', 'video', 'audio'];
            await Promise.all(ids.map(runOne));
        } finally {
            setRunning(false);
        }
    }, [running, isAuthed, runOne]);

    // ── Manual-only — no auto-run on mount. User must click "Run now"
    //    or a per-row tile to fire a check. (Previously this kicked
    //    off a full sweep on first render, which the user explicitly
    //    asked us to remove.)

    // ── Auto-rerun timer ─────────────────────────────────────────────
    useEffect(() => {
        if (!autoRun) return;
        const id = window.setInterval(() => {
            // Skip if a manual run is still in flight; the timer will
            // catch the next tick.
            if (!running) runAll();
        }, intervalMs);
        return () => window.clearInterval(id);
    }, [autoRun, intervalMs, running, runAll]);

    const overall = (() => {
        const states = Object.values(results).map((r) => r.state);
        if (states.includes('fail')) return 'fail';
        if (states.includes('running')) return 'running';
        if (states.every((s) => s === 'ok')) return 'ok';
        return 'idle';
    })();

    return (
        <div className="space-y-3">
            {/* Header — overall status + actions */}
            <div className="flex items-center justify-between gap-3 p-3 rounded-lg bg-surface-900 border border-surface-800">
                <div className="flex items-center gap-2">
                    <span
                        className={
                            'w-2.5 h-2.5 rounded-full ' +
                            (overall === 'ok'      ? 'bg-green-500'
                             : overall === 'fail'   ? 'bg-red-500'
                             : overall === 'running' ? 'bg-yellow-400 animate-pulse'
                             : 'bg-surface-700')
                        }
                    />
                    <span className="text-sm font-medium text-text-100">
                        Client health: {overall === 'ok' ? 'all good' : overall === 'fail' ? 'something\'s off' : overall === 'running' ? 'checking…' : 'not yet run'}
                    </span>
                </div>
                <div className="flex items-center gap-2">
                    <label className="inline-flex items-center gap-2 text-xs text-text-400">
                        <input
                            type="checkbox"
                            checked={autoRun}
                            onChange={(e) => setAutoRun(e.target.checked)}
                            className="h-3.5 w-3.5 accent-blue-500"
                        />
                        every {Math.round(intervalMs / 1000)}s
                    </label>
                    <select
                        value={intervalMs}
                        onChange={(e) => setIntervalMs(Number(e.target.value))}
                        className="px-2 py-1 text-xs bg-surface-800 border border-surface-700 rounded"
                    >
                        <option value={10_000}>10s</option>
                        <option value={30_000}>30s</option>
                        <option value={60_000}>60s</option>
                        <option value={300_000}>5m</option>
                    </select>
                    <button
                        onClick={runAll}
                        disabled={running}
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white transition-colors"
                    >
                        <RefreshCw size={14} className={running ? 'animate-spin' : ''} />
                        Run now
                    </button>
                </div>
            </div>

            {/* Per-check rows */}
            <div className="rounded-lg bg-surface-900 border border-surface-800 divide-y divide-surface-800">
                {ROWS.map((row) => {
                    const r = results[row.id];
                    const disabled = row.id === 'chat' && !isAuthed;
                    return (
                        <button
                            key={row.id}
                            onClick={() => !disabled && runOne(row.id)}
                            disabled={disabled || r.state === 'running'}
                            className="w-full flex items-center gap-3 p-3 hover:bg-surface-800 disabled:hover:bg-transparent transition-colors text-left"
                            title={disabled
                                ? 'Sign in to test the chat path'
                                : 'Click to retest this check'}
                        >
                            <span className="text-text-400">{row.icon}</span>
                            <div className="flex-1 min-w-0">
                                <div className="text-sm font-medium text-text-100">{row.label}</div>
                                <div className="text-xs text-text-500 truncate">
                                    {r.message ? r.message : row.sub}
                                </div>
                            </div>
                            <Pill result={disabled ? { state: 'idle' } : r} />
                        </button>
                    );
                })}
            </div>
        </div>
    );
};
