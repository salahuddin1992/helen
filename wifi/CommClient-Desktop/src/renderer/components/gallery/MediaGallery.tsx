/**
 * MediaGallery — AlertCircle view with type filters, date range, search, infinite scroll
 */
import React, { useEffect, useState } from 'react';
import { Search, Filter } from 'lucide-react';
import { useGalleryStore } from '../../stores/gallery.store';

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

type MediaType = 'All' | 'Images' | 'Videos' | 'Audio' | 'Documents';

interface MediaGalleryProps {
  channelId: string;
  onItemSelected?: (item: MediaItem) => void;
}

export const MediaGallery: React.FC<MediaGalleryProps> = ({
  channelId,
  onItemSelected,
}) => {
  const store = useGalleryStore();
  const [selectedType, setSelectedType] = useState<MediaType>('All');
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [dateRange, setDateRange] = useState<{ from: string; to: string }>({
    from: '',
    to: '',
  });

  const filterMediaItems = (items: MediaItem[]): MediaItem[] => {
    let filtered = items;

    // Filter by type
    if (selectedType !== 'All') {
      const typeMap: Record<MediaType, string> = {
        All: '',
        Images: 'image',
        Videos: 'video',
        Audio: 'audio',
        Documents: 'document',
      };
      filtered = filtered.filter((item) => item.type === typeMap[selectedType]);
    }

    // Filter by search
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter((item) =>
        item.name.toLowerCase().includes(query)
      );
    }

    // Filter by date range
    if (dateRange.from || dateRange.to) {
      filtered = filtered.filter((item) => {
        const date = new Date(item.uploadedAt);
        if (dateRange.from && date < new Date(dateRange.from)) return false;
        if (dateRange.to && date > new Date(dateRange.to)) return false;
        return true;
      });
    }

    return filtered;
  };

  const filteredItems = filterMediaItems(store.mediaItems);

  const handleLoadMore = async () => {
    setIsLoading(true);
    await store.loadMoreItems(channelId);
    setIsLoading(false);
  };

  const getMediaIcon = (type: string) => {
    const iconMap: Record<string, string> = {
      image: '🖼️',
      video: '🎥',
      audio: '🔊',
      document: '📄',
    };
    return iconMap[type] || '📎';
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Search and Filters */}
      <div className="space-y-3 rounded-lg bg-gray-100 p-4">
        <div className="flex gap-2">
          <div className="flex-1 relative">
            <Search className="absolute left-2 top-2.5 text-gray-500" size={18} />
            <input
              type="text"
              placeholder="Search media..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full rounded border border-gray-300 bg-white py-2 pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <button className="rounded bg-gray-300 p-2 hover:bg-gray-400">
            <Filter size={18} />
          </button>
        </div>

        {/* AlertCircle Filter */}
        <div className="flex gap-2">
          {(['All', 'Images', 'Videos', 'Audio', 'Documents'] as MediaType[]).map(
            (type) => (
              <button
                key={type}
                onClick={() => setSelectedType(type)}
                className={`rounded px-3 py-1 text-sm font-medium transition-all ${
                  selectedType === type
                    ? 'bg-blue-500 text-white'
                    : 'bg-white text-gray-700 hover:bg-gray-200'
                }`}
              >
                {type}
              </button>
            )
          )}
        </div>

        {/* Date Range */}
        <div className="flex gap-2">
          <input
            type="date"
            value={dateRange.from}
            onChange={(e) =>
              setDateRange({ ...dateRange, from: e.target.value })
            }
            className="rounded border border-gray-300 bg-white px-3 py-2 text-sm"
          />
          <span className="py-2 text-gray-500">to</span>
          <input
            type="date"
            value={dateRange.to}
            onChange={(e) =>
              setDateRange({ ...dateRange, to: e.target.value })
            }
            className="rounded border border-gray-300 bg-white px-3 py-2 text-sm"
          />
        </div>
      </div>

      {/* Media AlertCircle */}
      <div className="grid grid-cols-3 gap-4 md:grid-cols-4 lg:grid-cols-5">
        {filteredItems.map((item) => (
          <div
            key={item.id}
            onClick={() => onItemSelected?.(item)}
            className="group relative overflow-hidden rounded-lg border border-gray-200 bg-gray-100 transition-all hover:shadow-lg cursor-pointer"
          >
            {/* Thumbnail */}
            {item.thumbnail ? (
              <img
                src={item.thumbnail}
                alt={item.name}
                className="h-32 w-full object-cover group-hover:scale-110 transition-transform"
              />
            ) : (
              <div className="flex h-32 items-center justify-center bg-gradient-to-br from-gray-300 to-gray-400 text-4xl">
                {getMediaIcon(item.type)}
              </div>
            )}

            {/* Overlay */}
            <div className="absolute inset-0 flex items-end bg-gradient-to-t from-black to-transparent p-2 opacity-0 transition-opacity group-hover:opacity-100">
              <div className="text-xs text-white">
                <p className="truncate font-semibold">{item.name}</p>
                <p className="text-gray-300">{(item.size / 1024 / 1024).toFixed(2)} MB</p>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Load More */}
      {store.hasMore && (
        <div className="flex justify-center">
          <button
            onClick={handleLoadMore}
            disabled={isLoading}
            className="rounded bg-blue-500 px-6 py-2 text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {isLoading ? 'Loading...' : 'Load More'}
          </button>
        </div>
      )}

      {/* Empty State */}
      {filteredItems.length === 0 && (
        <div className="flex h-64 items-center justify-center rounded-lg border-2 border-dashed border-gray-300">
          <div className="text-center">
            <p className="text-gray-500">No media files found</p>
            <p className="text-sm text-gray-400">
              {searchQuery || selectedType !== 'All' ? 'Try adjusting your filters' : 'Share files to get started'}
            </p>
          </div>
        </div>
      )}
    </div>
  );
};
