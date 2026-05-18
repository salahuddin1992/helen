/**
 * Phase 3 / Module N — Desktop OAuth client.
 *
 * Coordinates the PKCE OAuth flow from the Electron renderer:
 *
 *   1) `startOAuth(provider)` — call the server's `/authorize` endpoint
 *      with a desktop redirect_uri pointing at the `helen://` custom
 *      protocol. The server records state + (server-side) code_verifier
 *      and returns the authorize URL + state + code_verifier.
 *
 *   2) Open the system browser via the main process
 *      (`window.helenAPI.openExternal(...)`).
 *
 *   3) Wait for the main process to forward the protocol callback.
 *      We register a one-shot listener on `helenAPI.onOAuthCallback`.
 *
 *   4) `completeOAuth(code, state, code_verifier)` — POST to the
 *      server's `/desktop/exchange` endpoint and return the JWT pair.
 *
 * The desktop is expected to expose, via preload, the following:
 *   window.helenAPI.openExternal(url: string): Promise<void>
 *   window.helenAPI.onOAuthCallback(cb: (params) => void): () => void
 *   window.helenAPI.getServerBaseUrl(): Promise<string>
 *
 * If `helenAPI` is missing (e.g. running inside a browser), we fall back
 * to `window.open` + `postMessage`.
 */

export interface OAuthAuthorizePayload {
  authorize_url: string;
  state: string;
  code_verifier: string | null;
  provider: string;
}

export interface OAuthTokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user_id: string;
  is_new_user: boolean;
  provider: string;
}

export interface OAuthCallbackParams {
  code: string;
  state: string;
}

const DEFAULT_DESKTOP_REDIRECT = 'helen://oauth/callback';

interface HelenAPI {
  openExternal?: (url: string) => Promise<void>;
  onOAuthCallback?: (cb: (p: OAuthCallbackParams) => void) => () => void;
  getServerBaseUrl?: () => Promise<string>;
}

function helen(): HelenAPI | undefined {
  return (globalThis as unknown as { helenAPI?: HelenAPI }).helenAPI;
}

async function getBaseUrl(): Promise<string> {
  const api = helen();
  if (api?.getServerBaseUrl) {
    try { return await api.getServerBaseUrl(); } catch { /* ignore */ }
  }
  // localStorage fallback — written by the connect screen.
  const fromStorage =
    (typeof localStorage !== 'undefined' && localStorage.getItem('serverBaseUrl')) ||
    '';
  if (fromStorage) return fromStorage.replace(/\/+$/, '');
  return '';
}

async function authHeaders(): Promise<HeadersInit> {
  const t = (typeof localStorage !== 'undefined' && localStorage.getItem('accessToken')) || '';
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export async function listProviders(): Promise<Array<{ name: string; scopes: string[] }>> {
  const base = await getBaseUrl();
  const r = await fetch(`${base}/api/oauth/providers`, { headers: await authHeaders() });
  if (!r.ok) throw new Error(`providers list failed (${r.status})`);
  return r.json();
}

export async function startOAuth(
  provider: string,
  opts: { redirectUri?: string } = {},
): Promise<OAuthAuthorizePayload> {
  const base = await getBaseUrl();
  const redirect = opts.redirectUri || DEFAULT_DESKTOP_REDIRECT;
  const url =
    `${base}/api/oauth/${encodeURIComponent(provider)}/authorize` +
    `?redirect_uri=${encodeURIComponent(redirect)}&desktop=1`;
  const r = await fetch(url, { headers: await authHeaders() });
  if (!r.ok) {
    const msg = await r.text();
    throw new Error(`authorize start failed: ${r.status} ${msg}`);
  }
  return (await r.json()) as OAuthAuthorizePayload;
}

export async function completeOAuth(
  provider: string,
  code: string,
  state: string,
  code_verifier: string | null,
): Promise<OAuthTokenPair> {
  const base = await getBaseUrl();
  const r = await fetch(`${base}/api/oauth/${encodeURIComponent(provider)}/desktop/exchange`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, state, code_verifier }),
  });
  if (!r.ok) {
    const msg = await r.text();
    throw new Error(`exchange failed: ${r.status} ${msg}`);
  }
  return (await r.json()) as OAuthTokenPair;
}

/**
 * End-to-end helper — kick off the flow, open the browser, wait for the
 * protocol callback, exchange tokens, persist them to localStorage and
 * return the token pair.
 *
 * Throws on user cancel (timeout default 5 minutes) or any error.
 */
export async function loginWithOAuth(
  provider: string,
  opts: { timeoutMs?: number; redirectUri?: string } = {},
): Promise<OAuthTokenPair> {
  const timeoutMs = opts.timeoutMs ?? 5 * 60 * 1000;
  const start = await startOAuth(provider, opts);

  const api = helen();
  let unsub: (() => void) | null = null;
  const callbackPromise = new Promise<OAuthCallbackParams>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | null = null;

    const finish = (p: OAuthCallbackParams) => {
      if (timer) clearTimeout(timer);
      if (unsub) unsub();
      resolve(p);
    };
    const fail = (e: Error) => {
      if (timer) clearTimeout(timer);
      if (unsub) unsub();
      reject(e);
    };

    if (api?.onOAuthCallback) {
      unsub = api.onOAuthCallback((p) => {
        if (p?.state && p.state === start.state) finish(p);
      });
    } else {
      // Browser fallback — `postMessage` from the popup window.
      const onMsg = (ev: MessageEvent) => {
        const d = ev.data;
        if (d?.type === 'helen-oauth' && d.access_token) {
          window.removeEventListener('message', onMsg);
          finish({ code: '__inline__', state: start.state });
          (start as OAuthAuthorizePayload & { _inline?: unknown })._inline = d;
        }
      };
      window.addEventListener('message', onMsg);
      unsub = () => window.removeEventListener('message', onMsg);
    }

    timer = setTimeout(() => fail(new Error('OAuth flow timed out.')), timeoutMs);
  });

  if (api?.openExternal) {
    await api.openExternal(start.authorize_url);
  } else if (typeof window !== 'undefined') {
    window.open(start.authorize_url, '_blank', 'width=500,height=700');
  }

  const cb = await callbackPromise;

  // Inline path (browser fallback) — tokens come straight from the popup.
  const inline = (start as OAuthAuthorizePayload & {
    _inline?: { access_token: string; refresh_token: string; user_id: string;
                is_new_user: boolean; provider: string };
  })._inline;
  if (cb.code === '__inline__' && inline) {
    persistTokens(inline.access_token, inline.refresh_token, inline.user_id);
    return {
      access_token: inline.access_token,
      refresh_token: inline.refresh_token,
      token_type: 'bearer',
      user_id: inline.user_id,
      is_new_user: inline.is_new_user,
      provider: inline.provider,
    };
  }

  const tokens = await completeOAuth(
    provider, cb.code, cb.state, start.code_verifier,
  );
  persistTokens(tokens.access_token, tokens.refresh_token, tokens.user_id);
  return tokens;
}

function persistTokens(access: string, refresh: string, userId: string): void {
  try {
    localStorage.setItem('accessToken', access);
    localStorage.setItem('refreshToken', refresh);
    localStorage.setItem('userId', userId);
  } catch { /* ignore (non-browser host) */ }
}

export async function listMyOAuthAccounts(): Promise<Array<{
  id: string; provider: string; provider_user_id: string;
  email: string | null; name: string | null; avatar_url: string | null;
  created_at: string;
}>> {
  const base = await getBaseUrl();
  const r = await fetch(`${base}/api/users/me/oauth-accounts`, { headers: await authHeaders() });
  if (!r.ok) throw new Error(`list linked accounts failed (${r.status})`);
  return r.json();
}

export async function unlinkOAuthAccount(id: string): Promise<void> {
  const base = await getBaseUrl();
  const r = await fetch(`${base}/api/users/me/oauth-accounts/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  });
  if (!r.ok && r.status !== 204) throw new Error(`unlink failed (${r.status})`);
}
