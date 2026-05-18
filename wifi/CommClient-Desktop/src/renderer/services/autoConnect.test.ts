/**
 * Vitest unit tests for autoConnect — the chain that picks a Helen
 * server through (local → saved → LAN → TCP → rendezvous).
 *
 * We mock fetch (for probeUrl) and window.electronAPI.discovery so
 * the chain runs synthetically without a real Helen-Server.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { runAutoConnect } from './autoConnect';

// --- Mock window.electronAPI.discovery ------------------------------
const makeDiscovery = (overrides: Partial<any> = {}) => ({
    lanOrch: {
        run: vi.fn().mockResolvedValue({ winner: null, methods: {} }),
    },
    activeScan: vi.fn().mockResolvedValue({ scanned: 0, found: 0, durationMs: 100 }),
    getServers:  vi.fn().mockResolvedValue([]),
    ...overrides,
});

beforeEach(() => {
    (globalThis as any).fetch = vi.fn();
    (window as any).electronAPI = { discovery: makeDiscovery() };
});

describe('autoConnect chain', () => {
    it('returns local on successful local probe', async () => {
        (fetch as any).mockResolvedValueOnce({ ok: true });
        const result = await runAutoConnect();
        expect(result.ok).toBe(true);
        expect(result.via).toBe('local');
        expect(result.url).toBe('http://127.0.0.1:3000');
    });

    it('falls through to saved URL when local fails', async () => {
        (fetch as any).mockImplementation((url: string) => {
            if (url.includes('127.0.0.1:3088')) return Promise.resolve({ ok: false });
            if (url.includes('192.168.1.50:3088')) return Promise.resolve({ ok: true });
            return Promise.resolve({ ok: false });
        });
        const result = await runAutoConnect({ savedUrl: 'http://192.168.1.50:3088' });
        expect(result.ok).toBe(true);
        expect(result.via).toBe('saved');
        expect(result.url).toBe('http://192.168.1.50:3088');
    });

    it('tries alternate ports when saved URL fails', async () => {
        (fetch as any).mockImplementation((url: string) => {
            if (url.includes('192.168.1.50:3000')) return Promise.resolve({ ok: false });
            if (url.includes('192.168.1.50:3088')) return Promise.resolve({ ok: true });
            return Promise.resolve({ ok: false });
        });
        const result = await runAutoConnect({ savedUrl: 'http://192.168.1.50:3000' });
        expect(result.ok).toBe(true);
        // Alt-port success — recorded via 'saved' step
        expect(result.via).toBe('saved');
        // URL constructor adds trailing slash; either form is fine.
        expect(result.url?.replace(/\/$/, '')).toBe('http://192.168.1.50:3088');
    });

    it('falls back to LAN orchestrator when local + saved both fail', async () => {
        (fetch as any).mockResolvedValue({ ok: false });
        (window as any).electronAPI.discovery.lanOrch.run.mockResolvedValueOnce({
            winner: 'mdns_local',
            methods: { mdns_local: { serverUrl: 'http://192.168.1.42:3088' } },
        });
        // Probe of the discovered URL must succeed
        (fetch as any).mockImplementation((url: string) => {
            if (url.includes('192.168.1.42:3088')) return Promise.resolve({ ok: true });
            return Promise.resolve({ ok: false });
        });
        const result = await runAutoConnect();
        expect(result.ok).toBe(true);
        expect(result.via).toBe('lan');
    });

    it('falls back to rendezvous when nothing else works', async () => {
        (fetch as any).mockImplementation((url: string) => {
            if (url.includes('helen.example.com')) return Promise.resolve({ ok: true });
            return Promise.resolve({ ok: false });
        });
        const result = await runAutoConnect({
            rendezvousUrl: 'http://helen.example.com/t/abc',
        });
        expect(result.ok).toBe(true);
        expect(result.via).toBe('rendezvous');
    });

    it('returns ok:false when every path fails', async () => {
        (fetch as any).mockResolvedValue({ ok: false });
        const result = await runAutoConnect();
        expect(result.ok).toBe(false);
        expect(result.url).toBeUndefined();
        expect(result.attempts.length).toBeGreaterThan(0);
    });

    it('emits step events in order via onStep callback', async () => {
        (fetch as any).mockResolvedValue({ ok: false });
        const events: string[] = [];
        await runAutoConnect({ onStep: (e) => events.push(`${e.id}:${e.state}`) });
        // Expected order: local running→fail, saved skipped, lan running→skipped/fail, ...
        expect(events[0]).toBe('local:running');
        expect(events[1]).toBe('local:fail');
        expect(events.some((e) => e.startsWith('saved:'))).toBe(true);
        expect(events.some((e) => e.startsWith('lan:'))).toBe(true);
    });

    it('handles probe network exception as fail (not crash)', async () => {
        (fetch as any).mockRejectedValue(new Error('network unreachable'));
        const result = await runAutoConnect();
        // Should NOT throw — graceful degradation through all steps
        expect(result.ok).toBe(false);
    });
});
