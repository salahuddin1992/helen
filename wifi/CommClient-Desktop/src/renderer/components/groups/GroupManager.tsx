import React, { useEffect, useState, useMemo } from 'react';
import { useChatStore } from '@/stores/chat.store.v2';
import { useContactsStore } from '@/stores/contacts.store';
import { useAuthStore } from '@/stores/auth.store';
import { api } from '@/services/api.client';
import {
  Plus,
  Users,
  Settings,
  Trash2,
  UserPlus,
  UserMinus,
  Shield,
  Lock,
  X,
  ChevronDown,
  ChevronUp,
  Search,
} from 'lucide-react';
import { t } from '@/i18n';
import { Handle } from '@/components/common/Handle';

interface Group {
  id: string;
  name: string;
  description?: string;
  avatar?: string;
  memberCount: number;
  isAdmin: boolean;
}

interface GroupMember {
  id: string;
  displayName: string;
  username: string;
  avatar?: string;
  role: 'admin' | 'moderator' | 'member';
  joinedAt: string;
}

interface ExtendedGroup extends Group {
  members: GroupMember[];
}

const GroupManager: React.FC = () => {
  const { user } = useAuthStore();
  const { channels } = useChatStore();
  const { contacts } = useContactsStore();

  // Cast channels as groups (same structure)
  const groups = (channels as any[]).filter((c) => c.type === 'group');

  const [selectedGroup, setSelectedGroup] = useState<ExtendedGroup | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showAddMembersModal, setShowAddMembersModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [groupDetails, setGroupDetails] = useState<ExtendedGroup | null>(null);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);

  // Fetch groups on mount
  useEffect(() => {
    // Groups are loaded via useChatStore which loads channels
  }, []);

  // Fetch group details when selection changes
  useEffect(() => {
    if (!selectedGroup) return;

    const fetchDetails = async () => {
      setIsLoadingDetails(true);
      try {
        const details = await api.getChannel(selectedGroup.id);
        setGroupDetails(details);
      } catch (error) {
        console.error('Error fetching group details:', error);
      } finally {
        setIsLoadingDetails(false);
      }
    };

    fetchDetails();
  }, [selectedGroup]);

  // Filter groups by search
  const filteredGroups = useMemo(() => {
    return groups.filter(
      (g: any) =>
        (g.name || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
        (g.description || '').toLowerCase().includes(searchQuery.toLowerCase())
    );
  }, [groups, searchQuery]);

  const handleGroupClick = (group: Group) => {
    setSelectedGroup(group as ExtendedGroup);
  };

  return (
    <div className="w-full h-full flex bg-surface-950">
      {/* Groups list sidebar */}
      <div className="w-80 border-r border-surface-800 flex flex-col">
        {/* Header */}
        <div className="sticky top-0 z-10 bg-surface-950 border-b border-surface-800 p-4 space-y-4">
          <div className="flex items-center justify-between">
            <h1 className="text-xl font-bold text-text-100">
              {t('groups.title')}
            </h1>
            <button
              onClick={() => setShowCreateModal(true)}
              className="p-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition-colors"
              title={t('groups.create_group')}
            >
              <Plus size={20} />
            </button>
          </div>

          {/* Search */}
          <div className="relative">
            <Search
              size={16}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-text-400"
            />
            <input
              type="text"
              placeholder={t('groups.search_groups')}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-sm text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            />
          </div>

          <p className="text-xs text-text-400">
            {filteredGroups.length} {t('groups.groups')}
          </p>
        </div>

        {/* Groups list */}
        <div className="flex-1 overflow-y-auto">
          {filteredGroups.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full px-4 py-8 text-center">
              <Users size={32} className="text-text-600 mb-3" />
              <p className="text-sm text-text-400">{t('groups.no_groups')}</p>
            </div>
          ) : (
            <div className="space-y-1 p-2">
              {filteredGroups.map((group: any) => (
                <button
                  key={group.id}
                  onClick={() => handleGroupClick(group)}
                  className={`w-full text-left px-4 py-3 rounded-lg transition-all ${
                    selectedGroup?.id === group.id
                      ? 'bg-blue-600/20 border border-blue-500/30'
                      : 'hover:bg-surface-800'
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <div className="w-12 h-12 rounded-lg bg-gradient-to-br from-purple-500 to-pink-500 flex items-center justify-center text-white font-bold flex-shrink-0">
                      {group.name.charAt(0).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <h3 className="font-medium text-text-100 truncate">
                        {group.name}
                      </h3>
                      <p className="text-xs text-text-500 truncate">
                        {group.memberCount} {t('groups.members')}
                      </p>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Group details panel */}
      <div className="flex-1 flex flex-col">
        {selectedGroup ? (
          <>
            {/* Group header */}
            <div className="border-b border-surface-800 p-6 bg-surface-900/50">
              <div className="flex items-start gap-4">
                <div className="w-16 h-16 rounded-lg bg-gradient-to-br from-purple-500 to-pink-500 flex items-center justify-center text-white text-2xl font-bold">
                  {selectedGroup.name.charAt(0).toUpperCase()}
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <h2 className="text-2xl font-bold text-text-100">
                      {selectedGroup.name}
                    </h2>
                    {(selectedGroup as any).isAdmin && (
                      <Shield size={18} className="text-yellow-400" />
                    )}
                  </div>
                  {selectedGroup.description && (
                    <p className="text-sm text-text-400 mb-2">
                      {selectedGroup.description}
                    </p>
                  )}
                  <p className="text-xs text-text-500">
                    {selectedGroup.memberCount} {t('groups.members')}
                  </p>
                </div>

                {(selectedGroup as any).isAdmin && (
                  <button
                    className="p-2 rounded-lg hover:bg-surface-800 text-text-400 transition-colors"
                    title={t('groups.group_settings')}
                  >
                    <Settings size={20} />
                  </button>
                )}
              </div>
            </div>

            {/* Members section */}
            <div className="flex-1 overflow-y-auto">
              {isLoadingDetails ? (
                <div className="flex items-center justify-center h-full">
                  <div className="w-8 h-8 border-3 border-surface-700 border-t-blue-500 rounded-full animate-spin" />
                </div>
              ) : groupDetails ? (
                <div className="p-6">
                  {/* Add members button */}
                  {(selectedGroup as any).isAdmin && (
                    <button
                      onClick={() => setShowAddMembersModal(true)}
                      className="w-full mb-6 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors flex items-center justify-center gap-2"
                    >
                      <UserPlus size={18} />
                      {t('groups.add_members')}
                    </button>
                  )}

                  {/* Members list */}
                  <h3 className="text-lg font-semibold text-text-100 mb-4">
                    {t('groups.members')}
                  </h3>
                  <div className="space-y-2">
                    {groupDetails.members.map((member) => (
                      <MemberCard
                        key={member.id}
                        member={member}
                        channelId={selectedGroup.id}
                        isAdmin={selectedGroup.isAdmin}
                        isCurrentUser={member.id === user?.id}
                      />
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <Users size={48} className="text-text-600 mx-auto mb-4" />
              <p className="text-text-400">{t('groups.select_group')}</p>
            </div>
          </div>
        )}
      </div>

      {/* Modals */}
      {showCreateModal && (
        <CreateGroupModal
          onClose={() => setShowCreateModal(false)}
          onGroupCreated={(newGroup) => {
            setShowCreateModal(false);
          }}
          contacts={contacts}
        />
      )}

      {showAddMembersModal && selectedGroup && (
        <AddMembersModal
          groupId={selectedGroup.id}
          onClose={() => setShowAddMembersModal(false)}
          onMembersAdded={() => {
            if (selectedGroup) {
              setSelectedGroup(null);
              setTimeout(() => setSelectedGroup(selectedGroup), 100);
            }
          }}
          contacts={contacts}
          currentMembers={groupDetails?.members.map((m) => m.id) || []}
        />
      )}
    </div>
  );
};

const MemberCard: React.FC<{
  member: GroupMember;
  channelId: string;
  isAdmin: boolean;
  isCurrentUser: boolean;
}> = ({ member, channelId, isAdmin, isCurrentUser }) => {
  const handleRemoveMember = async () => {
    try {
      await api.removeMember(channelId, member.id);
    } catch (error) {
      console.error('Error removing member:', error);
    }
  };

  return (
    <div className="flex items-center justify-between p-3 rounded-lg bg-surface-900 border border-surface-800 hover:border-surface-700 transition-colors">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-white font-bold text-sm">
          {member.displayName.charAt(0).toUpperCase()}
        </div>
        <div>
          <div className="flex items-center gap-2">
            <span className="font-medium text-text-100">{member.displayName}</span>
            {member.role === 'admin' && (
              <Shield size={14} className="text-yellow-400" />
            )}
            {member.role === 'moderator' && (
              <Lock size={14} className="text-blue-400" />
            )}
          </div>
          <Handle user={member as any} className="text-xs text-text-500 block" />
        </div>
      </div>

      {isAdmin && !isCurrentUser && (
        <button
          onClick={handleRemoveMember}
          className="p-2 rounded-lg hover:bg-red-600/20 text-red-400 transition-colors"
          title={t('groups.remove_member')}
        >
          <UserMinus size={16} />
        </button>
      )}
    </div>
  );
};

const CreateGroupModal: React.FC<{
  onClose: () => void;
  onGroupCreated: (group: Group) => void;
  contacts: any[];
}> = ({ onClose, onGroupCreated, contacts }) => {
  const [groupName, setGroupName] = useState('');
  const [groupDescription, setGroupDescription] = useState('');
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [isCreating, setIsCreating] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const filteredContacts = useMemo(() => {
    return (contacts as any[]).filter(
      (c: any) =>
        (c.displayName || c.contact?.display_name || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
        (c.username || c.contact?.username || '').toLowerCase().includes(searchQuery.toLowerCase())
    );
  }, [contacts, searchQuery]);

  const handleCreate = async () => {
    if (!groupName.trim()) return;

    setIsCreating(true);
    try {
      const newGroup = await api.createChannel({
        type: 'group',
        name: groupName,
        description: groupDescription,
        member_ids: selectedMembers,
      });
      onGroupCreated(newGroup);
    } catch (error) {
      console.error('Error creating group:', error);
    } finally {
      setIsCreating(false);
    }
  };

  const toggleMember = (memberId: string) => {
    setSelectedMembers((prev) =>
      prev.includes(memberId)
        ? prev.filter((id) => id !== memberId)
        : [...prev, memberId]
    );
  };

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-surface-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
          <h2 className="text-lg font-bold text-text-100">
            {t('groups.create_group')}
          </h2>
          <button
            onClick={onClose}
            className="text-text-400 hover:text-text-200 transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {/* Group name */}
          <div>
            <label className="block text-sm font-medium text-text-200 mb-2">
              {t('groups.group_name')}
            </label>
            <input
              type="text"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              placeholder={t('groups.group_name_placeholder')}
              className="w-full px-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
              autoFocus
            />
          </div>

          {/* Group description */}
          <div>
            <label className="block text-sm font-medium text-text-200 mb-2">
              {t('groups.description')}
            </label>
            <textarea
              value={groupDescription}
              onChange={(e) => setGroupDescription(e.target.value)}
              placeholder={t('groups.description_placeholder')}
              rows={3}
              className="w-full px-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50 resize-none"
            />
          </div>

          {/* Members selection */}
          <div>
            <label className="block text-sm font-medium text-text-200 mb-2">
              {t('groups.select_members')} ({selectedMembers.length})
            </label>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('common.search')}
              className="w-full px-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-sm text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50 mb-3"
            />

            <div className="space-y-2 max-h-40 overflow-y-auto">
              {filteredContacts.map((contact: any) => (
                <label
                  key={contact.id}
                  className="flex items-center gap-3 p-2 rounded-lg hover:bg-surface-800 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={selectedMembers.includes(contact.id)}
                    onChange={() => toggleMember(contact.id)}
                    className="w-4 h-4 rounded bg-surface-700 border-surface-600 text-blue-600 focus:ring-blue-500"
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-text-100">
                      {contact.displayName || contact.contact?.display_name}
                    </p>
                    <Handle user={(contact.contact || contact) as any}
                            className="text-xs text-text-500 block" />
                  </div>
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-6 py-4 border-t border-surface-800">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-text-100 rounded-lg font-medium transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleCreate}
            disabled={!groupName.trim() || isCreating}
            className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 disabled:text-text-400 text-white rounded-lg font-medium transition-colors"
          >
            {isCreating ? t('common.creating') : t('groups.create')}
          </button>
        </div>
      </div>
    </div>
  );
};

const AddMembersModal: React.FC<{
  groupId: string;
  onClose: () => void;
  onMembersAdded: () => void;
  contacts: any[];
  currentMembers: string[];
}> = ({ groupId, onClose, onMembersAdded, contacts, currentMembers }) => {
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [isAdding, setIsAdding] = useState(false);

  const availableContacts = (contacts as any[]).filter(
    (c: any) => !currentMembers.includes(c.id)
  );

  const filteredContacts = useMemo(() => {
    return availableContacts.filter(
      (c: any) =>
        (c.displayName || c.contact?.display_name || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
        (c.username || c.contact?.username || '').toLowerCase().includes(searchQuery.toLowerCase())
    );
  }, [availableContacts, searchQuery]);

  const handleAddMembers = async () => {
    if (selectedMembers.length === 0) return;

    setIsAdding(true);
    try {
      await Promise.all(
        selectedMembers.map((memberId) =>
          api.addMember(groupId, { user_id: memberId })
        )
      );
      onMembersAdded();
    } catch (error) {
      console.error('Error adding members:', error);
    } finally {
      setIsAdding(false);
    }
  };

  const toggleMember = (memberId: string) => {
    setSelectedMembers((prev) =>
      prev.includes(memberId)
        ? prev.filter((id) => id !== memberId)
        : [...prev, memberId]
    );
  };

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-surface-900 rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
          <h2 className="text-lg font-bold text-text-100">
            {t('groups.add_members')}
          </h2>
          <button
            onClick={onClose}
            className="text-text-400 hover:text-text-200 transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('common.search')}
            className="w-full px-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            autoFocus
          />

          <div className="space-y-2">
            {filteredContacts.length === 0 ? (
              <p className="text-sm text-text-400 text-center py-8">
                {t('groups.no_available_members')}
              </p>
            ) : (
              filteredContacts.map((contact: any) => (
                <label
                  key={contact.id}
                  className="flex items-center gap-3 p-2 rounded-lg hover:bg-surface-800 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={selectedMembers.includes(contact.id)}
                    onChange={() => toggleMember(contact.id)}
                    className="w-4 h-4 rounded bg-surface-700 border-surface-600 text-blue-600 focus:ring-blue-500"
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-text-100">
                      {contact.displayName || contact.contact?.display_name}
                    </p>
                    <Handle user={(contact.contact || contact) as any}
                            className="text-xs text-text-500 block" />
                  </div>
                </label>
              ))
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-6 py-4 border-t border-surface-800">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-text-100 rounded-lg font-medium transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleAddMembers}
            disabled={selectedMembers.length === 0 || isAdding}
            className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 disabled:text-text-400 text-white rounded-lg font-medium transition-colors"
          >
            {isAdding ? t('common.adding') : t('groups.add')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default GroupManager;
