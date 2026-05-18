/**
 * Discovery Store — manages auto-discovered LAN servers in the renderer.
 *
 * Lifecycle:
 *   1. On mount, subscribes to IPC 'discovery:servers-updated' push events.
 *   2. Also polls on init and periodically (fallback if push events are missed).
 *   3. Exposes `bestServer` — the top-ranked verified server for auto-connect.
 *   4. `autoConnectUrl` is the computed URL for the auth store to use.
 *
 * Auto-connect flow:
 *   - If a saved server URL exists in credentials, try that first.
 *   - If that fails (or no saved URL), use the best discovered server.
 *   - If no servers discovered, show "Searching..." then fallback to manual entry.
 */

import { create } from 'zustand';

// ── Types ─────────────────────────────────────────────────

export interface DiscoveredServer {
  server_id: string;
  name: string;
  host: string;
  port: number;
  version: string;
  uptime: number;
  users_online: number;
  protocol: string;
  url: string;
  verified: boolean;
  last_seen: number;
  discovery_method: 'udp' | 'mdns' | 'manual';
  rtt_ms: number | null;
}

type DiscoveryPhase =
  | 'idle'           // Not started
  | 'searching'      // Listening for broadcasts, no servers found yet
  | 'found'          // At least one server discovered
  | 'verified'       // At least one server verified via HTTP
  | 'failed';        // Timeout — no servers found after search period

type NetworkStatus = 'online' | 'offline' | 'reconnecting' | 'unknown';

// ── Store Interface ───────────────────────────────────────

interface DiscoveryState {
  servers: DiscoveredServer[];
  phase: DiscoveryPhase;
  bestServer: DiscoveredServer | null;
  autoConnectUrl: string | null;
  searchTimeoutMs: number;
  isManualMode: boolean;
  networkStatus: NetworkStatus;
  reconnectAttempt: number;

  // Actions
  startSearching: () => void;
  stopSearching: () => void;
  setServers: (servers: DiscoveredServer[]) => void;
  addManualServer: (url: string) => Promise<DiscoveredServer | null>;
  findServerByCode: (code: string, timeoutMs?: number) => Promise<DiscoveredServer | null>;
  enableManualMode: () => void;
  disableManualMode: () => void;
  refresh: () => Promise<void>;
  restartDiscovery: () => Promise<void>;
}

// 64-char alphanumeric — same alphabet the server uses for its server_id
// and for user share_codes. Exported so the UI can preflight input.
export const SERVER_CODE_RE = /^[A-Za-z0-9]{64}$/;
export function isServerCode(s: string): boolean {
  return SERVER_CODE_RE.test(s.trim());
}

// ── Electron API access ───────────────────────────────────

const discoveryAPI = (window as any).electronAPI?.discovery;

// ── Store ─────────────────────────────────────────────────

let _cleanupListener: (() => void) | null = null;
let _networkListener: (() => void) | null = null;
let _searchTimeout: ReturnType<typeof setTimeout> | null = null;
let _pollInterval: ReturnType<typeof setInterval> | null = null;

export const useDiscoveryStore = create<DiscoveryState>((set, get) => ({
  servers: [],
  phase: 'idle',
  bestServer: null,
  autoConnectUrl: null,
  searchTimeoutMs: 8_000,  // 8 seconds before showing "not found"
  isManualMode: false,
  networkStatus: 'unknown',
  reconnectAttempt: 0,

  startSearching: () => {
    set({ phase: 'searching', servers: [], bestServer: null, autoConnectUrl: null });

    // Subscribe to push events from main process
    if (discoveryAPI?.onServersUpdated && !_cleanupListener) {
      _cleanupListener = discoveryAPI.onServersUpdated((servers: DiscoveredServer[]) => {
        get().setServers(servers);
      });
    }

    // Subscribe to network status changes from main process
    if (discoveryAPI?.onNetworkStatus && !_networkListener) {
      _networkListener = discoveryAPI.onNetworkStatus((data: { status: string; attempt: number }) => {
        const status = data.status as NetworkStatus;
        set({ networkStatus: status, reconnectAttempt: data.attempt });

        if (status === 'offline') {
          // Mark all servers as unverified in renderer state too
          const updated = get().servers.map((s) => ({ ...s, verified: false }));
          set({ servers: updated, phase: 'failed' });
        } else if (status === 'reconnecting') {
          set({ phase: 'searching' });
        } else if (status === 'online') {
          set({ reconnectAttempt: 0 });
          // Refresh server list
          if (discoveryAPI?.getServers) {
            discoveryAPI.getServers().then((servers: DiscoveredServer[]) => {
              if (servers?.length > 0) get().setServers(servers);
            });
          }
        }
      });
    }

    // Initial pull
    if (discoveryAPI?.getServers) {
      discoveryAPI.getServers().then((servers: DiscoveredServer[]) => {
        if (servers?.length > 0) {
          get().setServers(servers);
        }
      });
    }

    // Check initial network status
    if (discoveryAPI?.getNetworkStatus) {
      discoveryAPI.getNetworkStatus().then((ns: any) => {
        set({ networkStatus: ns.hasNetwork ? 'online' : 'offline' });
      });
    }

    // Periodic poll as fallback (every 3s)
    if (!_pollInterval) {
      _pollInterval = setInterval(() => {
        if (discoveryAPI?.getServers) {
          discoveryAPI.getServers().then((servers: DiscoveredServer[]) => {
            if (servers?.length > 0) {
              get().setServers(servers);
            }
          });
        }
      }, 3000);
    }

    // Timeout: if no verified server found within searchTimeoutMs, set phase to 'failed'
    if (_searchTimeout) clearTimeout(_searchTimeout);
    _searchTimeout = setTimeout(() => {
      const state = get();
      if (state.phase === 'searching') {
        set({ phase: 'failed' });
      }
    }, get().searchTimeoutMs);
  },

  stopSearching: () => {
    if (_cleanupListener) {
      _cleanupListener();
      _cleanupListener = null;
    }
    if (_networkListener) {
      _networkListener();
      _networkListener = null;
    }
    if (_searchTimeout) {
      clearTimeout(_searchTimeout);
      _searchTimeout = null;
    }
    if (_pollInterval) {
      clearInterval(_pollInterval);
      _pollInterval = null;
    }
  },

  setServers: (servers: DiscoveredServer[]) => {
    const verified = servers.filter((s) => s.verified);
    const best = verified[0] || servers[0] || null;
    const phase: DiscoveryPhase = verified.length > 0
      ? 'verified'
      : servers.length > 0
        ? 'found'
        : get().phase;

    // Clear the timeout if we found a verified server
    if (verified.length > 0 && _searchTimeout) {
      clearTimeout(_searchTimeout);
      _searchTimeout = null;
    }

    set({
      servers,
      bestServer: best,
      autoConnectUrl: best?.url || null,
      phase,
    });
  },

  findServerByCode: async (
    code: string,
    timeoutMs = 8_000,
  ): Promise<DiscoveredServer | null> => {
    const trimmed = code.trim();
    if (!isServerCode(trimmed)) return null;
    if (!discoveryAPI?.findByCode) return null;

    // Make sure the UDP listener is up — otherwise the waiter would never
    // see a broadcast. startSearching() is idempotent.
    if (get().phase === 'idle') get().startSearching();

    const server = await discoveryAPI.findByCode(trimmed, timeoutMs);
    if (server) {
      const current = get().servers;
      const updated = [...current.filter((s) => s.server_id !== server.server_id), server];
      get().setServers(updated);
    }
    return server;
  },

  addManualServer: async (url: string): Promise<DiscoveredServer | null> => {
    if (!discoveryAPI?.addManual) {
      // No Electron API — construct manually for web dev
      set({
        autoConnectUrl: url,
        isManualMode: true,
        phase: 'verified',
      });
      return null;
    }

    const server = await discoveryAPI.addManual(url);
    if (server) {
      const current = get().servers;
      const updated = [...current.filter((s) => s.server_id !== server.server_id), server];
      get().setServers(updated);
    }
    return server;
  },

  enableManualMode: () => set({ isManualMode: true }),
  disableManualMode: () => set({ isManualMode: false }),

  refresh: async () => {
    if (discoveryAPI?.refresh) {
      const servers = await discoveryAPI.refresh();
      if (servers?.length > 0) {
        get().setServers(servers);
      }
    }
  },

  restartDiscovery: async () => {
    // Full restart: stops and re-starts the main process discovery service
    if (discoveryAPI?.restart) {
      set({ phase: 'searching', servers: [], bestServer: null, autoConnectUrl: null, networkStatus: 'reconnecting' });
      await discoveryAPI.restart();
      // Re-subscribe since restart clears state
      get().stopSearching();
      get().startSearching();
    }
  },
}));
