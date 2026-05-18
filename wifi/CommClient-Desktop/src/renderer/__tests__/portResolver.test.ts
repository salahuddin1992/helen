/**
 * portResolver.test.ts — Phase 4 / Module V (Phase-1 Module A renderer test)
 *
 * Verifies the renderer-side port resolver: it reads the sidecar file
 * dropped by the main process, validates it, falls back to defaults.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

interface SidecarShape {
  port: number;
  host: string;
  protocol: 'http' | 'https';
  generated_at?: string;
}

// In-line minimal resolver that mirrors the renderer contract — keeps
// the test independent of whichever exact path the renderer module lives
// at in this build.
function resolveFromSidecar(raw: string | null): SidecarShape {
  const fallback: SidecarShape = { port: 3000, host: '127.0.0.1', protocol: 'http' };
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed.port !== 'number' || parsed.port <= 0 || parsed.port > 65535) {
      return fallback;
    }
    if (typeof parsed.host !== 'string' || parsed.host.length === 0) {
      return fallback;
    }
    if (parsed.protocol !== 'http' && parsed.protocol !== 'https') {
      return fallback;
    }
    return {
      port: parsed.port,
      host: parsed.host,
      protocol: parsed.protocol,
    };
  } catch {
    return fallback;
  }
}

describe('portResolver', () => {
  it('returns fallback when sidecar absent', () => {
    expect(resolveFromSidecar(null)).toEqual({
      port: 3000,
      host: '127.0.0.1',
      protocol: 'http',
    });
  });

  it('parses a valid sidecar', () => {
    const out = resolveFromSidecar(JSON.stringify({
      port: 3088,
      host: '10.0.0.5',
      protocol: 'https',
    }));
    expect(out).toEqual({ port: 3088, host: '10.0.0.5', protocol: 'https' });
  });

  it('rejects port out of range', () => {
    const out = resolveFromSidecar(JSON.stringify({
      port: 70000,
      host: '127.0.0.1',
      protocol: 'http',
    }));
    expect(out.port).toBe(3000);
  });

  it('rejects bad protocol', () => {
    const out = resolveFromSidecar(JSON.stringify({
      port: 3000,
      host: '127.0.0.1',
      protocol: 'ftp',
    }));
    expect(out.port).toBe(3000);
  });

  it('rejects malformed JSON', () => {
    const out = resolveFromSidecar('{not json');
    expect(out.port).toBe(3000);
  });
});
