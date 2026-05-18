/**
 * SimpleCreateGroup.tsx — Frictionless 2-step group creation.
 *
 * Step 1: AlertCircle a group name (just one field, no description)
 * Step 2: Tap on people to add them (visual toggle, no checkboxes)
 * → Created! Auto-navigates to the new group.
 *
 * Design principles:
 *   - Zero jargon: "New Group", not "Create Channel"
 *   - Big touch targets (56px rows)
 *   - Immediate visual feedback (avatars light up on tap)
 *   - ChevronRight step 2 allowed (create empty, add later)
 *   - Auto-closes on success
 */

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { X, ArrowRight, Check, Loader2, Search, Users, UserPlus } from 'lucide-react';
import { t } from '@/i18n';
import { useChatStore } from '@/stores/chat.store.v2';
import { useContactsStore } from '@/stores/contacts.store';

interface SimpleCreateGroupProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated?: (channelId: string) => void;
}

interface SelectableUser {
  id: string;
  displayName: string;
  username: string;
  avatar?: string;
  isOnline: boolean;
  selected: boolean;
}

const SimpleCreateGroup: React.FC<SimpleCreateGroupProps> = ({ isOpen, onClose, onCreated }) => {
  const [step, setStep] = useState<1 | 2>(1);
  const [groupName, setGroupName] = useState('');
  const [search, setSearch] = useState('');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState('');

  const createGroup = useChatStore((s) => s.createGroup);
  const contacts = useContactsStore((s) => s.contacts);
  const allUsers = useContactsStore((s) => s.allUsers);
  const onlineUsers = useContactsStore((s) => s.onlineUsers);
  const loadUsers = useContactsStore((s) => s.loadUsers);

  // Load users on open
  useEffect(() => {
    if (isOpen) {
      loadUsers();
      // Reset state
      setStep(1);
      setGroupName('');
      setSearch('');
      setSelectedIds(new Set());
      setError('');
      setIsCreating(false);
    }
  }, [isOpen]);

  // Merge contacts + allUsers, deduplicate, sort online first
  const people = useMemo(() => {
    const map = new Map<string, SelectableUser>();

    for (const u of allUsers) {
      map.set(u.id, {
        id: u.id,
        displayName: u.display_name || u.username,
        username: u.username,
        avatar: u.avatar_url || undefined,
        isOnline: !!onlineUsers[u.id],
        selected: selectedIds.has(u.id),
      });
    }

    for (const c of contacts) {
      if (!map.has(c.contact.id)) {
        map.set(c.contact.id, {
          id: c.contact.id,
          displayName: c.contact.display_name || c.contact.username,
          username: c.contact.username,
          avatar: c.contact.avatar_url || undefined,
          isOnline: !!onlineUsers[c.contact.id],
          selected: selectedIds.has(c.contact.id),
        });
      }
    }

    const result = Array.from(map.values());

    // Filter by search
    const q = search.toLowerCase().trim();
    const filtered = q
      ? result.filter((u) =>
          u.displayName.toLowerCase().includes(q) ||
          u.username.toLowerCase().includes(q)
        )
      : result;

    // Sort: selected first, then online, then alphabetical
    filtered.sort((a, b) => {
      if (a.selected !== b.selected) return a.selected ? -1 : 1;
      if (a.isOnline !== b.isOnline) return a.isOnline ? -1 : 1;
      return a.displayName.localeCompare(b.displayName);
    });

    return filtered;
  }, [allUsers, contacts, onlineUsers, selectedIds, search]);

  const togglePerson = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleStep1Next = () => {
    setError('');
    if (!groupName.trim()) {
      setError(t('group.name_required') || 'Give your group a name');
      return;
    }
    setStep(2);
  };

  const handleCreate = async () => {
    setError('');
    setIsCreating(true);

    try {
      const channel = await createGroup(groupName.trim(), Array.from(selectedIds));
      onCreated?.(channel.id);
      onClose();
    } catch (e: any) {
      setError(e?.message || t('group.create_failed') || 'Could not create the group');
      setIsCreating(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && step === 1) {
      e.preventDefault();
      handleStep1Next();
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="w-full max-w-md mx-4 bg-surface-900 rounded-2xl border border-surface-800 shadow-2xl overflow-hidden">

        {/* ─── Header ─── */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-surface-800">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-blue-600/20 flex items-center justify-center">
              <Users size={18} className="text-blue-400" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-white">
                {t('group.new_group') || 'New Group'}
              </h2>
              <p className="text-xs text-gray-500">
                {step === 1
                  ? (t('group.step_name') || 'Step 1: Choose a name')
                  : (t('group.step_people') || 'Step 2: Add people')}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-surface-800 flex items-center justify-center text-gray-500 hover:text-gray-300 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Step dots */}
        <div className="flex items-center justify-center gap-2 py-3">
          <div className={`h-1.5 rounded-full transition-all duration-300 ${step === 1 ? 'w-6 bg-blue-500' : 'w-1.5 bg-blue-400'}`} />
          <div className={`h-1.5 rounded-full transition-all duration-300 ${step === 2 ? 'w-6 bg-blue-500' : 'w-1.5 bg-surface-700'}`} />
        </div>

        {/* ─── Step 1: Group Name ─── */}
        {step === 1 && (
          <div className="px-5 pb-5 animate-fadeIn">
            <label className="block text-sm font-medium text-gray-300 mb-2">
              {t('group.name_label') || 'Group name'}
            </label>
            <input
              type="text"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t('group.name_placeholder') || 'e.g. Team Chat, Family, Project X'}
              className="w-full px-4 py-3.5 bg-surface-800 border border-surface-700 rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all text-base"
              autoFocus
              maxLength={50}
            />

            {error && (
              <p className="text-sm text-red-400 mt-2">{error}</p>
            )}

            <button
              onClick={handleStep1Next}
              className="w-full mt-4 py-3.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-2 text-base"
            >
              {t('group.next_add_people') || 'Next: Add People'}
              <ArrowRight size={18} />
            </button>
          </div>
        )}

        {/* ─── Step 2: Add People ─── */}
        {step === 2 && (
          <div className="animate-fadeIn">
            {/* Selected count + search */}
            <div className="px-5 pb-3">
              {selectedIds.size > 0 && (
                <div className="flex items-center gap-2 mb-3">
                  <div className="flex -space-x-2">
                    {Array.from(selectedIds).slice(0, 5).map((id) => {
                      const person = people.find((p) => p.id === id);
                      if (!person) return null;
                      return (
                        <div
                          key={id}
                          className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-xs font-bold border-2 border-surface-900"
                        >
                          {person.displayName.charAt(0).toUpperCase()}
                        </div>
                      );
                    })}
                    {selectedIds.size > 5 && (
                      <div className="w-8 h-8 rounded-full bg-surface-700 flex items-center justify-center text-gray-400 text-xs font-bold border-2 border-surface-900">
                        +{selectedIds.size - 5}
                      </div>
                    )}
                  </div>
                  <span className="text-sm text-gray-400">
                    {selectedIds.size} {t('group.selected') || 'selected'}
                  </span>
                </div>
              )}

              {/* Search */}
              <div className="relative">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('group.search_people') || 'Search people...'}
                  className="w-full pl-9 pr-4 py-2.5 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  autoFocus
                />
              </div>
            </div>

            {/* People list */}
            <div className="max-h-64 overflow-y-auto px-2">
              {people.length === 0 ? (
                <div className="text-center py-8 text-gray-500 text-sm">
                  {t('group.no_people') || 'No people found'}
                </div>
              ) : (
                people.map((person) => (
                  <button
                    key={person.id}
                    onClick={() => togglePerson(person.id)}
                    className={`w-full flex items-center gap-3 px-3 py-3 rounded-xl transition-all mb-0.5 ${
                      person.selected
                        ? 'bg-blue-600/15 border border-blue-500/30'
                        : 'hover:bg-surface-800 border border-transparent'
                    }`}
                  >
                    {/* Avatar */}
                    <div className="relative">
                      <div className={`w-10 h-10 rounded-full flex items-center justify-center text-white text-sm font-bold ${
                        person.selected
                          ? 'bg-blue-600'
                          : 'bg-gradient-to-br from-surface-600 to-surface-700'
                      }`}>
                        {person.selected
                          ? <Check size={18} />
                          : person.displayName.charAt(0).toUpperCase()
                        }
                      </div>
                      {/* Online dot */}
                      {person.isOnline && (
                        <div className="absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 bg-green-500 rounded-full border-2 border-surface-900" />
                      )}
                    </div>

                    {/* Name */}
                    <div className="flex-1 text-left min-w-0">
                      <p className={`text-sm font-medium truncate ${person.selected ? 'text-blue-300' : 'text-white'}`}>
                        {person.displayName}
                      </p>
                      <p className="text-xs text-gray-500 truncate">
                        {person.isOnline ? (t('status.online') || 'Online') : (t('status.offline') || 'Offline')}
                      </p>
                    </div>

                    {/* Tap indicator */}
                    {!person.selected && (
                      <UserPlus size={16} className="text-gray-600 flex-shrink-0" />
                    )}
                  </button>
                ))
              )}
            </div>

            {/* Error */}
            {error && (
              <div className="px-5 pt-2">
                <p className="text-sm text-red-400">{error}</p>
              </div>
            )}

            {/* Action buttons */}
            <div className="flex gap-3 px-5 py-4 border-t border-surface-800">
              <button
                onClick={() => { setStep(1); setError(''); }}
                disabled={isCreating}
                className="px-4 py-3 text-gray-400 hover:text-gray-300 text-sm transition-colors"
              >
                {t('onboarding.back') || '← Back'}
              </button>
              <button
                onClick={handleCreate}
                disabled={isCreating}
                className="flex-1 py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-600/50 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-2 text-base"
              >
                {isCreating ? (
                  <>
                    <Loader2 size={18} className="animate-spin" />
                    {t('group.creating') || 'Creating...'}
                  </>
                ) : (
                  <>
                    <Check size={18} />
                    {selectedIds.size > 0
                      ? `${t('group.create_with') || 'Create with'} ${selectedIds.size} ${t('group.people') || 'people'}`
                      : (t('group.create_empty') || 'Create Group')
                    }
                  </>
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default SimpleCreateGroup;
