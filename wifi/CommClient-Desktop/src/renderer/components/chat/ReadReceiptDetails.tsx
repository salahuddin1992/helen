/**
 * ReadReceiptDetails.tsx
 * Shows detailed delivery and read receipt information for a message
 */

import React, { useEffect, useState } from 'react';
import { X, Loader, CheckCheck, Check } from 'lucide-react';
import { useChatStore } from '@/stores/chat.store.v2';

export interface ReadReceiptDetailsProps {
  messageId: string;
  onClose: () => void;
}

interface ReceiptEntry {
  userId: string;
  userName: string;
  status: 'delivered' | 'read';
  timestamp: string;
}

export function ReadReceiptDetails({ messageId, onClose }: ReadReceiptDetailsProps) {
  const [activeTab, setActiveTab] = useState<'delivered' | 'read'>('delivered');

  // Store selectors
  const loadReceiptDetails = useChatStore((s) => s.loadReceiptDetails);
  const receiptDetails = useChatStore((s) => s.receiptDetails[messageId] || {});
  const [isLoading, setIsLoading] = useState(true);

  // Load receipt details on mount
  useEffect(() => {
    const load = async () => {
      setIsLoading(true);
      try {
        await loadReceiptDetails(messageId);
      } finally {
        setIsLoading(false);
      }
    };
    load();
  }, [messageId, loadReceiptDetails]);

  // Parse receipt data
  const deliveredUsers: ReceiptEntry[] = receiptDetails.delivered || [];
  const readUsers: ReceiptEntry[] = receiptDetails.read || [];

  const currentTabData = activeTab === 'delivered' ? deliveredUsers : readUsers;
  const totalCount = deliveredUsers.length + readUsers.length;

  const formatTime = (timestamp: string) => {
    try {
      const date = new Date(timestamp);
      return date.toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        hour12: true,
      });
    } catch {
      return timestamp;
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-slate-900 rounded-lg border border-slate-700 w-96 max-h-96 flex flex-col shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-slate-700">
          <h2 className="text-lg font-semibold text-white">Message Status</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-slate-800 rounded transition text-slate-400 hover:text-white"
            title="Close"
          >
            <X size={20} />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-slate-700 bg-slate-800">
          <button
            onClick={() => setActiveTab('delivered')}
            className={`flex-1 py-3 text-sm font-medium transition ${
              activeTab === 'delivered'
                ? 'border-b-2 border-blue-500 text-blue-400 bg-slate-700'
                : 'text-slate-400 hover:text-slate-300'
            }`}
          >
            <div className="flex items-center justify-center gap-2">
              <Check size={16} />
              <span>Delivered ({deliveredUsers.length})</span>
            </div>
          </button>
          <button
            onClick={() => setActiveTab('read')}
            className={`flex-1 py-3 text-sm font-medium transition ${
              activeTab === 'read'
                ? 'border-b-2 border-blue-500 text-blue-400 bg-slate-700'
                : 'text-slate-400 hover:text-slate-300'
            }`}
          >
            <div className="flex items-center justify-center gap-2">
              <CheckCheck size={16} />
              <span>Read ({readUsers.length})</span>
            </div>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center justify-center h-full">
              <Loader className="animate-spin text-blue-500" size={24} />
            </div>
          ) : currentTabData.length === 0 ? (
            <div className="flex items-center justify-center h-full text-slate-400">
              <p className="text-sm">
                {activeTab === 'delivered'
                  ? 'No delivered receipts'
                  : 'No read receipts'}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-slate-700">
              {currentTabData.map((entry, idx) => (
                <div
                  key={idx}
                  className="p-4 hover:bg-slate-800 transition flex items-center justify-between"
                >
                  <div className="flex-1">
                    <p className="text-sm font-medium text-slate-100">
                      {entry.userName}
                    </p>
                    <p className="text-xs text-slate-500 mt-1">
                      {formatTime(entry.timestamp)}
                    </p>
                  </div>
                  <div className="flex-shrink-0 text-slate-400">
                    {activeTab === 'delivered' ? (
                      <Check size={18} />
                    ) : (
                      <CheckCheck size={18} className="text-blue-400" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer summary */}
        <div className="border-t border-slate-700 p-3 bg-slate-800 text-xs text-slate-400 text-center">
          <p>
            {totalCount === 0
              ? 'Message sent'
              : `Seen by ${readUsers.length} of ${totalCount} recipients`}
          </p>
        </div>
      </div>
    </div>
  );
}
