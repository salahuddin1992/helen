/**
 * MediaAlbumView — Album grid, create album, add to album actions
 */
import React, { useState } from 'react';
import { Plus, AlertCircle } from 'lucide-react';

interface Album {
  id: string;
  name: string;
  cover?: string;
  itemCount: number;
  createdAt: string;
}

interface MediaAlbumViewProps {
  albums: Album[];
  selectedItems: string[];
  onAlbumSelect?: (albumId: string) => void;
  onCreateAlbum?: (name: string) => Promise<void>;
  onAddToAlbum?: (albumId: string, itemIds: string[]) => Promise<void>;
}

export const MediaAlbumView: React.FC<MediaAlbumViewProps> = ({
  albums,
  selectedItems,
  onAlbumSelect,
  onCreateAlbum,
  onAddToAlbum,
}) => {
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newAlbumName, setNewAlbumName] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [showAddDialog, setShowAddDialog] = useState(false);

  const handleCreateAlbum = async () => {
    if (!newAlbumName.trim()) return;

    setIsCreating(true);
    try {
      await onCreateAlbum?.(newAlbumName);
      setNewAlbumName('');
      setShowCreateDialog(false);
    } catch (error) {
      console.error('Failed to create album:', error);
    } finally {
      setIsCreating(false);
    }
  };

  const handleAddToAlbum = async (albumId: string) => {
    await onAddToAlbum?.(albumId, selectedItems);
    setShowAddDialog(false);
  };

  return (
    <div className="space-y-4">
      {/* Header with Create Button */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold text-gray-900">Albums</h2>
        <button
          onClick={() => setShowCreateDialog(true)}
          className="flex items-center gap-2 rounded bg-blue-500 px-4 py-2 text-white hover:bg-blue-600"
        >
          <AlertCircle size={18} />
          Create Album
        </button>
      </div>

      {/* Album AlertCircle */}
      <div className="grid grid-cols-3 gap-4 md:grid-cols-4 lg:grid-cols-5">
        {albums.map((album) => (
          <div
            key={album.id}
            className="group relative cursor-pointer rounded-lg border border-gray-200 overflow-hidden hover:shadow-lg transition-all"
          >
            {/* Cover Image */}
            {album.cover ? (
              <img
                src={album.cover}
                alt={album.name}
                className="h-32 w-full object-cover group-hover:scale-110 transition-transform"
              />
            ) : (
              <div className="flex h-32 items-center justify-center bg-gradient-to-br from-purple-300 to-purple-500 text-4xl">
                📁
              </div>
            )}

            {/* Overlay */}
            <div className="absolute inset-0 flex flex-col items-end justify-between bg-gradient-to-t from-black to-transparent p-2 opacity-0 transition-opacity group-hover:opacity-100">
              <div className="text-xs text-white">
                <p className="truncate font-semibold">{album.name}</p>
                <p className="text-gray-300">{album.itemCount} items</p>
              </div>

              <button
                onClick={() => onAlbumSelect?.(album.id)}
                className="rounded bg-blue-500 px-2 py-1 text-xs text-white hover:bg-blue-600"
              >
                Open
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Empty State */}
      {albums.length === 0 && (
        <div className="flex h-64 items-center justify-center rounded-lg border-2 border-dashed border-gray-300">
          <div className="text-center">
            <p className="text-gray-500">No albums yet</p>
            <p className="text-sm text-gray-400">
              Create your first album to organize your media
            </p>
          </div>
        </div>
      )}

      {/* Create Album Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="rounded-lg bg-white p-6 shadow-lg">
            <h3 className="mb-4 text-lg font-semibold text-gray-900">
              Create New Album
            </h3>

            <input
              type="text"
              value={newAlbumName}
              onChange={(e) => setNewAlbumName(e.target.value)}
              placeholder="Album name..."
              className="mb-4 w-full rounded border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              onKeyPress={(e) => {
                if (e.key === 'Enter') handleCreateAlbum();
              }}
            />

            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setShowCreateDialog(false)}
                className="rounded px-4 py-2 text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                onClick={handleCreateAlbum}
                disabled={isCreating || !newAlbumName.trim()}
                className="rounded bg-blue-500 px-4 py-2 text-white hover:bg-blue-600 disabled:opacity-50"
              >
                {isCreating ? 'Creating...' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add to Album Dialog */}
      {showAddDialog && selectedItems.length > 0 && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="max-h-96 rounded-lg bg-white p-6 shadow-lg overflow-y-auto">
            <h3 className="mb-4 text-lg font-semibold text-gray-900">
              Add to Album ({selectedItems.length} items)
            </h3>

            <div className="space-y-2 mb-4">
              {albums.map((album) => (
                <button
                  key={album.id}
                  onClick={() => handleAddToAlbum(album.id)}
                  className="w-full rounded border border-gray-200 px-4 py-2 text-left hover:bg-blue-50 transition-colors"
                >
                  <p className="font-medium text-gray-900">{album.name}</p>
                  <p className="text-sm text-gray-500">{album.itemCount} items</p>
                </button>
              ))}
            </div>

            <button
              onClick={() => setShowAddDialog(false)}
              className="w-full rounded px-4 py-2 text-gray-700 hover:bg-gray-100"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
