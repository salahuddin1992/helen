/**
 * Server Identity Store — holds the connected server's 64-char federation
 * handle plus the LAN peers visible through it. Populated after login via
 * /api/peers/me + /api/peers.
 *
 * Used by:
 *   - TitleBar (chip showing short-form server_code)
 *   - AddContactModal (federation-scope hint)
 *   - Settings (copy full server_code)
 */

import { create } from 'zustand';
import { api } from '@/services/api.client';

interface PeerSummary {
  server_id: string;
  name: string;
  host: string;
  port: number;
}

interface ServerIdentityState {
  serverCode: string | null;   // 64-char handle
  serverName: string | null;
  host: string | null;
  port: number | null;
  peers: PeerSummary[];
  loading: boolean;
  error: string | null;

  load: () => Promise<void>;
  clear: () => void;
}

export const useServerIdentityStore = create<ServerIdentityState>((set) => ({
  serverCode: null,
  serverName: null,
  host: null,
  port: null,
  peers: [],
  loading: false,
  error: null,

  load: async () => {
    set({ loading: true, error: null });
    try {
      const [me, peers] = await Promise.all([
        api.getServerIdentity(),
        api.listPeers().catch(() => ({ self: null, peers: [], total: 0 })),
      ]);
      set({
        serverCode: me.server_code || me.server_id || null,
        serverName: me.name,
        host: me.host,
        port: me.port,
        peers: peers.peers || [],
        loading: false,
      });
    } catch (e: any) {
      set({ loading: false, error: e?.message || 'failed' });
    }
  },

  clear: () => set({
    serverCode: null, serverName: null, host: null, port: null,
    peers: [], loading: false, error: null,
  }),
}));
