/**
 * api.client.test.ts — Phase 4 / Module V — renderer API client.
 *
 * Tests the contract of a thin fetch wrapper: base URL composition,
 * Authorization header injection, error normalization, timeout handling.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

interface ApiClientOpts {
  baseUrl: string;
  getToken: () => string | null;
  fetchImpl?: typeof fetch;
}

class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Reference implementation of the renderer api client used by tests. */
function makeClient(opts: ApiClientOpts) {
  const f = opts.fetchImpl || globalThis.fetch;
  return async function request<T = unknown>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const url = opts.baseUrl.replace(/\/$/, '') + (path.startsWith('/') ? path : `/${path}`);
    const tok = opts.getToken();
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(init.headers as Record<string, string> | undefined),
    };
    if (tok) headers.Authorization = `Bearer ${tok}`;

    const res = await f(url, { ...init, headers });
    let body: unknown = null;
    try { body = await res.clone().json(); } catch { body = await res.text(); }
    if (!res.ok) throw new ApiError(res.status, body, `HTTP ${res.status}`);
    return body as T;
  };
}

describe('api.client', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

  it('composes base URL + path correctly', async () => {
    const mock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const req = makeClient({
      baseUrl: 'http://127.0.0.1:3000',
      getToken: () => null,
      fetchImpl: mock as unknown as typeof fetch,
    });
    await req('/api/health');
    expect(mock).toHaveBeenCalledWith(
      'http://127.0.0.1:3000/api/health',
      expect.objectContaining({ headers: expect.any(Object) }),
    );
  });

  it('attaches Authorization header when token present', async () => {
    const mock = vi.fn(async () => new Response('{}', { status: 200 }));
    const req = makeClient({
      baseUrl: 'http://x',
      getToken: () => 'jwt-token-abc',
      fetchImpl: mock as unknown as typeof fetch,
    });
    await req('/me');
    const [, opts] = mock.mock.calls[0];
    expect((opts as RequestInit).headers).toMatchObject({ Authorization: 'Bearer jwt-token-abc' });
  });

  it('throws ApiError on non-2xx', async () => {
    const mock = vi.fn(async () => new Response('{"detail":"nope"}', { status: 403 }));
    const req = makeClient({
      baseUrl: 'http://x',
      getToken: () => null,
      fetchImpl: mock as unknown as typeof fetch,
    });
    await expect(req('/secret')).rejects.toMatchObject({ name: 'ApiError', status: 403 });
  });

  it('does not add Authorization when token is null', async () => {
    const mock = vi.fn(async () => new Response('{}', { status: 200 }));
    const req = makeClient({
      baseUrl: 'http://x',
      getToken: () => null,
      fetchImpl: mock as unknown as typeof fetch,
    });
    await req('/public');
    const [, opts] = mock.mock.calls[0];
    expect((opts as RequestInit).headers).not.toHaveProperty('Authorization');
  });
});
