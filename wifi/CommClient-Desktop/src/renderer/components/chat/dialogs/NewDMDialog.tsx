/**
 * NewDMDialog.tsx
 * Modal dialog for creating a new direct message with a user.
 */

import React, { useState, useEffect } from 'react';
import { Search, X } from 'lucide-react';
import { t } from '@/i18n';
import { useContactsStore } from '@/stores/contacts.store';
import { useChatStore } from '@/stores/chat.store.v2';
import { useAuthStore } from '@/stores/auth.store';
import { Handle } from '@/components/common/Handle';

interface NewDMDialogProps {
  onClose: () => void;
  onCreateDm: (userId: string) => Promise<void>;
}

export function NewDMDialog({ onClose, onCreateDm }: NewDMDialogProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { allUsers, loadUsers, contacts, onlineUsers } = useContactsStore();
  const { channels } = useChatStore();

  useEffect(() => {
    loadUsers('');
  }, [loadUsers]);

  // Filter users based on search and exclude those already in DMs
  const existingDmUserIds = new Set(
    channels
      .filter((ch) => ch.type === 'dm')
      .flatMap((ch) =>
        ch.members
          .filter((m) => m.user_id !== (useAuthStore.getState().user?.id || ''))
          .map((m) => m.user_id)
      )
  );

  const filteredUsers = allUsers.filter(
    (user) =>
      !existingDmUserIds.has(user.id) &&
      (searchQuery === '' ||
        user.display_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        user.username.toLowerCase().includes(searchQuery.toLowerCase()))
  );

  const handleCreateDm = async () => {
    if (!selectedUserId) return;

    setIsLoading(true);
    setError(null);
    try {
      await onCreateDm(selectedUserId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create DM');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-slate-900 rounded-xl shadow-2xl w-96 border border-slate-800">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-slate-800">
          <h2 className="text-lg font-bold text-white">{t('chat.new_dm')}</h2>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-white transition"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-4">
          {/* Search input */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              type="text"
              placeholder={t('contacts.search')}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-slate-800 text-white rounded-lg border border-slate-700 focus:border-blue-500 focus:outline-none text-sm placeholder-slate-500"
            />
          </div>

          {/* Error message */}
          {error && (
            <p className="text-sm text-red-400 bg-red-500 bg-opacity-10 px-3 py-2 rounded">
              {error}
            </p>
          )}

          {/* User list */}
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {filteredUsers.length === 0 ? (
              <p className="text-center text-slate-400 text-sm py-4">
                {searchQuery ? 'No users found' : 'No available users'}
              </p>
            ) : (
              filteredUsers.map((user) => {
                const isOnline = onlineUsers[user.id] === 'online';
                const isSelected = selectedUserId === user.id;

                return (
                  <button
                    key={user.id}
                    onClick={() => setSelectedUserId(user.id)}
                    className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg transition ${
                      isSelected
                        ? 'bg-blue-600 border border-blue-500'
                        : 'bg-slate-800 hover:bg-slate-700 border border-slate-700'
                    }`}
                  >
                    {/* Avatar */}
                    <div className="relative flex-shrink-0">
                      <div className="w-10 h-10 rounded-full bg-gradient-to-br from-purple-400 to-pink-500 flex items-center justify-center text-white text-sm font-semibold">
                        {user.display_name.charAt(0).toUpperCase()}
                      </div>
                      <div
                        className={`absolute bottom-0 right-0 w-3 h-3 rounded-full border-2 border-slate-900 ${
                          isOnline ? 'bg-green-500' : 'bg-slate-500'
                        }`}
                      />
                    </div>

                    {/* User info */}
                    <div className="flex-1 text-left">
                      <p className="font-medium text-white text-sm">
                        {user.display_name}
                      </p>
                      <Handle user={user} className="text-xs text-slate-400 block" />
                    </div>

                    {/* Check indicator */}
                    {isSelected && (
                      <div className="w-5 h-5 bg-white rounded-full flex items-center justify-center">
                        <svg
                          className="w-3 h-3 text-blue-600"
                          fill="currentColor"
                          viewBox="0 0 20 20"
                        >
                          <path
                            fillRule="evenodd"
                            d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                            clipRule="evenodd"
                          />
                        </svg>
                      </div>
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex gap-3 p-6 border-t border-slate-800">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-lg font-medium transition"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleCreateDm}
            disabled={!selectedUserId || isLoading}
            className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? t('common.loading') : t('common.create')}
          </button>
        </div>
      </div>
    </div>
  );
}
