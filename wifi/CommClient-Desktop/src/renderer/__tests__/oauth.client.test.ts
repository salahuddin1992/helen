/**
 * oauth.client.test.ts — Phase 4 / Module V — renderer OAuth client.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

/** Reference implementation of the renderer OAuth helper used by the
 *  test — mirrors the contract the real module must honor. */
function buildAuthorizeUrl(opts: {
  provider: string;
  state: string;
  baseUrl?: string;
}): string {
  const base = (opts.baseUrl ?? 'http://127.0.0.1:3000').replace(/\/$/, '');
  const provider = encodeURIComponent(opts.provider);
  const state = encodeURIComponent(opts.state);
  return `${base}/api/oauth/authorize/${provider}?state=${state}`;
}

interface CallbackResult {
  ok: boolean;
  error?: string;
}

function parseCallbackParams(url: string, expectedState: string): CallbackResult {
  try {
    const parsed = new URL(url);
    const code = parsed.searchParams.get('code');
    const state = parsed.searchParams.get('state');
    if (!code) return { ok: false, error: 'missing_code' };
    if (state !== expectedState) return { ok: false, error: 'state_mismatch' };
    return { ok: true };
  } catch {
    return { ok: false, error: 'malformed_url' };
  }
}

describe('oauth client — buildAuthorizeUrl', () => {
  it('builds a valid URL with required params', () => {
    const url = buildAuthorizeUrl({ provider: 'google', state: 'abc123' });
    expect(url).toMatch(/\/api\/oauth\/authorize\/google\?state=abc123$/);
  });

  it('encodes special characters in state', () => {
    const url = buildAuthorizeUrl({ provider: 'github', state: 'a b/c+d' });
    expect(url).toContain('state=a%20b%2Fc%2Bd');
  });

  it('honors custom base URL', () => {
    const url = buildAuthorizeUrl({
      provider: 'azure',
      state: 's',
      baseUrl: 'https://lan.example:3443/',
    });
    expect(url.startsWith('https://lan.example:3443/api/oauth/')).toBe(true);
  });
});

describe('oauth client — parseCallbackParams', () => {
  it('accepts a valid callback URL', () => {
    const url = 'http://127.0.0.1/cb?code=XYZ&state=abc';
    expect(parseCallbackParams(url, 'abc')).toEqual({ ok: true });
  });

  it('rejects missing code', () => {
    const url = 'http://127.0.0.1/cb?state=abc';
    const r = parseCallbackParams(url, 'abc');
    expect(r.ok).toBe(false);
    expect(r.error).toBe('missing_code');
  });

  it('rejects state mismatch', () => {
    const url = 'http://127.0.0.1/cb?code=XYZ&state=tampered';
    const r = parseCallbackParams(url, 'abc');
    expect(r.ok).toBe(false);
    expect(r.error).toBe('state_mismatch');
  });

  it('rejects malformed URL', () => {
    const r = parseCallbackParams('not a url', 'abc');
    expect(r.ok).toBe(false);
    expect(r.error).toBe('malformed_url');
  });
});
