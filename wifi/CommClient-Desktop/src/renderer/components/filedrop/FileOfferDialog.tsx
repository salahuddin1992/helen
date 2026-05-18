/**
 * FileOfferDialog — Modal for receiving file offers
 * Shows sender, filename, size, accept/reject buttons
 */
import React from 'react';
import { Download, X } from 'lucide-react';

interface FileOffer {
  id: string;
  senderId: string;
  senderName: string;
  fileName: string;
  fileSize: number;
  fileType: string;
}

interface FileOfferDialogProps {
  offers: FileOffer[];
  onAccept?: (offerId: string) => void;
  onReject?: (offerId: string) => void;
}

export const FileOfferDialog: React.FC<FileOfferDialogProps> = ({
  offers,
  onAccept,
  onReject,
}) => {
  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(2) + ' ' + sizes[i];
  };

  if (offers.length === 0) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50 p-4">
      <div className="max-h-96 w-full max-w-md overflow-y-auto rounded-lg bg-white shadow-lg">
        <div className="border-b border-gray-200 px-6 py-4">
          <h3 className="text-lg font-semibold text-gray-900">
            Incoming Files
          </h3>
          <p className="text-sm text-gray-600">
            {offers.length} file{offers.length !== 1 ? 's' : ''} waiting to be accepted
          </p>
        </div>

        <div className="space-y-2 p-4">
          {offers.map((offer) => (
            <div
              key={offer.id}
              className="rounded-lg border border-gray-200 bg-gray-50 p-4"
            >
              {/* Offer Header */}
              <div className="mb-3">
                <p className="text-sm font-medium text-gray-900">
                  {offer.senderName} is sending you a file
                </p>
              </div>

              {/* File Info */}
              <div className="mb-4 flex items-start gap-3 rounded bg-white p-3">
                <div className="flex-shrink-0">
                  <Download
                    size={24}
                    className="text-blue-500"
                  />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="truncate font-medium text-gray-900">
                    {offer.fileName}
                  </p>
                  <p className="text-sm text-gray-600">
                    {formatFileSize(offer.fileSize)}
                  </p>
                  <p className="text-xs text-gray-500">
                    {offer.fileType}
                  </p>
                </div>
              </div>

              {/* Actions */}
              <div className="flex gap-2">
                <button
                  onClick={() => onReject?.(offer.id)}
                  className="flex-1 rounded border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 transition-colors"
                >
                  Decline
                </button>
                <button
                  onClick={() => onAccept?.(offer.id)}
                  className="flex-1 rounded bg-blue-500 px-3 py-2 text-sm font-medium text-white hover:bg-blue-600 transition-colors"
                >
                  Accept
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
