/**
 * Virtual media sources — streams that are NOT produced by
 * navigator.mediaDevices but should appear as selectable devices (paired
 * phones, ingest feeds, etc.). Shared singleton so both the Settings UI and
 * the active CallEngine's MediaDeviceManager see the same list.
 *
 * Device IDs are prefixed with "virtual:" — see VIRTUAL_DEVICE_PREFIX.
 */
import { create } from 'zustand';

export interface VirtualSource {
  deviceId: string;                        // starts with "virtual:"
  label: string;
  kind: 'audioinput' | 'videoinput';
  stream: MediaStream;                     // live tracks
  /** Transport hint for UI badges — "usb_tether" for an iPhone on USB, "wifi"
   *  otherwise. Absent for non-phone virtual sources. */
  transport?: 'usb_tether' | 'wifi';
}

interface VirtualSourcesState {
  sources: Record<string, VirtualSource>;
  add: (src: VirtualSource) => void;
  remove: (deviceId: string) => void;
  get: (deviceId: string) => VirtualSource | undefined;
  listByKind: (kind: VirtualSource['kind']) => VirtualSource[];
  clear: () => void;
}

export const useVirtualSourcesStore = create<VirtualSourcesState>((set, get) => ({
  sources: {},
  add: (src) => {
    set((s) => ({ sources: { ...s.sources, [src.deviceId]: src } }));
  },
  remove: (deviceId) => {
    set((s) => {
      const existing = s.sources[deviceId];
      if (existing) {
        try { existing.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      }
      const next = { ...s.sources };
      delete next[deviceId];
      return { sources: next };
    });
  },
  get: (deviceId) => get().sources[deviceId],
  listByKind: (kind) => Object.values(get().sources).filter((s) => s.kind === kind),
  clear: () => {
    const cur = get().sources;
    for (const src of Object.values(cur)) {
      try { src.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    }
    set({ sources: {} });
  },
}));

// Non-React accessors for services that live outside components.
export function getVirtualSource(deviceId: string): VirtualSource | undefined {
  return useVirtualSourcesStore.getState().sources[deviceId];
}
export function listVirtualSources(kind?: VirtualSource['kind']): VirtualSource[] {
  const all = Object.values(useVirtualSourcesStore.getState().sources);
  return kind ? all.filter((s) => s.kind === kind) : all;
}
