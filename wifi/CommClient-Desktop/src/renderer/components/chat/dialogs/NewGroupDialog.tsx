/**
 * NewGroupDialog.tsx
 * Modal dialog for creating a new group chat with multiple members.
 */

import React, { useState, useEffect } from 'react';
import { Search, X, Check } from 'lucide-react';
import { t } from '@/i18n';
import { useContactsStore } from '@/stores/contacts.store';
import { useAuthStore } from '@/stores/auth.store';
import { Handle } from '@/components/common/Handle';

interface NewGroupDialogProps {
  onClose: () => void;
  onCreateGroup: (name: string, memberIds: string[]) => Promise<void>;
}

export function NewGroupDialog({
  onClose,
  onCreateGroup,
}: NewGroupDialogProps) {
  const [groupName, setGroupName] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedMembers, setSelectedMembers] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<'name' | 'members'>('name');

  const { allUsers, loadUsers, onlineUsers } = useContactsStore();
  const currentUserId = useAuthStore.getState().user?.id || '';

  useEffect(() => {
    loadUsers('');
  }, [loadUsers]);

  // Filter users (exclude current user)
  const filteredUsers = allUsers.filter(
    (user) =>
      user.id !== currentUserId &&
      (searchQuery === '' ||
        user.display_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        user.username.toLowerCase().includes(searchQuery.toLowerCase()))
  );

  /**
   * Toggle user selection
   */
  const toggleMember = (userId: string) => {
    const updated = new Set(selectedMembers);
    if (updated.has(userId)) {
      updated.delete(userId);
    } else {
      updated.add(userId);
    }
    setSelectedMembers(updated);
  };

  /**
   * Handle create group
   */
  const handleCreateGroup = async () => {
    if (!groupName.trim()) {
      setError('Group name is required');
      return;
    }

    // Allow zero-member groups: user can create the group now and invite
    // members later from the channel header. This unblocks first-run
    // operators who haven't paired with anyone yet but want to set up the
    // structure ahead of time.
    setIsLoading(true);
    setError(null);
    try {
      await onCreateGroup(groupName.trim(), Array.from(selectedMembers));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create group');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-slate-900 rounded-xl shadow-2xl w-96 border border-slate-800">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-slate-800">
          <h2 className="text-lg font-bold text-white">
            {step === 'name' ? t('groups.create') : t('groups.members')}
          </h2>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-white transition"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-4">
          {/* Error message */}
          {error && (
            <p className="text-sm text-red-400 bg-red-500 bg-opacity-10 px-3 py-2 rounded">
              {error}
            </p>
          )}

          {step === 'name' ? (
            // Step 1: Group name
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-white mb-2">
                  {t('groups.name')}
                </label>
                <input
                  type="text"
                  value={groupName}
                  onChange={(e) => { setGroupName(e.target.value); if (error) setError(null); }}
                  onKeyDown={(e) => {
                    // Enter advances to member-selection step. Stops people
                    // hammering "Enter" expecting to submit and ending up
                    // staring at a half-finished form.
                    if (e.key === 'Enter' && groupName.trim()) {
                      e.preventDefault();
                      setStep('members');
                    }
                  }}
                  placeholder="e.g., Project Team"
                  className="w-full px-4 py-2 bg-slate-800 text-white rounded-lg border border-slate-700 focus:border-blue-500 focus:outline-none text-sm placeholder-slate-500"
                  autoFocus
                />
              </div>
              <p className="text-xs text-slate-400">
                Give your group a descriptive name. You can change it later.
              </p>
            </div>
          ) : (
            // Step 2: Member selection
            <div className="space-y-4">
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

              {/* Selected members summary */}
              {selectedMembers.size > 0 && (
                <div className="flex flex-wrap gap-2 p-3 bg-slate-800 rounded-lg">
                  {Array.from(selectedMembers).map((userId) => {
                    const user = allUsers.find((u) => u.id === userId);
                    return (
                      <div
                        key={userId}
                        className="flex items-center gap-2 px-3 py-1 bg-blue-600 rounded-full text-sm text-white"
                      >
                        <span>{user?.display_name || 'User'}</span>
                        <button
                          onClick={() => toggleMember(userId)}
                          className="hover:opacity-75 transition"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </div>
                    );
                  })}
                </div>
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
                    const isSelected = selectedMembers.has(user.id);

                    return (
                      <button
                        key={user.id}
                        onClick={() => toggleMember(user.id)}
                        className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg transition ${
                          isSelected
                            ? 'bg-blue-600 border border-blue-500'
                            : 'bg-slate-800 hover:bg-slate-700 border border-slate-700'
                        }`}
                      >
                        {/* Avatar */}
                        <div className="relative flex-shrink-0">
                          <div className="w-10 h-10 rounded-full bg-gradient-to-br from-green-400 to-cyan-500 flex items-center justify-center text-white text-sm font-semibold">
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
                          <Check className="w-5 h-5 text-white flex-shrink-0" />
                        )}
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex gap-3 p-6 border-t border-slate-800">
          <button
            onClick={
              step === 'members'
                ? () => setStep('name')
                : onClose
            }
            className="flex-1 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-lg font-medium transition"
          >
            {step === 'members' ? 'Back' : t('common.cancel')}
          </button>

          {step === 'name' ? (
            <button
              onClick={() => {
                if (groupName.trim()) {
                  setStep('members');
                  setError(null);
                } else {
                  setError('Group name is required');
                }
              }}
              disabled={!groupName.trim()}
              className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next
            </button>
          ) : (
            <button
              onClick={handleCreateGroup}
              disabled={isLoading}
              className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isLoading
                ? t('common.loading')
                : selectedMembers.size === 0
                    ? `${t('common.create')} (no members)`
                    : t('common.create')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
