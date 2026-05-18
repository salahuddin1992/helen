/**
 * FileDropZone — Drag-and-drop overlay with drop target animation
 * Shows file type icons, size validation
 */
import React, { useState } from 'react';
import { Upload, AlertCircle } from 'lucide-react';

interface FileDropZoneProps {
  onFilesDropped?: (files: File[]) => void;
  maxFileSize?: number; // in bytes
  acceptedTypes?: string[]; // MIME types
  isActive?: boolean;
}

const FILE_TYPE_ICONS: Record<string, string> = {
  'image/': '🖼️',
  'video/': '🎥',
  'audio/': '🔊',
  'application/pdf': '📄',
  'application/': '📎',
  'text/': '📝',
};

export const FileDropZone: React.FC<FileDropZoneProps> = ({
  onFilesDropped,
  maxFileSize = 0, // 0 = unlimited
  acceptedTypes,
  isActive = true,
}) => {
  const [isDragging, setIsDragging] = useState(false);
  const [preview, setPreview] = useState<File[]>([]);
  const [error, setError] = useState<string>('');

  const getFileIcon = (file: File): string => {
    const type = file.type;

    for (const [prefix, icon] of Object.entries(FILE_TYPE_ICONS)) {
      if (type.startsWith(prefix)) return icon;
    }

    return '📎';
  };

  const validateFile = (file: File): boolean => {
    if (maxFileSize > 0 && file.size > maxFileSize) {
      setError(
        `File "${file.name}" exceeds maximum size of ${(maxFileSize / 1024 / 1024).toFixed(0)}MB`
      );
      return false;
    }

    if (acceptedTypes && !acceptedTypes.some((type) => file.type === type)) {
      setError(`File type "${file.type}" is not accepted`);
      return false;
    }

    return true;
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (isActive) setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    if (!isActive) return;

    const files = Array.from(e.dataTransfer.files);
    const validFiles = files.filter(validateFile);

    if (validFiles.length > 0) {
      setPreview(validFiles);
      setError('');
      onFilesDropped?.(validFiles);
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(2) + ' ' + sizes[i];
  };

  if (!isActive) return null;

  return (
    <div
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className={`fixed inset-0 z-40 flex items-center justify-center pointer-events-none transition-all ${
        isDragging
          ? 'bg-surface-800 opacity-100'
          : 'bg-transparent opacity-0'
      }`}
    >
      {isDragging && (
        <div className="pointer-events-auto flex flex-col items-center gap-4 rounded-lg border-2 border-dashed border-blue-500 bg-surface-800 p-12">
          <Upload size={48} className="text-blue-500 animate-bounce" />
          <div className="text-center">
            <p className="text-lg font-semibold text-white">
              Drop files here
            </p>
            <p className="text-sm text-gray-300">
              Release to upload
            </p>
          </div>

          {/* File Preview */}
          {preview.length > 0 && (
            <div className="mt-4 max-w-sm space-y-2">
              {preview.map((file) => (
                <div
                  key={file.name}
                  className="flex items-center gap-2 rounded bg-surface-900 p-2"
                >
                  <span className="text-2xl">{getFileIcon(file)}</span>
                  <div className="flex-1 min-w-0">
                    <p className="truncate text-sm font-medium text-white">
                      {file.name}
                    </p>
                    <p className="text-xs text-gray-500">
                      {formatFileSize(file.size)}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Error Message */}
          {error && (
            <div className="flex items-center gap-2 rounded bg-red-50 p-3">
              <AlertCircle size={18} className="text-red-500 flex-shrink-0" />
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
