/**
 * MediaViewer — Full-screen media viewer with zoom/pan, seek, next/previous, download
 */
import React, { useRef, useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  Download,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';

interface MediaItem {
  id: string;
  name: string;
  type: 'image' | 'video' | 'audio' | 'document';
  url: string;
  size: number;
  uploadedAt: string;
  uploadedBy: string;
}

interface MediaViewerProps {
  item: MediaItem;
  allItems: MediaItem[];
  onClose?: () => void;
  onNavigate?: (item: MediaItem) => void;
}

export const MediaViewer: React.FC<MediaViewerProps> = ({
  item,
  allItems,
  onClose,
  onNavigate,
}) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  const currentIndex = allItems.findIndex((i) => i.id === item.id);
  const hasPrevious = currentIndex > 0;
  const hasNext = currentIndex < allItems.length - 1;

  const handlePrevious = () => {
    if (hasPrevious) {
      onNavigate?.(allItems[currentIndex - 1]);
    }
  };

  const handleNext = () => {
    if (hasNext) {
      onNavigate?.(allItems[currentIndex + 1]);
    }
  };

  const handleZoomIn = () => setZoom((z) => Math.min(z + 0.1, 3));
  const handleZoomOut = () => setZoom((z) => Math.max(z - 0.1, 0.5));

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    setDragStart({ x: e.clientX, y: e.clientY });
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isDragging) return;

    const deltaX = e.clientX - dragStart.x;
    const deltaY = e.clientY - dragStart.y;

    setPan((p) => ({ x: p.x + deltaX, y: p.y + deltaY }));
    setDragStart({ x: e.clientX, y: e.clientY });
  };

  const handleMouseUp = () => {
    setIsDragging(false);
  };

  const handleDownload = () => {
    const link = document.createElement('a');
    link.href = item.url;
    link.download = item.name;
    link.click();
  };

  const formatFileSize = (bytes: number) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black">
      {/* Header */}
      <div className="flex items-center justify-between bg-gray-900 px-6 py-4 text-white">
        <div>
          <h3 className="font-semibold">{item.name}</h3>
          <p className="text-sm text-gray-400">
            {formatFileSize(item.size)} • {formatDate(item.uploadedAt)} • {item.uploadedBy}
          </p>
        </div>

        <button
          onClick={onClose}
          className="rounded p-2 hover:bg-gray-800"
        >
          <X size={24} />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 flex items-center justify-center overflow-hidden">
        {item.type === 'image' && (
          <div
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            className="relative cursor-grab active:cursor-grabbing"
          >
            <img
              src={item.url}
              alt={item.name}
              className="max-h-full max-w-full object-contain"
              style={{
                transform: `scale(${zoom}) translate(${pan.x}px, ${pan.y}px)`,
                transition: isDragging ? 'none' : 'transform 0.2s',
              }}
            />
          </div>
        )}

        {item.type === 'video' && (
          <video
            ref={videoRef}
            src={item.url}
            controls
            className="max-h-full max-w-full"
          />
        )}

        {item.type === 'audio' && (
          <div className="flex flex-col items-center gap-4">
            <div className="text-6xl">🎵</div>
            <audio
              ref={audioRef}
              src={item.url}
              controls
              className="w-96"
            />
          </div>
        )}

        {item.type === 'document' && (
          <div className="flex flex-col items-center gap-4 text-center">
            <div className="text-6xl">📄</div>
            <p className="text-white">Document: {item.name}</p>
            <button
              onClick={handleDownload}
              className="flex items-center gap-2 rounded bg-blue-600 px-4 py-2 text-white hover:bg-blue-700"
            >
              <Download size={18} />
              Download
            </button>
          </div>
        )}
      </div>

      {/* Footer Controls */}
      <div className="flex items-center justify-between bg-gray-900 px-6 py-4 text-white">
        <div className="flex gap-2">
          {item.type === 'image' && (
            <>
              <button
                onClick={handleZoomOut}
                className="rounded p-2 hover:bg-gray-800"
              >
                <ZoomOut size={20} />
              </button>
              <span className="flex items-center px-2">{Math.round(zoom * 100)}%</span>
              <button
                onClick={handleZoomIn}
                className="rounded p-2 hover:bg-gray-800"
              >
                <ZoomIn size={20} />
              </button>
            </>
          )}
        </div>

        <div className="flex gap-2">
          <button
            onClick={handlePrevious}
            disabled={!hasPrevious}
            className="rounded p-2 hover:bg-gray-800 disabled:opacity-50"
          >
            <ChevronLeft size={24} />
          </button>

          <div className="flex items-center px-4 text-sm">
            {currentIndex + 1} / {allItems.length}
          </div>

          <button
            onClick={handleNext}
            disabled={!hasNext}
            className="rounded p-2 hover:bg-gray-800 disabled:opacity-50"
          >
            <ChevronRight size={24} />
          </button>

          <button
            onClick={handleDownload}
            className="flex items-center gap-2 rounded bg-blue-600 px-4 py-2 hover:bg-blue-700"
          >
            <Download size={18} />
            Download
          </button>
        </div>
      </div>
    </div>
  );
};
