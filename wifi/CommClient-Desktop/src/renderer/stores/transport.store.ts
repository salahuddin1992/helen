/**
 * transport.store.ts — Zustand store for transport network layer state.
 *
 * Manages:
 *   - Transport catalog and filtering
 *   - Detected transports with signal quality
 *   - Active bridges and their status
 *   - Real-time signal quality metrics
 *   - Auto-detection and failover
 */

import { create } from 'zustand';
import { api } from '../services/api.client';
import { socketManager } from '../services/socket.manager';

// ── Types ────────────────────────────────────────────────────

export interface TransportDefinition {
  transport_id: string;
  name: string;
  category: string;
  medium: 'wired' | 'wireless' | 'optical' | 'usb';
  description: string;
  typical_bandwidth: string;
  typical_range: string | null;
  typical_latency: string;
  detection_method: string;
  is_common: boolean;
  requires_hardware: boolean;
}

export interface DetectedTransport {
  transport_id: string;
  name: string;
  adapter_family: string;
  interface_name: string;
  ip_address: string | null;
  mac_address: string | null;
  speed: string;
  mtu: number;
  is_up: boolean;
  is_loopback: boolean;
  signal_strength: number; // 0-100
  signal_quality: 'excellent' | 'good' | 'fair' | 'poor';
}

export interface SignalQuality {
  transport_id: string;
  interface_name: string;
  signal_strength: number; // 0-100
  snr: number | null; // Signal-to-noise ratio (dB)
  bandwidth: number; // Mbps
  latency: number; // ms
  jitter: number; // ms
  packet_loss: number; // %
  quality_score: number; // 0-100
  quality_label: 'excellent' | 'good' | 'fair' | 'poor';
  measured_at: string; // ISO 8601
}

export interface BridgeStatus {
  bridge_id: string;
  name: string;
  transport_id: string;
  transport_name: string;
  bind_address: string;
  bind_port: number;
  status: 'active' | 'idle' | 'error';
  is_encrypted: boolean;
  connected_peers: string[];
  peer_count: number;
  bytes_sent: number;
  bytes_received: number;
  uptime_seconds: number;
  avg_latency_ms: number | null;
  created_at: string;
}

export interface BridgeCreateRequest {
  transport_id: string;
  name: string;
  bind_port?: number;
  protocol?: 'tcp' | 'udp' | 'both';
  encryption?: boolean;
  max_connections?: number;
}

export interface CapabilityCheck {
  transport_id: string;
  transport_name: string;
  supports_voice: boolean;
  supports_video: boolean;
  supports_screen_share: boolean;
  supports_file_transfer: boolean;
  max_participants: number;
  recommended_codec: string | null;
  recommended_video_quality: 'low' | 'medium' | 'high';
  notes: string | null;
}

// ── Store ────────────────────────────────────────────────────

interface TransportState {
  // Catalog
  categories: Array<{ name: string; count: number }>;
  transports: TransportDefinition[];
  transportSearch: string;

  // Detection
  detectedTransports: DetectedTransport[];
  isScanning: boolean;
  lastScanTime: string | null;

  // Bridges
  activeBridges: BridgeStatus[];
  selectedBridge: string | null;
  isCreatingBridge: boolean;

  // Signal quality
  signalQualities: Record<string, SignalQuality>;
  signalSubscriptions: Set<string>; // Active subscriptions to signal updates

  // Error handling
  error: string | null;

  // ── Actions ──

  // Catalog operations
  loadCategories: () => Promise<void>;
  loadAllTransports: () => Promise<void>;
  searchTransports: (query: string, filters?: Record<string, any>) => Promise<void>;

  // Detection operations
  runDetection: () => Promise<void>;
  getDetectedTransports: () => Promise<void>;

  // Bridge operations
  createBridge: (config: BridgeCreateRequest) => Promise<void>;
  destroyBridge: (bridgeId: string) => Promise<void>;
  autoBridge: (name: string) => Promise<void>;
  selectBridge: (bridgeId: string) => void;
  listBridges: () => Promise<void>;

  // Signal operations
  measureSignal: (transportId: string) => Promise<void>;
  subscribeSignal: (transportId: string, intervalSeconds?: number) => Promise<void>;
  unsubscribeSignal: (transportId: string) => void;
  checkCapabilities: (transportId: string) => Promise<CapabilityCheck>;

  // UI state
  setError: (error: string | null) => void;
  clear: () => void;
}

export const useTransportStore = create<TransportState>((set, get) => {
  // Register socket.IO listeners on store creation
  if (typeof window !== 'undefined') {
    socketManager.on('transport:scan_result', (data: any) => {
      set((state) => ({
        ...state,
        detectedTransports: data.detected_transports,
        lastScanTime: data.scan_timestamp,
        isScanning: false,
      }));
    });

    socketManager.on('transport:bridge_created', (bridge: BridgeStatus) => {
      set((state) => ({
        activeBridges: [...state.activeBridges, bridge],
      }));
    });

    socketManager.on('transport:bridge_destroyed', (data: { bridge_id: string }) => {
      set((state) => ({
        activeBridges: state.activeBridges.filter((b) => b.bridge_id !== data.bridge_id),
        selectedBridge: state.selectedBridge === data.bridge_id ? null : state.selectedBridge,
      }));
    });

    socketManager.on('transport:signal_update', (signal: SignalQuality) => {
      set((state) => ({
        signalQualities: {
          ...state.signalQualities,
          [signal.transport_id]: signal,
        },
      }));
    });

    socketManager.on('transport:peer_joined', (data: any) => {
      set((state) => ({
        activeBridges: state.activeBridges.map((b) =>
          b.bridge_id === data.bridge_id
            ? {
                ...b,
                connected_peers: [...b.connected_peers, data.peer_id],
                peer_count: b.peer_count + 1,
              }
            : b
        ),
      }));
    });

    socketManager.on('transport:peer_left', (data: any) => {
      set((state) => ({
        activeBridges: state.activeBridges.map((b) =>
          b.bridge_id === data.bridge_id
            ? {
                ...b,
                connected_peers: b.connected_peers.filter((p) => p !== data.peer_id),
                peer_count: Math.max(0, b.peer_count - 1),
              }
            : b
        ),
      }));
    });

    socketManager.on('transport:auto_failover', (data: any) => {
      console.warn('Transport failover detected:', data);
    });
  }

  return {
    categories: [],
    transports: [],
    transportSearch: '',
    detectedTransports: [],
    isScanning: false,
    lastScanTime: null,
    activeBridges: [],
    selectedBridge: null,
    isCreatingBridge: false,
    signalQualities: {},
    signalSubscriptions: new Set(),
    error: null,

    // ── Catalog ────

    async loadCategories() {
      try {
        const response = await (api as any).get('/transports/categories');
        const cats = Object.entries(response as Record<string, number>).map(([name, count]) => ({
          name,
          count,
        }));
        set({ categories: cats });
      } catch (err) {
        set({ error: `Failed to load categories: ${err}` });
      }
    },

    async loadAllTransports() {
      try {
        set({ isScanning: true });
        const response = await (api as any).get('/transports', {
          params: { per_page: 100 },
        });
        set({
          transports: response.transports,
          isScanning: false,
        });
      } catch (err) {
        set({ error: `Failed to load transports: ${err}`, isScanning: false });
      }
    },

    async searchTransports(query: string, filters?: Record<string, any>) {
      try {
        set({ transportSearch: query });
        const response = await (api as any).get('/transports', {
          params: {
            search: query,
            ...filters,
          },
        });
        set({ transports: response.transports });
      } catch (err) {
        set({ error: `Search failed: ${err}` });
      }
    },

    // ── Detection ────

    async runDetection() {
      try {
        set({ isScanning: true, error: null });
        socketManager.emit('transport:scan_request', {
          adapter_family: null,
        });
        // Response comes via socket:transport:scan_result
      } catch (err) {
        set({ error: `Detection failed: ${err}`, isScanning: false });
      }
    },

    async getDetectedTransports() {
      try {
        const response = await (api as any).get('/transports/detected');
        set({
          detectedTransports: response.detected_transports,
          lastScanTime: response.scan_timestamp,
        });
      } catch (err) {
        set({ error: `Failed to fetch detected transports: ${err}` });
      }
    },

    // ── Bridges ────

    async createBridge(config: BridgeCreateRequest) {
      try {
        set({ isCreatingBridge: true, error: null });
        const response = await (api as any).post('/transports/bridges', config);
        set({
          activeBridges: [...get().activeBridges, response],
          isCreatingBridge: false,
        });
      } catch (err) {
        set({ error: `Bridge creation failed: ${err}`, isCreatingBridge: false });
      }
    },

    async destroyBridge(bridgeId: string) {
      try {
        set({ error: null });
        await (api as any).delete(`/transports/bridges/${bridgeId}`);
        set({
          activeBridges: get().activeBridges.filter((b) => b.bridge_id !== bridgeId),
          selectedBridge: get().selectedBridge === bridgeId ? null : get().selectedBridge,
        });
      } catch (err) {
        set({ error: `Bridge destruction failed: ${err}` });
      }
    },

    async autoBridge(name: string) {
      try {
        set({ isCreatingBridge: true, error: null });
        const response = await (api as any).post('/transports/bridges/auto', null, {
          params: { name },
        });
        set({
          activeBridges: [...get().activeBridges, response],
          isCreatingBridge: false,
        });
      } catch (err) {
        set({ error: `Auto-bridge failed: ${err}`, isCreatingBridge: false });
      }
    },

    selectBridge(bridgeId: string) {
      set({ selectedBridge: bridgeId });
    },

    async listBridges() {
      try {
        const response = await (api as any).get('/transports/bridges');
        set({ activeBridges: response.bridges });
      } catch (err) {
        set({ error: `Failed to list bridges: ${err}` });
      }
    },

    // ── Signal Quality ────

    async measureSignal(transportId: string) {
      try {
        set({ error: null });
        const response = await (api as any).get(`/transports/${transportId}/signal`);
        set({
          signalQualities: {
            ...get().signalQualities,
            [transportId]: response,
          },
        });
      } catch (err) {
        set({ error: `Signal measurement failed: ${err}` });
      }
    },

    async subscribeSignal(transportId: string, intervalSeconds = 5) {
      const current = get().signalSubscriptions;
      if (!current.has(transportId)) {
        current.add(transportId);
        set({ signalSubscriptions: new Set(current) });

        socketManager.emit('transport:signal_subscribe', {
          transport_id: transportId,
          interval_seconds: intervalSeconds,
        });
      }
    },

    unsubscribeSignal(transportId: string) {
      const current = get().signalSubscriptions;
      current.delete(transportId);
      set({ signalSubscriptions: new Set(current) });
    },

    async checkCapabilities(transportId: string): Promise<CapabilityCheck> {
      try {
        const response = await (api as any).get(`/transports/capabilities/${transportId}`);
        return response;
      } catch (err) {
        set({ error: `Capability check failed: ${err}` });
        throw err;
      }
    },

    // ── State Management ────

    setError(error: string | null) {
      set({ error });
    },

    clear() {
      set({
        categories: [],
        transports: [],
        transportSearch: '',
        detectedTransports: [],
        isScanning: false,
        lastScanTime: null,
        activeBridges: [],
        selectedBridge: null,
        isCreatingBridge: false,
        signalQualities: {},
        signalSubscriptions: new Set(),
        error: null,
      });
    },
  };
});
