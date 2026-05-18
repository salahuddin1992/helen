import React, { useEffect, useState, useMemo } from 'react';
import { useContactsStore } from '@/stores/contacts.store';
import { useChatStore } from '@/stores/chat.store.v2';
import { useCallStore } from '@/stores/call.store.v2';
import { useServerIdentityStore } from '@/stores/server-identity.store';
import { api } from '@/services/api.client';
import { AppLogger } from '@/services/AppLogger';

const _log = AppLogger.create('ContactList');
import {
  Search,
  Phone,
  Video,
  MessageCircle,
  Trash2,
  UserPlus,
  Star,
  Globe,
  Server,
  AlertCircle,
} from 'lucide-react';
import { t } from '@/i18n';
import { Handle } from '@/components/common/Handle';
import { Presence } from '@/components/common/Presence';

// share_code is a fixed 64-char alphanumeric token — see server's
// app/core/share_code.py. Validate client-side so we don't waste a
// roundtrip on obviously malformed codes.
const SHARE_CODE_RE = /^[A-Za-z0-9]{64}$/;

interface LookupResult {
  id: string;
  username: string;
  display_name: string;
  share_code: string;
  avatar_url?: string | null;
  bio?: string | null;
  origin_server?: { id?: string; name?: string; url?: string } | null;
}

interface ContactDisplay {
  id: string;
  username: string;
  displayName: string;
  avatar?: string;
  isOnline: boolean;
  isFavorite: boolean;
  status?: string;
}

const ContactCard: React.FC<{
  contact: ContactDisplay;
  onAction: (action: 'message' | 'audio' | 'video' | 'remove') => void;
  onFavorite: (isFav: boolean) => void;
  showContextMenu: boolean;
  onContextMenuChange: (show: boolean) => void;
}> = ({
  contact,
  onAction,
  onFavorite,
  showContextMenu,
  onContextMenuChange,
}) => {
  return (
    <div
      className="relative"
      onMouseEnter={() => onContextMenuChange(true)}
      onMouseLeave={() => onContextMenuChange(false)}
    >
      <div className="flex items-center gap-3 px-4 py-3 rounded-lg hover:bg-surface-800/50 transition-colors cursor-pointer group">
        {/* Avatar with status indicator */}
        <div className="relative flex-shrink-0">
          <div className="w-12 h-12 rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-white font-bold">
            {contact.displayName.charAt(0).toUpperCase()}
          </div>
          {contact.isOnline && (
            <div className="absolute bottom-0 right-0 w-3.5 h-3.5 bg-green-500 rounded-full border-2 border-surface-900" />
          )}
        </div>

        {/* Contact info */}
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-text-100 truncate">
            {contact.displayName}
          </h3>
          <Handle user={contact as any} className="text-xs text-text-500 truncate block" />
          <div className="mt-0.5">
            <Presence
              userId={contact.id}
              lastSeenAt={(contact as any).last_seen ?? (contact as any).lastSeen ?? null}
            />
          </div>
        </div>

        {/* Favorite button */}
        <button
          onClick={() => onFavorite(!contact.isFavorite)}
          className="opacity-0 group-hover:opacity-100 transition-opacity p-1 hover:bg-surface-700 rounded"
          title={
            contact.isFavorite
              ? t('contacts.remove_favorite')
              : t('contacts.add_favorite')
          }
        >
          {contact.isFavorite ? (
            <Star size={16} className="text-yellow-400 fill-current" />
          ) : (
            <Star size={16} className="text-text-400" />
          )}
        </button>

        {/* Action buttons */}
        {showContextMenu && (
          <div className="flex gap-1">
            <button
              onClick={() => onAction('message')}
              className="p-2 hover:bg-blue-600/20 text-blue-400 rounded-lg transition-colors"
              title={t('contacts.message')}
            >
              <MessageCircle size={16} />
            </button>
            <button
              onClick={() => onAction('audio')}
              className="p-2 hover:bg-green-600/20 text-green-400 rounded-lg transition-colors"
              title={t('contacts.audio_call')}
            >
              <Phone size={16} />
            </button>
            <button
              onClick={() => onAction('video')}
              className="p-2 hover:bg-purple-600/20 text-purple-400 rounded-lg transition-colors"
              title={t('contacts.video_call')}
            >
              <Video size={16} />
            </button>
            <button
              onClick={() => onAction('remove')}
              className="p-2 hover:bg-red-600/20 text-red-400 rounded-lg transition-colors"
              title={t('contacts.remove')}
            >
              <Trash2 size={16} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

const ContactList: React.FC = () => {
  const { contacts, removeContact } = useContactsStore();
  const { createDm } = useChatStore();
  const { initiateCall } = useCallStore();

  const [searchQuery, setSearchQuery] = useState('');
  const [showAddModal, setShowAddModal] = useState(false);
  const [contextMenuId, setContextMenuId] = useState<string | null>(null);
  const [addSearchQuery, setAddSearchQuery] = useState('');
  const [availableUsers, setAvailableUsers] = useState<ContactDisplay[]>([]);
  const [addingUser, setAddingUser] = useState(false);

  // Group contacts
  const groupedContacts = useMemo(() => {
    const filtered = contacts.map((c) => ({
      id: c.id,
      username: c.contact.username,
      displayName: c.contact.display_name,
      avatar: c.contact.avatar_url,
      isOnline: (c as any).isOnline || false,
      isFavorite: c.is_favorite,
      status: c.contact.bio,
    } as ContactDisplay)).filter(
      (c) =>
        c.displayName.toLowerCase().includes(searchQuery.toLowerCase()) ||
        c.username.toLowerCase().includes(searchQuery.toLowerCase())
    );

    const favs = filtered.filter((c) => c.isFavorite);
    const online = filtered.filter((c) => c.isOnline && !c.isFavorite);
    const offline = filtered.filter(
      (c) => !c.isOnline && !c.isFavorite
    );

    return { favorites: favs, online, offline };
  }, [contacts, searchQuery]);

  const handleAction = (contactId: string, action: string) => {
    const contact = contacts.find((c) => c.id === contactId);
    if (!contact) return;

    switch (action) {
      case 'message':
        createDm(contactId);
        break;
      case 'audio':
        initiateCall(contactId, 'audio');
        break;
      case 'video':
        initiateCall(contactId, 'video');
        break;
      case 'remove':
        if (window.confirm(t('contacts.confirm_remove'))) {
          removeContact(contactId);
        }
        break;
    }
  };

  const handleAddContact = async (userId: string) => {
    try {
      setAddingUser(true);
      // API call would go here
      await useContactsStore.getState().addContact(userId);
      setShowAddModal(false);
      setAddSearchQuery('');
    } catch (error) {
      _log.error('Error adding contact', error);
    } finally {
      setAddingUser(false);
    }
  };

  // Total contacts count
  const totalContacts =
    groupedContacts.favorites.length +
    groupedContacts.online.length +
    groupedContacts.offline.length;

  return (
    <div className="w-full h-full flex flex-col bg-surface-950">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-surface-950 border-b border-surface-800 px-4 py-4 space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-text-100">
            {t('contacts.title')}
          </h1>
          <button
            onClick={() => setShowAddModal(true)}
            className="p-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition-colors"
            title={t('contacts.add_contact')}
          >
            <UserPlus size={20} />
          </button>
        </div>

        {/* Search bar */}
        <div className="relative">
          <Search
            size={18}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-text-400"
          />
          <input
            type="text"
            placeholder={t('contacts.search_placeholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 bg-surface-900 border border-surface-800 rounded-lg text-text-100 placeholder-text-500 focus:outline-none focus:ring-2 focus:ring-blue-500/50"
          />
        </div>

        {/* Stats */}
        <div className="flex gap-4 text-sm text-text-400">
          <span>{totalContacts} {t('contacts.contacts')}</span>
          <span>{groupedContacts.online.length} {t('contacts.online')}</span>
        </div>
      </div>

      {/* Contacts list */}
      <div className="flex-1 overflow-y-auto">
        {totalContacts === 0 ? (
          <div className="flex flex-col items-center justify-center h-full px-4 text-center">
            <div className="text-surface-700 mb-4">
              <MessageCircle size={48} className="mx-auto opacity-50" />
            </div>
            <h3 className="text-lg font-medium text-text-300 mb-1">
              {t('contacts.no_contacts')}
            </h3>
            <p className="text-sm text-text-500 mb-6 max-w-xs">
              {t('contacts.no_contacts_hint')}
            </p>
            <button
              onClick={() => setShowAddModal(true)}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors"
            >
              {t('contacts.add_first_contact')}
            </button>
          </div>
        ) : (
          <div className="px-2 py-3 space-y-1">
            {/* Favorites */}
            {groupedContacts.favorites.length > 0 && (
              <div>
                <h2 className="text-xs font-semibold text-text-400 uppercase px-4 py-2 tracking-wide">
                  {t('contacts.favorites')}
                </h2>
                {groupedContacts.favorites.map((contact) => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    onAction={(action) => handleAction(contact.id, action)}
                    onFavorite={(isFav) => {
                      // Favorite toggle would be implemented with store action
                      _log.debug('Toggle favorite', { contactId: contact.id, isFav });
                    }}
                    showContextMenu={contextMenuId === contact.id}
                    onContextMenuChange={(show) =>
                      setContextMenuId(show ? contact.id : null)
                    }
                  />
                ))}
              </div>
            )}

            {/* Online contacts */}
            {groupedContacts.online.length > 0 && (
              <div>
                <h2 className="text-xs font-semibold text-text-400 uppercase px-4 py-2 tracking-wide">
                  {t('contacts.online')}
                </h2>
                {groupedContacts.online.map((contact) => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    onAction={(action) => handleAction(contact.id, action)}
                    onFavorite={(isFav) => {
                      // Favorite toggle would be implemented with store action
                      _log.debug('Toggle favorite', { contactId: contact.id, isFav });
                    }}
                    showContextMenu={contextMenuId === contact.id}
                    onContextMenuChange={(show) =>
                      setContextMenuId(show ? contact.id : null)
                    }
                  />
                ))}
              </div>
            )}

            {/* Offline contacts */}
            {groupedContacts.offline.length > 0 && (
              <div>
                <h2 className="text-xs font-semibold text-text-400 uppercase px-4 py-2 tracking-wide">
                  {t('contacts.offline')}
                </h2>
                {groupedContacts.offline.map((contact) => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    onAction={(action) => handleAction(contact.id, action)}
                    onFavorite={(isFav) => {
                      // Favorite toggle would be implemented with store action
                      _log.debug('Toggle favorite', { contactId: contact.id, isFav });
                    }}
                    showContextMenu={contextMenuId === contact.id}
                    onContextMenuChange={(show) =>
                      setContextMenuId(show ? contact.id : null)
                    }
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Add Contact Modal */}
      {showAddModal && (
        <AddContactModal
          onClose={() => setShowAddModal(false)}
          onAddContact={handleAddContact}
          isLoading={addingUser}
        />
      )}
    </div>
  );
};

const AddContactModal: React.FC<{
  onClose: () => void;
  onAddContact: (userId: string) => void;
  isLoading: boolean;
}> = ({ onClose, onAddContact, isLoading }) => {
  const { createDm } = useChatStore();
  const { initiateCall } = useCallStore();
  const serverName = useServerIdentityStore((s) => s.serverName);
  const peerCount = useServerIdentityStore((s) => s.peers.length);

  const [code, setCode] = useState('');
  const [result, setResult] = useState<LookupResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = code.trim();
  const isValidFormat = SHARE_CODE_RE.test(trimmed);
  const isRemote = !!result?.origin_server;

  const runLookup = async () => {
    setError(null);
    setResult(null);
    if (!isValidFormat) {
      setError(t('contacts.invalid_share_code'));
      return;
    }
    setSearching(true);
    try {
      const user = await api.lookupByCode(trimmed);
      setResult(user as LookupResult);
    } catch (e: any) {
      const status = e?.status || e?.response?.status;
      if (status === 404) {
        setError(t('contacts.share_code_not_found'));
      } else if (status === 400) {
        setError(t('contacts.invalid_share_code'));
      } else {
        setError(t('contacts.lookup_failed'));
      }
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-surface-900 rounded-2xl shadow-2xl w-full max-w-md">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
          <h2 className="text-lg font-bold text-text-100">
            {t('contacts.search_by_code')}
          </h2>
          <button
            onClick={onClose}
            className="text-text-400 hover:text-text-200 transition-colors"
          >
            ✕
          </button>
        </div>

        {/* Input */}
        <div className="p-6 border-b border-surface-800 space-y-3">
          <p className="text-xs text-text-400">
            {t('contacts.share_code_hint')}
          </p>
          {/* Federation-scope hint: makes it obvious the search covers
              both the local server AND every connected peer. */}
          <div className="flex items-start gap-2 text-[11px] text-text-500 bg-surface-800/50 rounded-md p-2">
            <Server size={12} className="mt-0.5 shrink-0 text-blue-400" />
            <div className="space-y-0.5">
              <div>
                {t('server.searching_local')}
                {serverName ? ` — ${serverName}` : ''}
              </div>
              {peerCount > 0 && (
                <div className="text-purple-400">
                  {t('server.searching_federated')} ({peerCount})
                </div>
              )}
            </div>
          </div>
          <textarea
            placeholder={t('contacts.share_code_placeholder')}
            value={code}
            onChange={(e) => {
              setCode(e.target.value);
              setError(null);
              setResult(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                runLookup();
              }
            }}
            rows={2}
            className="w-full px-4 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 placeholder-text-500 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/50 resize-none"
            autoFocus
            dir="ltr"
          />
          <div className="flex items-center justify-between">
            <span className={`text-xs ${isValidFormat ? 'text-green-400' : 'text-text-500'}`}>
              {trimmed.length}/64
            </span>
            <button
              onClick={runLookup}
              disabled={!isValidFormat || searching}
              className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 disabled:text-text-500 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {searching ? t('common.loading') : t('contacts.search')}
            </button>
          </div>
        </div>

        {/* Result / Error */}
        <div className="max-h-80 overflow-y-auto">
          {error && (
            <div className="px-6 py-4 flex items-start gap-3 text-red-400">
              <AlertCircle size={18} className="flex-shrink-0 mt-0.5" />
              <p className="text-sm">{error}</p>
            </div>
          )}

          {result && (
            <div className="p-6 space-y-4">
              <div className="flex items-center gap-3">
                <div className="w-14 h-14 rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-white font-bold text-xl">
                  {(result.display_name || result.username || '?').charAt(0).toUpperCase()}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-text-100 truncate">{result.display_name}</p>
                  <Handle user={result} className="text-sm text-text-500 truncate block" />
                </div>
              </div>

              {/* Origin badge */}
              <div
                className={`flex items-center gap-2 text-xs px-3 py-2 rounded-lg ${
                  isRemote
                    ? 'bg-purple-900/30 text-purple-300 border border-purple-700/40'
                    : 'bg-green-900/30 text-green-300 border border-green-700/40'
                }`}
              >
                {isRemote ? <Globe size={14} /> : <Server size={14} />}
                <span>
                  {isRemote
                    ? t('contacts.found_on_remote').replace(
                        '{server}',
                        result.origin_server?.name || result.origin_server?.url || '—',
                      )
                    : t('contacts.found_locally')}
                </span>
              </div>

              {result.bio && (
                <p className="text-sm text-text-400 italic">{result.bio}</p>
              )}

              {/* Actions */}
              <div className="grid grid-cols-3 gap-2 pt-2">
                <button
                  onClick={() => {
                    createDm(result.id);
                    onClose();
                  }}
                  className="flex flex-col items-center gap-1 py-2 px-2 bg-surface-800 hover:bg-blue-600/20 text-blue-400 rounded-lg transition-colors"
                  title={t('contacts.message')}
                >
                  <MessageCircle size={18} />
                  <span className="text-xs">{t('contacts.message')}</span>
                </button>
                <button
                  onClick={() => {
                    initiateCall(result.id, 'audio');
                    onClose();
                  }}
                  className="flex flex-col items-center gap-1 py-2 px-2 bg-surface-800 hover:bg-green-600/20 text-green-400 rounded-lg transition-colors"
                  title={t('contacts.audio_call')}
                >
                  <Phone size={18} />
                  <span className="text-xs">{t('contacts.audio_call')}</span>
                </button>
                <button
                  onClick={() => {
                    initiateCall(result.id, 'video');
                    onClose();
                  }}
                  className="flex flex-col items-center gap-1 py-2 px-2 bg-surface-800 hover:bg-purple-600/20 text-purple-400 rounded-lg transition-colors"
                  title={t('contacts.video_call')}
                >
                  <Video size={18} />
                  <span className="text-xs">{t('contacts.video_call')}</span>
                </button>
              </div>

              {!isRemote && (
                <button
                  onClick={() => onAddContact(result.id)}
                  disabled={isLoading}
                  className="w-full py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 text-white rounded-lg font-medium transition-colors flex items-center justify-center gap-2"
                >
                  <UserPlus size={16} />
                  {t('contacts.add_contact')}
                </button>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-surface-800">
          <button
            onClick={onClose}
            className="w-full py-2 bg-surface-800 hover:bg-surface-700 text-text-100 rounded-lg font-medium transition-colors"
          >
            {t('common.close')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ContactList;
