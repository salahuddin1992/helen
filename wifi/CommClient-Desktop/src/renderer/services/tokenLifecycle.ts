/**
 * tokenLifecycle — pre-emptive refresh of the JWT access token so a
 * long-lived socket session never sees its bearer expire mid-call.
 *
 * Why pre-emptive instead of reactive (refresh on 401)?
 *  • Socket.IO connect validates the JWT once. Once accepted, the
 *    socket has no automatic re-auth path. A reactive 401-on-HTTP
 *    refresh works for REST but a socket happily ferries events for
 *    hours past token expiry — until the next reconnect tries to
 *    re-attach with the dead token, fails, and the user is bounced.
 *  • An ongoing call (>JWT_ACCESS_TOKEN_EXPIRE_MINUTES) can absolutely
 *    outlive a single token. We schedule a refresh ~60s before the
 *    token's exp claim so the new token is in place before anything
 *    notices.
 *
 * The scheduler is idempotent: arm() cancels any existing timer and
 * re-arms based on the new token's exp. cancel() drops everything.
 */

import { socketManager } from './socket.manager';
import { refreshTokensIfPossible } from './api.client';

let _timer: ReturnType<typeof setTimeout> | null = null;
// We refresh this many seconds before the token's exp claim. 60s is
// enough to absorb clock skew + one network round-trip without leaving
// a window where the token is technically expired.
const REFRESH_LEAD_SECONDS = 60;
// Hard floor — never schedule less than this far out. Avoids pathological
// busy-loops if a backend issues near-expired tokens.
const MIN_DELAY_SECONDS = 30;

interface JwtPayload {
  exp?: number;
  iat?: number;
  sub?: string;
}

function decodeJwt(token: string): JwtPayload | null {
  try {
    const part = token.split('.')[1];
    if (!part) return null;
    // base64url → base64 → utf-8 JSON
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    return JSON.parse(atob(padded));
  } catch {
    return null;
  }
}

/**
 * Schedule a refresh attempt N seconds before the access token expires.
 * Call this every time a fresh access token is obtained (login, restore,
 * after a successful refresh).
 *
 * @param accessToken  The current access JWT.
 * @param getRefresh   Lazy accessor for the refresh token. Lazy because
 *                     the caller's tokens object may be replaced before
 *                     this timer fires (rotation).
 */
export function arm(accessToken: string, getRefresh: () => string | null): void {
  cancel();
  const payload = decodeJwt(accessToken);
  if (!payload?.exp) return;

  const nowSec = Math.floor(Date.now() / 1000);
  const secsUntilExp = payload.exp - nowSec;
  const delaySec = Math.max(MIN_DELAY_SECONDS, secsUntilExp - REFRESH_LEAD_SECONDS);

  // If the token already expired or expires in <30s, fire immediately.
  // The refresh path is single-flight so concurrent calls collapse.
  const ms = Math.max(0, delaySec * 1000);
  _timer = setTimeout(() => { void run(getRefresh); }, ms);
}

export function cancel(): void {
  if (_timer) {
    clearTimeout(_timer);
    _timer = null;
  }
}

async function run(getRefresh: () => string | null): Promise<void> {
  _timer = null;
  const refresh = getRefresh();
  if (!refresh) return;

  // Prefer socket-level refresh — it's already authenticated, no extra
  // round-trip cost, and the response carries the new exp so we can
  // re-arm without an extra decode.
  let nextAccess: string | null = null;
  if (socketManager.isConnected()) {
    nextAccess = await socketManager.refreshAccessToken(refresh);
  }

  // Fall back to HTTP if socket is offline or the socket-level refresh
  // failed (e.g. server hasn't been updated yet).
  if (!nextAccess) {
    const ok = await refreshTokensIfPossible();
    if (!ok) {
      // Total failure — caller's onAuthFailed handler will fire on the
      // next 401 from a REST call. Don't busy-loop here.
      return;
    }
    // The HTTP path doesn't return the new access token directly; it
    // mutates module-level state and fires _onTokenRefreshed.
    // arm() will be re-called from that callback path.
    return;
  }

  // Socket-level refresh succeeded — caller's auth.store re-arms via
  // the same callback flow as the HTTP path.
  arm(nextAccess, getRefresh);
}
