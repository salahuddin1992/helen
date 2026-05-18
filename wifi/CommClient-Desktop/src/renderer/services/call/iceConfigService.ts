/**
 * iceConfigService — fetch RTCConfiguration from the server, cache it
 * with TTL, and refresh before expiry.
 *
 * The server endpoint `/api/turn/ice-config` returns:
 *   {
 *     ice_servers: [
 *       { urls: ["stun:..."] },
 *       { urls: ["turn:...", "turn:...?transport=tcp"],
 *         username: "...", credential: "..." }
 *     ],
 *     ice_transport_policy: "all" | "relay",
 *     ttl_seconds: 3600,
 *     realm: "commclient.local"
 *   }
 *
 * Without this, every RTCPeerConnection in the app shipped with the
 * legacy hard-coded config (LAN-only or Google STUN). Cross-NAT and
 * carrier-grade-NAT users had no way to connect — listed in the
 * production NO-GO checklist as "TURN required but not configured".
 *
 * Cache strategy: refresh when ≤120s remain on the TTL or on demand
 * (call-start). Failure is non-fatal: we fall back to LAN-only so a
 * server without TURN still works on the local network.
 */

import { api } from '../api.client';

const REFRESH_BUFFER_SEC = 120;
const FALLBACK: RTCConfiguration = {
    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    iceTransportPolicy: 'all',
    bundlePolicy: 'max-bundle',
    rtcpMuxPolicy: 'require',
};

let cached: RTCConfiguration | null = null;
let expiresAt = 0;            // ms epoch

async function fetchFresh(): Promise<RTCConfiguration | null> {
    try {
        const resp = await (api as any).iceConfig?.();
        if (!resp) return null;
        const cfg: RTCConfiguration = {
            iceServers:          resp.ice_servers ?? [],
            iceTransportPolicy:  resp.ice_transport_policy === 'relay' ? 'relay' : 'all',
            bundlePolicy:        'max-bundle',
            rtcpMuxPolicy:       'require',
        };
        const ttl = Number(resp.ttl_seconds) || 3600;
        cached    = cfg;
        expiresAt = Date.now() + (ttl * 1000);
        console.log(`[ice] config refreshed — ${cfg.iceServers?.length ?? 0} servers, ttl=${ttl}s`);
        return cfg;
    } catch (err: any) {
        console.warn('[ice] fetch failed, will fall back to LAN-only:', err?.message);
        return null;
    }
}

/**
 * Return a working RTCConfiguration. Fetches from the server if cache
 * is stale; falls back to LAN+Google STUN if the server doesn't expose
 * the endpoint (older builds) or is unreachable.
 */
export async function getIceConfig(): Promise<RTCConfiguration> {
    if (cached && expiresAt - Date.now() > REFRESH_BUFFER_SEC * 1000) {
        return cached;
    }
    const fresh = await fetchFresh();
    return fresh ?? cached ?? FALLBACK;
}

/** Force-clear the cached config — useful after server URL changes. */
export function invalidateIceConfig(): void {
    cached = null;
    expiresAt = 0;
}
