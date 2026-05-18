/**
 * CallErrorLog — append-only ring buffer of recent call-subsystem events.
 *
 * Every state transition, retry attempt, timeout, and error gets a row here
 * so the in-app DebugCallPanel can render an accurate timeline without
 * needing to re-attach to live event streams.
 */

export type LogLevel = 'info' | 'warn' | 'error';

export interface LogEntry {
    id: number;
    ts: number;
    level: LogLevel;
    tag: string;          // e.g. "CallController", "Engine"
    message: string;
    detail?: string;      // stack trace, JSON snapshot, etc.
}

const MAX_ENTRIES = 500;

class _CallErrorLog {
    private entries: LogEntry[] = [];
    private nextId = 1;
    private listeners = new Set<(snapshot: LogEntry[]) => void>();

    log(level: LogLevel, tag: string, message: string, detail?: unknown) {
        const entry: LogEntry = {
            id: this.nextId++,
            ts: Date.now(),
            level,
            tag,
            message,
            detail: detail === undefined ? undefined : this.fmt(detail),
        };
        this.entries.push(entry);
        if (this.entries.length > MAX_ENTRIES) {
            this.entries.splice(0, this.entries.length - MAX_ENTRIES);
        }

        // Mirror to console so developers see it without opening the panel.
        const line = `[${tag}] ${message}` + (entry.detail ? `\n${entry.detail}` : '');
        if (level === 'error')      console.error(line);
        else if (level === 'warn')  console.warn(line);
        else                        console.log(line);

        for (const l of this.listeners) {
            try { l(this.snapshot()); } catch { /* listener cannot break log */ }
        }
    }

    info (tag: string, msg: string, detail?: unknown) { this.log('info',  tag, msg, detail); }
    warn (tag: string, msg: string, detail?: unknown) { this.log('warn',  tag, msg, detail); }
    error(tag: string, msg: string, detail?: unknown) { this.log('error', tag, msg, detail); }

    snapshot(): LogEntry[] {
        return [...this.entries];
    }

    clear() {
        this.entries = [];
        for (const l of this.listeners) {
            try { l([]); } catch { /* */ }
        }
    }

    subscribe(listener: (snapshot: LogEntry[]) => void): () => void {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    }

    private fmt(v: unknown): string {
        if (v instanceof Error) {
            return `${v.name}: ${v.message}\n${v.stack ?? ''}`;
        }
        if (typeof v === 'string') return v;
        try { return JSON.stringify(v, null, 2); } catch { return String(v); }
    }
}

export const callErrorLog = new _CallErrorLog();
