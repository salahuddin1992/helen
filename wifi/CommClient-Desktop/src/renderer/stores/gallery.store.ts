/**
 * Gallery Store — Zustand store for media gallery state
 */
import { create } from 'zustand';

interface MediaItem {
  id: string;
  name: string;
  type: 'image' | 'video' | 'audio' | 'document';
  url: string;
  thumbnail?: string;
  size: number;
  uploadedAt: string;
  uploadedBy: string;
}

interface Album {
  id: string;
  name: string;
  cover?: string;
  itemCount: number;
  createdAt: string;
}

interface GalleryState {
  mediaItems: MediaItem[];
  albums: Album[];
  selectedAlbumId: string | null;
  selectedItems: Set<string>;
  currentPage: number;
  hasMore: boolean;
  isLoading: boolean;
  viewMode: 'grid' | 'list';

  // Actions
  setMediaItems: (items: MediaItem[]) => void;
  addMediaItem: (item: MediaItem) => void;
  removeMediaItem: (itemId: string) => void;
  loadMoreItems: (channelId: string) => Promise<void>;
  setAlbums: (albums: Album[]) => void;
  createAlbum: (name: string) => Promise<Album>;
  selectAlbum: (albumId: string | null) => void;
  toggleItemSelection: (itemId: string) => void;
  clearSelection: () => void;
  setViewMode: (mode: 'grid' | 'list') => void;
}

export const useGalleryStore = create<GalleryState>((set, get) => ({
  mediaItems: [],
  albums: [],
  selectedAlbumId: null,
  selectedItems: new Set(),
  currentPage: 0,
  hasMore: true,
  isLoading: false,
  viewMode: 'grid',

  setMediaItems: (items) => {
    set({ mediaItems: items });
  },

  addMediaItem: (item) => {
    set((state) => ({
      mediaItems: [item, ...state.mediaItems],
    }));
  },

  removeMediaItem: (itemId) => {
    set((state) => ({
      mediaItems: state.mediaItems.filter((item) => item.id !== itemId),
    }));
  },

  loadMoreItems: async (channelId) => {
    const state = get();
    if (state.isLoading || !state.hasMore) return;

    set({ isLoading: true });
    try {
      // In a real app, this would fetch from the API
      // For now, simulate loading
      await new Promise((resolve) => setTimeout(resolve, 500));

      set((s) => ({
        currentPage: s.currentPage + 1,
        hasMore: false, // Would check response for more items
      }));
    } catch (error) {
      console.error('Failed to load more items:', error);
    } finally {
      set({ isLoading: false });
    }
  },

  setAlbums: (albums) => {
    set({ albums });
  },

  createAlbum: async (name) => {
    // In a real app, this would call the API
    const newAlbum: Album = {
      id: `album-${Date.now()}`,
      name,
      itemCount: 0,
      createdAt: new Date().toISOString(),
    };

    set((state) => ({
      albums: [...state.albums, newAlbum],
    }));

    return newAlbum;
  },

  selectAlbum: (albumId) => {
    set({
      selectedAlbumId: albumId,
      selectedItems: new Set(),
    });
  },

  toggleItemSelection: (itemId) => {
    set((state) => {
      const newSelected = new Set(state.selectedItems);
      if (newSelected.has(itemId)) {
        newSelected.delete(itemId);
      } else {
        newSelected.add(itemId);
      }
      return { selectedItems: newSelected };
    });
  },

  clearSelection: () => {
    set({ selectedItems: new Set() });
  },

  setViewMode: (mode) => {
    set({ viewMode: mode });
  },
}));
