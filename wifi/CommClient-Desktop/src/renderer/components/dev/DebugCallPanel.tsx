/**
 * DebugCallPanel — floating in-app diagnostic surface.
 *
 * Toggle with `Ctrl+Shift+D`. Renders the live call lifecycle state, the
 * underlying engine status, retry counts, the last error, and the rolling
 * log buffer. Provides three buttons for manual fault-injection so we can
 * exercise retry/reconnection paths without flaking real hardware.
 *
 * Stays out of the production build's user-visible surface — it's only
 * mounted when the user opens it. Keep it small; this is a debugging
 * tool, not a feature.
 */

import React, { useEffect, useRef, useState } from 'react';
import { callController, type CallControllerSnapshot } from '@/services/call/CallController';
import { callErrorLog, type LogEntry } from '@/services/call/CallErrorLog';
import { useCallStore } from '@/stores/call.store.v2';

export const DebugCallPanel: React.FC = () => {
    const [open, setOpen] = useState(false);
    const [snap, setSnap] = useState<CallControllerSnapshot>(callController.snapshot);
    const [logs, setLogs] = useState<LogEntry[]>(callErrorLog.snapshot());
    const logEnd = useRef<HTMLDivElement>(null);

    const engineStatus = useCallStore((s) => s.status);
    const engineCallId = useCallStore((s) => s.callId);

    // Toggle hotkey + Esc to close.
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
                e.preventDefault();
                setOpen((v) => !v);
            } else if (e.key === 'Escape' && open) {
                setOpen(false);
            }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open]);

    useEffect(() => callController.subscribe(setSnap), []);
    useEffect(() => callErrorLog.subscribe(setLogs), []);

    // Auto-scroll to newest log entry.
    useEffect(() => {
        if (open) logEnd.current?.scrollIntoView({ behavior: 'auto', block: 'end' });
    }, [logs.length, open]);

    if (!open) return null;

    return (
        <div
            role="dialog"
            aria-label="Call debug panel"
            style={{
                position: 'fixed', right: 16, bottom: 16, zIndex: 99_999,
                width: 460, maxHeight: '70vh',
                background: 'rgba(15, 18, 26, 0.96)',
                color: '#e6e9f1',
                fontFamily: 'ui-monospace, SFMono-Regular, monospace',
                fontSize: 12, lineHeight: 1.4,
                border: '1px solid rgba(120, 130, 160, 0.35)',
                borderRadius: 12,
                boxShadow: '0 16px 40px rgba(0,0,0,0.55)',
                display: 'flex', flexDirection: 'column',
                backdropFilter: 'blur(8px)',
            }}
        >
            <Header onClose={() => setOpen(false)} />
            <Stat label="Lifecycle"      value={snap.state} tone={tone(snap.state)} />
            <Stat label="Engine status"  value={engineStatus || '—'} />
            <Stat label="Call ID"        value={snap.callId || engineCallId || '—'} mono />
            <Stat label="Retry attempt"  value={String(snap.retryCount)} />
            <Stat label="Locks"          value={
                [snap.isStartingCall && 'starting',
                 snap.isEndingCall   && 'ending',
                 snap.isRetrying     && 'retrying']
                    .filter(Boolean).join(', ') || 'none'
            } />
            <Stat label="Last error"     value={snap.lastError || '—'}
                  tone={snap.lastError ? 'error' : 'normal'} />
            <Actions />
            <Logs entries={logs} endRef={logEnd} />
        </div>
    );
};

// ── Sub-views ─────────────────────────────────────────

const Header: React.FC<{ onClose: () => void }> = ({ onClose }) => (
    <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '10px 14px', borderBottom: '1px solid rgba(120,130,160,0.25)',
    }}>
        <strong style={{ letterSpacing: 0.4 }}>Call Debug</strong>
        <span style={{ color: '#8a93a6' }}>
            Ctrl+Shift+D &nbsp;·&nbsp;
            <button onClick={onClose} style={btnStyle()}>close</button>
        </span>
    </div>
);

const Stat: React.FC<{
    label: string; value: string; mono?: boolean;
    tone?: 'normal' | 'error' | 'success' | 'warn' | string;
}> = ({ label, value, mono, tone }) => (
    <div style={{
        display: 'flex', justifyContent: 'space-between',
        padding: '4px 14px',
    }}>
        <span style={{ color: '#8a93a6' }}>{label}</span>
        <span style={{
            color: toneColor(tone),
            fontFamily: mono ? 'ui-monospace, monospace' : undefined,
            maxWidth: '60%', overflow: 'hidden', textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
        }} title={value}>
            {value}
        </span>
    </div>
);

const Actions: React.FC = () => (
    <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 6,
        padding: '10px 14px',
        borderTop: '1px solid rgba(120,130,160,0.25)',
    }}>
        <button style={btnStyle()} onClick={() => callErrorLog.clear()}>
            Clear logs
        </button>
        <button style={btnStyle()} onClick={() => {
            try { throw new Error('Simulated error from DebugCallPanel'); }
            catch (e) { callErrorLog.error('DebugPanel', 'Simulate Error', e); }
        }}>
            Simulate Error
        </button>
        <button style={btnStyle()} onClick={() => {
            // Drop the socket transport to exercise reconnect paths.
            callErrorLog.warn('DebugPanel', 'Simulating network drop');
            try {
                // @ts-expect-error window.__socketManager is exposed by socket.manager
                window.__socketManager?.disconnect?.();
            } catch (e) {
                callErrorLog.error('DebugPanel', 'Network drop simulation failed', e);
            }
        }}>
            Simulate Network Drop
        </button>
        <button style={btnStyle()} onClick={() => callController.reset()}>
            Reset
        </button>
    </div>
);

const Logs: React.FC<{ entries: LogEntry[]; endRef: React.RefObject<HTMLDivElement> }> =
    ({ entries, endRef }) => (
    <div style={{
        flex: 1, overflowY: 'auto', padding: '8px 14px',
        borderTop: '1px solid rgba(120,130,160,0.25)',
        background: 'rgba(0,0,0,0.18)',
        minHeight: 140,
    }}>
        {entries.length === 0 && (
            <div style={{ color: '#5e6678', textAlign: 'center', padding: 20 }}>
                No log entries yet
            </div>
        )}
        {entries.slice(-200).map((e) => (
            <div key={e.id} style={{ marginBottom: 4 }}>
                <span style={{ color: '#5e6678' }}>
                    {new Date(e.ts).toLocaleTimeString()} ·
                </span>{' '}
                <span style={{ color: levelColor(e.level), fontWeight: 600 }}>
                    [{e.tag}]
                </span>{' '}
                <span>{e.message}</span>
                {e.detail && (
                    <pre style={{
                        margin: '2px 0 4px 0',
                        color: '#a3aac0',
                        fontSize: 11,
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                    }}>{e.detail}</pre>
                )}
            </div>
        ))}
        <div ref={endRef} />
    </div>
);

// ── Style helpers ─────────────────────────────────────

const btnStyle = (): React.CSSProperties => ({
    background: 'rgba(78, 96, 132, 0.25)',
    color: '#e6e9f1',
    border: '1px solid rgba(120,130,160,0.35)',
    padding: '4px 9px',
    borderRadius: 6,
    fontSize: 11,
    cursor: 'pointer',
    fontFamily: 'inherit',
});

function tone(state: string): string {
    switch (state) {
        case 'connected': return 'success';
        case 'failed':    return 'error';
        case 'reconnecting':
        case 'requestingPermissions':
        case 'connecting':
        case 'preparing': return 'warn';
        default:          return 'normal';
    }
}

function toneColor(t: string | undefined): string {
    switch (t) {
        case 'success': return '#5cd6a8';
        case 'error':   return '#ff6f7d';
        case 'warn':    return '#f0c674';
        default:        return '#e6e9f1';
    }
}

function levelColor(l: LogEntry['level']): string {
    switch (l) {
        case 'error': return '#ff6f7d';
        case 'warn':  return '#f0c674';
        default:      return '#7fb6ff';
    }
}
