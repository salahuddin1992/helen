/**
 * FileDrop Store — Zustand store for file drop state management
 */
import { create } from 'zustand';
import { FileDropManager, type FileOffer, type ActiveTransfer } from '../services/filedrop';

interface FilePath {
  id: string;
  name: string;
  type: 'file' | 'folder';
  parentId: string | null;
  size?: number;
  modifiedAt: string;
}

interface FileDropState {
  manager: FileDropManager | null;
  activeTransfers: ActiveTransfer[];
  pendingOffers: FileOffer[];
  sharedFolders: Map<string, FilePath[]>;
  currentFolderId: string | null;
  transferHistory: Array<{
    id: string;
    fileName: string;
    size: number;
    direction: 'upload' | 'download';
    timestamp: number;
    status: 'completed' | 'failed';
  }>;

  // Actions
  initialize: (uploadUrl: string) => void;
  sendFile: (file: File, recipientIds: string[], channelId: string) => Promise<void>;
  acceptOffer: (offerId: string, downloadPath?: string) => Promise<void>;
  rejectOffer: (offerId: string) => void;
  cancelTransfer: (transferId: string) => void;
  setSharedFolder: (folderId: string, files: FilePath[]) => void;
  navigateFolder: (folderId: string | null) => void;
  updateTransfers: () => void;
  addToHistory: (transfer: FileDropState['transferHistory'][0]) => void;
}

let fileDropManager: FileDropManager | null = null;

export const useFileDropStore = create<FileDropState>((set, get) => ({
  manager: null,
  activeTransfers: [],
  pendingOffers: [],
  sharedFolders: new Map(),
  currentFolderId: null,
  transferHistory: [],

  initialize: (uploadUrl) => {
    if (!fileDropManager) {
      fileDropManager = new FileDropManager(uploadUrl);
    }
    set({ manager: fileDropManager });
  },

  sendFile: async (file, recipientIds, channelId) => {
    const manager = get().manager;
    if (!manager) return;

    try {
      await manager.sendFile(file, recipientIds, channelId);
      get().updateTransfers();
    } catch (error) {
      console.error('Failed to send file:', error);
    }
  },

  acceptOffer: async (offerId, downloadPath) => {
    const manager = get().manager;
    if (!manager) return;

    try {
      await manager.acceptOffer(offerId, downloadPath);
      get().updateTransfers();
    } catch (error) {
      console.error('Failed to accept offer:', error);
    }
  },

  rejectOffer: (offerId) => {
    const manager = get().manager;
    if (!manager) return;

    manager.rejectOffer(offerId);
    set((state) => ({
      pendingOffers: state.pendingOffers.filter((o) => o.id !== offerId),
    }));
  },

  cancelTransfer: (transferId) => {
    const manager = get().manager;
    if (!manager) return;

    manager.cancelTransfer(transferId);
    get().updateTransfers();
  },

  setSharedFolder: (folderId, files) => {
    set((state) => {
      const newFolders = new Map(state.sharedFolders);
      newFolders.set(folderId, files);
      return { sharedFolders: newFolders };
    });
  },

  navigateFolder: (folderId) => {
    set({ currentFolderId: folderId });
  },

  updateTransfers: () => {
    const manager = get().manager;
    if (!manager) return;

    const transfers = manager.getActiveTransfers();
    const offers = manager.getPendingOffers();

    set({
      activeTransfers: transfers,
      pendingOffers: offers,
    });
  },

  addToHistory: (transfer) => {
    set((state) => ({
      transferHistory: [transfer, ...state.transferHistory].slice(0, 50), // Keep last 50
    }));
  },
}));
