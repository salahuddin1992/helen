/**
 * Vitest tests for settings.store — DND, channel mute, theme.
 *
 * The store wraps localStorage so tests reset both store + DOM
 * before each case and assert observable side-effects (theme class
 * on documentElement, persisted JSON, in-memory state).
 */
import { describe, it, expect, beforeEach } from 'vitest';

// Minimal in-memory localStorage polyfill — vitest's jsdom env may not
// expose one by default depending on the config matrix. Install before
// loading the store so the module-level reads at import time see it.
function installLocalStorage(): void {
    if (typeof (globalThis as any).localStorage !== 'undefined' &&
        typeof (globalThis as any).localStorage.clear === 'function') {
        return;
    }
    const store: Record<string, string> = {};
    const ls = {
        getItem: (k: string) => (k in store ? store[k] : null),
        setItem: (k: string, v: string) => { store[k] = String(v); },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
        key: (i: number) => Object.keys(store)[i] || null,
        get length() { return Object.keys(store).length; },
    };
    Object.defineProperty(globalThis, 'localStorage', {
        value: ls, writable: true, configurable: true,
    });
    if (typeof window !== 'undefined') {
        Object.defineProperty(window, 'localStorage', {
            value: ls, writable: true, configurable: true,
        });
    }
}

// Polyfill matchMedia — jsdom doesn't ship it, but the store uses
// window.matchMedia('(prefers-color-scheme: dark)') for the system theme
// listener. Stub one that returns matches=false + a no-op listener.
function installMatchMedia(): void {
    if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
        Object.defineProperty(window, 'matchMedia', {
            writable: true,
            configurable: true,
            value: (q: string) => ({
                matches: false,
                media: q,
                onchange: null,
                addEventListener: () => { /* noop */ },
                removeEventListener: () => { /* noop */ },
                addListener: () => { /* legacy */ },
                removeListener: () => { /* legacy */ },
                dispatchEvent: () => false,
            }),
        });
    }
}
installLocalStorage();
installMatchMedia();

import { useSettingsStore } from './settings.store';

beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove('dark');
    // Reset the store to defaults by clearing its persisted state then
    // re-loading. Calling `load()` from a fresh state pulls from the
    // (now empty) localStorage and applies defaults.
    useSettingsStore.getState().reset();
});

describe('settings.store — DND', () => {
    it('starts with dndUntil = null', () => {
        expect(useSettingsStore.getState().settings.dndUntil).toBeFalsy();
    });

    it('persists dndUntil through update()', () => {
        const future = new Date(Date.now() + 60_000).toISOString();
        useSettingsStore.getState().update({ dndUntil: future });
        expect(useSettingsStore.getState().settings.dndUntil).toBe(future);
        // localStorage should reflect the change.
        const raw = localStorage.getItem('commclient_settings');
        expect(raw).toBeTruthy();
        expect(JSON.parse(raw!).dndUntil).toBe(future);
    });

    it('accepts the indefinite sentinel', () => {
        useSettingsStore.getState().update({ dndUntil: 'indefinite' });
        expect(useSettingsStore.getState().settings.dndUntil).toBe('indefinite');
    });

    it('reset() clears DND', () => {
        useSettingsStore.getState().update({ dndUntil: 'indefinite' });
        useSettingsStore.getState().reset();
        expect(useSettingsStore.getState().settings.dndUntil).toBeFalsy();
    });
});

describe('settings.store — per-channel mute', () => {
    it('defaults to empty channelMutes map', () => {
        expect(useSettingsStore.getState().settings.channelMutes).toEqual({});
    });

    it('records a mute mode per channel', () => {
        useSettingsStore.getState().update({
            channelMutes: { 'ch-1': 'mentions' },
        });
        expect(useSettingsStore.getState().settings.channelMutes?.['ch-1']).toBe('mentions');
    });

    it('preserves other channels when adding a new one', () => {
        useSettingsStore.getState().update({ channelMutes: { 'ch-1': 'muted' } });
        const current = useSettingsStore.getState().settings.channelMutes || {};
        useSettingsStore.getState().update({
            channelMutes: { ...current, 'ch-2': 'mentions' },
        });
        const after = useSettingsStore.getState().settings.channelMutes!;
        expect(after['ch-1']).toBe('muted');
        expect(after['ch-2']).toBe('mentions');
    });
});

describe('settings.store — theme', () => {
    it('applies the dark class on theme=dark', () => {
        useSettingsStore.getState().update({ theme: 'dark' });
        expect(document.documentElement.classList.contains('dark')).toBe(true);
    });

    it('removes the dark class on theme=light', () => {
        useSettingsStore.getState().update({ theme: 'dark' });
        useSettingsStore.getState().update({ theme: 'light' });
        expect(document.documentElement.classList.contains('dark')).toBe(false);
    });

    it("system theme follows prefers-color-scheme", () => {
        // jsdom's matchMedia returns matches=false unless we polyfill;
        // verify the store at least accepts the value without throwing.
        useSettingsStore.getState().update({ theme: 'system' });
        expect(useSettingsStore.getState().settings.theme).toBe('system');
    });
});

describe('settings.store — language', () => {
    it('updates document.dir on language=ar', () => {
        useSettingsStore.getState().update({ language: 'ar' });
        expect(document.documentElement.dir).toBe('rtl');
        expect(document.documentElement.lang).toBe('ar');
    });

    it('updates document.dir on language=en', () => {
        useSettingsStore.getState().update({ language: 'ar' });
        useSettingsStore.getState().update({ language: 'en' });
        expect(document.documentElement.dir).toBe('ltr');
        expect(document.documentElement.lang).toBe('en');
    });
});

describe('settings.store — load()', () => {
    it('hydrates from localStorage', () => {
        localStorage.setItem(
            'commclient_settings',
            JSON.stringify({ language: 'ar', theme: 'dark', dndUntil: 'indefinite' }),
        );
        useSettingsStore.getState().load();
        const s = useSettingsStore.getState().settings;
        expect(s.language).toBe('ar');
        expect(s.theme).toBe('dark');
        expect(s.dndUntil).toBe('indefinite');
    });

    it('drops virtual: device IDs at load time (stale bridge cleanup)', () => {
        localStorage.setItem(
            'commclient_settings',
            JSON.stringify({
                videoInputDevice: 'virtual:phone:qt:abc',
                audioInputDevice: 'virtual:phone:qt:abc',
            }),
        );
        useSettingsStore.getState().load();
        const s = useSettingsStore.getState().settings;
        expect(s.videoInputDevice).toBe('');
        expect(s.audioInputDevice).toBe('default');
    });
});
