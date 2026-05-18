import React, { useState } from 'react';
import { X, Download, ZoomIn, ZoomOut } from 'lucide-react';
import { FileText, Image, Music, Video, Archive, File } from 'lucide-react';

interface FilePreviewFile {
  id: string;
  filename: string;
  mime_type: string;
  size: number;
  url: string;
  thumbnail_url?: string;
}

interface FilePreviewProps {
  file: FilePreviewFile;
}

interface Lightbox {
  isOpen: boolean;
  zoom: number;
}

export const FilePreview: React.FC<FilePreviewProps> = ({ file }) => {
  const [lightbox, setLightbox] = useState<Lightbox>({ isOpen: false, zoom: 100 });

  const isImage = file.mime_type.startsWith('image/');
  const isVideo = file.mime_type.startsWith('video/');
  const isAudio = file.mime_type.startsWith('audio/');
  const isArchive = ['application/zip', 'application/x-rar-compressed', 'application/x-7z-compressed', 'application/gzip'].includes(
    file.mime_type
  );
  const isPdf = file.mime_type === 'application/pdf';

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
  };

  const getFileIcon = () => {
    if (isImage) return <Image size={32} className="text-blue-400" />;
    if (isVideo) return <Video size={32} className="text-purple-400" />;
    if (isAudio) return <Music size={32} className="text-orange-400" />;
    if (isArchive) return <Archive size={32} className="text-yellow-400" />;
    if (isPdf) return <FileText size={32} className="text-red-400" />;
    return <File size={32} className="text-gray-400" />;
  };

  const handleDownload = () => {
    const link = document.createElement('a');
    link.href = file.url;
    link.download = file.filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleZoom = (delta: number) => {
    setLightbox((prev) => ({
      ...prev,
      zoom: Math.max(50, Math.min(300, prev.zoom + delta * 10)),
    }));
  };

  return (
    <>
      {/* File Preview Card */}
      <div className="bg-surface-800 border border-surface-700 rounded-lg p-4 max-w-xs hover:border-surface-600 transition-colors">
        {isImage ? (
          <div className="mb-3">
            <img
              src={file.thumbnail_url || file.url}
              alt={file.filename}
              onClick={() => setLightbox({ isOpen: true, zoom: 100 })}
              className="w-full h-auto rounded max-h-48 object-cover cursor-pointer hover:opacity-80 transition-opacity"
            />
          </div>
        ) : null}

        {isVideo ? (
          <div
            onClick={() => setLightbox({ isOpen: true, zoom: 100 })}
            className="mb-3 bg-surface-700 rounded aspect-video flex items-center justify-center cursor-pointer hover:bg-surface-600 transition-colors"
          >
            {file.thumbnail_url ? (
              <img src={file.thumbnail_url} alt={file.filename} className="w-full h-full object-cover rounded" />
            ) : (
              <Video size={40} className="text-purple-400" />
            )}
          </div>
        ) : null}

        {!isImage && !isVideo && (
          <div className="mb-3 flex justify-center p-4 bg-surface-700 rounded">
            {getFileIcon()}
          </div>
        )}

        {/* File Info */}
        <div className="mb-3">
          <p className="text-sm font-medium text-white truncate" title={file.filename}>
            {file.filename}
          </p>
          <p className="text-xs text-surface-400 mt-1">{formatFileSize(file.size)}</p>
        </div>

        {/* Download Button */}
        <button
          onClick={handleDownload}
          className="w-full flex items-center justify-center gap-2 bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium py-2 rounded transition-colors"
          aria-label={`Download ${file.filename}`}
        >
          <Download size={16} />
          Download
        </button>
      </div>

      {/* Lightbox Modal */}
      {lightbox.isOpen && (isImage || isVideo) && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 backdrop-blur-sm"
          onClick={() => setLightbox({ isOpen: false, zoom: 100 })}
          role="dialog"
          aria-label="File preview lightbox"
        >
          {/* Close button */}
          <button
            onClick={() => setLightbox({ isOpen: false, zoom: 100 })}
            className="absolute top-4 right-4 text-white hover:text-gray-300 transition-colors p-2 -m-2 z-10"
            aria-label="Close preview"
          >
            <X size={24} />
          </button>

          {/* Zoom controls */}
          <div className="absolute bottom-4 left-1/2 transform -translate-x-1/2 flex items-center gap-2 bg-surface-900/80 backdrop-blur rounded-lg p-3 z-10">
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleZoom(-1);
              }}
              className="text-white hover:text-primary-500 transition-colors p-1 -m-1"
              aria-label="Zoom out"
            >
              <ZoomOut size={20} />
            </button>
            <span className="text-white text-sm w-12 text-center">{lightbox.zoom}%</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleZoom(1);
              }}
              className="text-white hover:text-primary-500 transition-colors p-1 -m-1"
              aria-label="Zoom in"
            >
              <ZoomIn size={20} />
            </button>
          </div>

          {/* Content */}
          <div
            onClick={(e) => e.stopPropagation()}
            className="max-w-4xl max-h-[90vh] flex items-center justify-center overflow-auto"
          >
            {isImage ? (
              <img
                src={file.url}
                alt={file.filename}
                style={{ width: `${lightbox.zoom}%`, height: 'auto' }}
                className="max-w-full max-h-[90vh] object-contain"
              />
            ) : isVideo ? (
              <video
                src={file.url}
                controls
                autoPlay
                style={{ width: `${lightbox.zoom}%`, height: 'auto' }}
                className="max-w-full max-h-[90vh] object-contain rounded"
              />
            ) : null}
          </div>
        </div>
      )}
    </>
  );
};
