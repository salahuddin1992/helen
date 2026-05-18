/**
 * SharedFolderView — File browser with folder tree, breadcrumb, file list
 * Upload button, create folder, context menu (download, delete, move)
 */
import React, { useState } from 'react';
import {
  ChevronRight,
  Download,
  Trash2,
  AlertCircle,
  Upload,
  MoreVertical,
} from 'lucide-react';

interface FileItem {
  id: string;
  name: string;
  type: 'file' | 'folder';
  size?: number;
  modifiedAt: string;
  parentId: string | null;
}

interface SharedFolderViewProps {
  files: FileItem[];
  currentFolderId: string | null;
  onNavigate?: (folderId: string | null) => void;
  onCreateFolder?: (name: string, parentId: string | null) => Promise<void>;
  onUpload?: (files: File[], parentId: string | null) => Promise<void>;
  onDownload?: (fileId: string) => void;
  onDelete?: (fileId: string) => Promise<void>;
}

export const SharedFolderView: React.FC<SharedFolderViewProps> = ({
  files,
  currentFolderId,
  onNavigate,
  onCreateFolder,
  onUpload,
  onDownload,
  onDelete,
}) => {
  const [showCreateFolder, setShowCreateFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; fileId: string } | null>(null);
  const [isCreating, setIsCreating] = useState(false);

  // Get breadcrumb path
  const getBreadcrumb = (): Array<{ id: string | null; name: string }> => {
    const path: Array<{ id: string | null; name: string }> = [{ id: null, name: 'Root' }];

    let current = currentFolderId;
    while (current) {
      const folder = files.find((f) => f.id === current && f.type === 'folder');
      if (!folder) break;
      path.unshift({ id: folder.parentId, name: folder.name });
      current = folder.parentId;
    }

    return path;
  };

  // Get files in current folder
  const currentFiles = files.filter((f) => f.parentId === currentFolderId);

  const handleCreateFolder = async () => {
    if (!newFolderName.trim()) return;

    setIsCreating(true);
    try {
      await onCreateFolder?.(newFolderName, currentFolderId);
      setNewFolderName('');
      setShowCreateFolder(false);
    } catch (error) {
      console.error('Failed to create folder:', error);
    } finally {
      setIsCreating(false);
    }
  };

  const handleContextMenu = (e: React.MouseEvent, fileId: string) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, fileId });
  };

  const formatFileSize = (bytes: number | undefined): string => {
    if (!bytes) return '--';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(2) + ' ' + sizes[i];
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <div className="flex flex-col h-full bg-gray-50">
      {/* Toolbar */}
      <div className="flex gap-2 border-b border-gray-200 bg-white p-3">
        <button
          onClick={() => setShowCreateFolder(true)}
          className="flex items-center gap-2 rounded bg-blue-500 px-3 py-2 text-sm text-white hover:bg-blue-600"
        >
          <AlertCircle size={18} />
          New Folder
        </button>

        <label className="flex cursor-pointer items-center gap-2 rounded bg-green-500 px-3 py-2 text-sm text-white hover:bg-green-600">
          <Upload size={18} />
          Upload
          <input
            type="file"
            multiple
            onChange={(e) => {
              const files = Array.from(e.currentTarget.files || []);
              onUpload?.(files, currentFolderId);
            }}
            className="hidden"
          />
        </label>
      </div>

      {/* Breadcrumb */}
      <div className="flex items-center gap-1 border-b border-gray-200 bg-white px-4 py-2 text-sm">
        {getBreadcrumb().map((item, index) => (
          <React.Fragment key={item.id}>
            {index > 0 && <ChevronRight size={16} className="text-gray-400" />}
            <button
              onClick={() => onNavigate?.(item.id)}
              className="text-blue-600 hover:underline"
            >
              {item.name}
            </button>
          </React.Fragment>
        ))}
      </div>

      {/* File List */}
      <div className="flex-1 overflow-auto">
        {currentFiles.length === 0 ? (
          <div className="flex h-64 items-center justify-center">
            <p className="text-gray-500">This folder is empty</p>
          </div>
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-100">
                <th className="px-4 py-2 text-left text-sm font-semibold text-gray-900">
                  Name
                </th>
                <th className="px-4 py-2 text-left text-sm font-semibold text-gray-900">
                  Size
                </th>
                <th className="px-4 py-2 text-left text-sm font-semibold text-gray-900">
                  Modified
                </th>
                <th className="px-4 py-2 text-right text-sm font-semibold text-gray-900">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {currentFiles.map((file) => (
                <tr
                  key={file.id}
                  className="border-b border-gray-200 hover:bg-gray-100 transition-colors"
                  onContextMenu={(e) => handleContextMenu(e, file.id)}
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {file.type === 'folder' ? (
                        <>
                          <span className="text-lg">📁</span>
                          <button
                            onClick={() => onNavigate?.(file.id)}
                            className="text-blue-600 hover:underline"
                          >
                            {file.name}
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="text-lg">📄</span>
                          <span className="text-gray-900">{file.name}</span>
                        </>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600">
                    {formatFileSize(file.size)}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600">
                    {formatDate(file.modifiedAt)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setContextMenu({ x: 0, y: 0, fileId: file.id })}
                      className="rounded p-1 text-gray-500 hover:bg-gray-200"
                    >
                      <MoreVertical size={18} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Create Folder Dialog */}
      {showCreateFolder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="rounded-lg bg-white p-6 shadow-lg">
            <h3 className="mb-4 text-lg font-semibold">Create Folder</h3>
            <input
              autoFocus
              type="text"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              placeholder="Folder name..."
              className="mb-4 w-full rounded border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
              onKeyPress={(e) => {
                if (e.key === 'Enter') handleCreateFolder();
              }}
            />
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setShowCreateFolder(false)}
                className="rounded px-4 py-2 text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                onClick={handleCreateFolder}
                disabled={isCreating || !newFolderName.trim()}
                className="rounded bg-blue-500 px-4 py-2 text-white hover:bg-blue-600 disabled:opacity-50"
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Context Menu */}
      {contextMenu && (
        <div
          className="fixed z-50 rounded-lg border border-gray-200 bg-white shadow-lg"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            onClick={() => {
              onDownload?.(contextMenu.fileId);
              setContextMenu(null);
            }}
            className="flex items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 w-full text-left"
          >
            <Download size={16} />
            Download
          </button>
          <button
            onClick={() => {
              onDelete?.(contextMenu.fileId);
              setContextMenu(null);
            }}
            className="flex items-center gap-2 px-4 py-2 text-sm text-red-700 hover:bg-red-50 w-full text-left"
          >
            <Trash2 size={16} />
            Delete
          </button>
        </div>
      )}
    </div>
  );
};
