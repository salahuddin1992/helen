/**
 * TransferProgress — Progress cards for file transfers
 * Shows filename, progress bar, speed, ETA, cancel button
 * Supports multiple concurrent transfers
 */
import React from 'react';
import { X, Pause, Play } from 'lucide-react';

export interface Transfer {
  id: string;
  fileName: string;
  fileSize: number;
  uploadedBytes: number;
  speed: number; // bytes per second
  direction?: 'upload' | 'download';
  status: 'uploading' | 'downloading' | 'paused' | 'completed' | 'error';
  error?: string;
}

interface TransferProgressProps {
  transfers: Transfer[];
  onCancel?: (transferId: string) => void;
  onPause?: (transferId: string) => void;
  onResume?: (transferId: string) => void;
}

export const TransferProgress: React.FC<TransferProgressProps> = ({
  transfers,
  onCancel,
  onPause,
  onResume,
}) => {
  const formatBytes = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(2) + ' ' + sizes[i];
  };

  const formatSpeed = (bytesPerSecond: number): string => {
    if (bytesPerSecond === 0) return '0 B/s';
    const k = 1024;
    const sizes = ['B/s', 'KB/s', 'MB/s'];
    const i = Math.floor(Math.log(bytesPerSecond) / Math.log(k));
    return (bytesPerSecond / Math.pow(k, i)).toFixed(1) + ' ' + sizes[i];
  };

  const calculateETA = (transfer: Transfer): string => {
    const remaining = transfer.fileSize - transfer.uploadedBytes;
    if (transfer.speed === 0 || (transfer.status !== 'uploading' && transfer.status !== 'downloading')) return '--:--';

    const seconds = Math.ceil(remaining / transfer.speed);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  };

  if (transfers.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-40 space-y-2 max-w-sm">
      {transfers.map((transfer) => {
        const progress = (transfer.uploadedBytes / transfer.fileSize) * 100;
        const isCompleted = transfer.status === 'completed';
        const isError = transfer.status === 'error';
        const isPaused = transfer.status === 'paused';

        return (
          <div
            key={transfer.id}
            className={`rounded-lg p-3 shadow-lg ${
              isError
                ? 'border border-red-500/30 bg-red-900/20'
                : isCompleted
                ? 'border border-green-500/30 bg-green-900/20'
                : 'bg-surface-900 border border-surface-700'
            }`}
          >
            {/* Header */}
            <div className="flex items-start justify-between gap-2 mb-2">
              <div className="flex-1 min-w-0">
                <p className="truncate text-sm font-medium text-white">
                  {transfer.fileName}
                </p>
                <p className="text-xs text-gray-400">
                  {formatBytes(transfer.uploadedBytes)} / {formatBytes(transfer.fileSize)}
                </p>
              </div>
              <button
                onClick={() => onCancel?.(transfer.id)}
                className="flex-shrink-0 p-1 text-gray-400 hover:text-red-400 transition-colors"
              >
                <X size={16} />
              </button>
            </div>

            {/* Progress Bar */}
            <div className="mb-2 h-2 rounded-full overflow-hidden bg-surface-700">
              <div
                className={`h-full transition-all ${
                  isError ? 'bg-red-500' : isCompleted ? 'bg-green-500' : 'bg-blue-500'
                }`}
                style={{ width: `${progress}%` }}
              />
            </div>

            {/* Stats */}
            <div className="flex items-center justify-between text-xs">
              <div className="flex gap-2 text-gray-300">
                <span>{progress.toFixed(0)}%</span>
                {!isCompleted && !isError && (
                  <>
                    <span>•</span>
                    <span>{formatSpeed(transfer.speed)}</span>
                    <span>•</span>
                    <span>ETA: {calculateETA(transfer)}</span>
                  </>
                )}
              </div>

              {/* Controls */}
              <div className="flex gap-1">
                {(transfer.status === 'uploading' || transfer.status === 'downloading') && (
                  <button
                    onClick={() => onPause?.(transfer.id)}
                    className="p-1 text-gray-400 hover:text-white"
                  >
                    <Pause size={14} />
                  </button>
                )}
                {isPaused && (
                  <button
                    onClick={() => onResume?.(transfer.id)}
                    className="p-1 text-gray-400 hover:text-white"
                  >
                    <Play size={14} />
                  </button>
                )}
              </div>
            </div>

            {/* Error Message */}
            {isError && transfer.error && (
              <p className="mt-2 text-xs text-red-600">{transfer.error}</p>
            )}

            {/* Completed Message */}
            {isCompleted && (
              <p className="mt-2 text-xs text-green-600">
                {transfer.direction === 'download' ? 'Download complete' : 'Upload complete'}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
};
