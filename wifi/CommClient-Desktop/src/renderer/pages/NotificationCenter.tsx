/**
 * NotificationCenter.tsx — Full notification center page.
 *
 * Features:
 * - List of notifications with icons, titles, timestamps
 * - Filter tabs: All, Unread, Messages, Calls, System
 * - Mark as read (single/all), Delete notification (single/all)
 * - Click to navigate to related page
 * - Empty state with illustration
 * - Real-time socket updates via Zustand store listener
 */

import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useNotificationStore, type NotificationType } from '@/stores/notification.store';
import {
  MessageSquare,
  PhoneMissed,
  Phone,
  UserPlus,
  Users,
  Info,
  AtSign,
  Trash2,
  CheckCheck,
  Trash,
  ArrowRight,
  Loader,
  RefreshCw,
} from 'lucide-react';
import { t } from '@/i18n';

type FilterTab = 'all' | 'unread' | 'messages' | 'calls' | 'system';

const getNotificationIcon = (type: NotificationType) => {
  switch (type) {
    case 'message':
      return <MessageSquare size={20} className="text-blue-400" />;
    case 'call_missed':
      return <PhoneMissed size={20} className="text-red-400" />;
    case 'call_incoming':
      return <Phone size={20} className="text-green-400" />;
    case 'call_ended':
      return <Phone size={20} className="text-gray-400" />;
    case 'contact_request':
      return <UserPlus size={20} className="text-purple-400" />;
    case 'contact_accepted':
      return <UserPlus size={20} className="text-green-400" />;
    case 'group_invite':
      return <Users size={20} className="text-cyan-400" />;
    case 'mention':
      return <AtSign size={20} className="text-yellow-400" />;
    case 'system':
    default:
      return <Info size={20} className="text-gray-400" />;
  }
};

const getRelativeTime = (timestamp: string): string => {
  const now = new Date();
  const date = new Date(timestamp);
  const seconds = Math.floor((now.getTime() - date.getTime()) / 1000);

  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;

  return date.toLocaleDateString();
};

const NotificationCenter: React.FC = () => {
  const navigate = useNavigate();
  const {
    notifications,
    unreadCount,
    isLoading,
    error,
    fetchNotifications,
    fetchUnreadCount,
    markRead,
    markAllRead,
    deleteNotification,
    deleteAllNotifications,
    clearError,
  } = useNotificationStore();

  const [activeFilter, setActiveFilter] = useState<FilterTab>('all');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Fetch notifications on mount and when filter changes
  useEffect(() => {
    fetchNotifications(100, 0, activeFilter === 'unread');
    fetchUnreadCount();
  }, [fetchNotifications, fetchUnreadCount, activeFilter]);

  // Filter notifications
  const filteredNotifications = React.useMemo(() => {
    let result = notifications;

    switch (activeFilter) {
      case 'unread':
        result = result.filter((n) => !n.read);
        break;
      case 'messages':
        result = result.filter((n) => n.type === 'message' || n.type === 'mention');
        break;
      case 'calls':
        result = result.filter((n) =>
          ['call_missed', 'call_incoming', 'call_ended'].includes(n.type)
        );
        break;
      case 'system':
        result = result.filter(
          (n) =>
            n.type === 'system' ||
            n.type === 'contact_request' ||
            n.type === 'contact_accepted' ||
            n.type === 'group_invite'
        );
        break;
      default:
        // 'all'
        break;
    }

    return result;
  }, [notifications, activeFilter]);

  // Handle notification click - mark read and navigate
  const handleNotificationClick = (notification: typeof notifications[0]) => {
    if (!notification.read) {
      markRead([notification.id]);
    }

    if (notification.action_url) {
      navigate(notification.action_url);
    } else if (notification.related_channel_id) {
      navigate(`/chat/${notification.related_channel_id}`);
    } else if (notification.related_user_id) {
      navigate(`/contacts/${notification.related_user_id}`);
    }
  };

  // Handle selection toggle
  const toggleSelection = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // Handle select all
  const toggleSelectAll = () => {
    if (selectedIds.size === filteredNotifications.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredNotifications.map((n) => n.id)));
    }
  };

  // Handle bulk operations
  const handleMarkSelectedAsRead = async () => {
    const ids = Array.from(selectedIds);
    await markRead(ids);
    setSelectedIds(new Set());
  };

  const handleDeleteSelected = async () => {
    const ids = Array.from(selectedIds);
    for (const id of ids) {
      await deleteNotification(id);
    }
    setSelectedIds(new Set());
  };

  const handleRefresh = async () => {
    setIsRefreshing(true);
    try {
      await fetchNotifications(100, 0, activeFilter === 'unread');
      await fetchUnreadCount();
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-surface-900">
      {/* Header */}
      <div className="border-b border-surface-800 px-6 py-4">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-text-100">Notifications</h1>
            {unreadCount > 0 && (
              <p className="text-sm text-text-400 mt-1">
                {unreadCount} unread {unreadCount === 1 ? 'notification' : 'notifications'}
              </p>
            )}
          </div>
          <button
            onClick={handleRefresh}
            disabled={isRefreshing}
            className="p-2 hover:bg-surface-800 rounded-lg transition-colors disabled:opacity-50"
            title="Refresh notifications"
          >
            <RefreshCw size={20} className={isRefreshing ? 'animate-spin' : ''} />
          </button>
        </div>

        {/* Filter tabs */}
        <div className="border-b border-surface-800 -mx-6 px-6 flex gap-4 overflow-x-auto">
          {(['all', 'unread', 'messages', 'calls', 'system'] as const).map((filter) => (
            <button
              key={filter}
              onClick={() => {
                setActiveFilter(filter);
                setSelectedIds(new Set());
              }}
              className={`px-4 py-3 font-medium text-sm capitalize transition-colors border-b-2 whitespace-nowrap ${
                activeFilter === filter
                  ? 'border-primary-500 text-primary-400'
                  : 'border-transparent text-text-400 hover:text-text-300'
              }`}
            >
              {filter}
            </button>
          ))}
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="mx-6 mt-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-start gap-3">
          <div className="flex-1">
            <p className="text-sm text-red-400">{error}</p>
          </div>
          <button
            onClick={clearError}
            className="text-red-400 hover:text-red-300 flex-shrink-0"
          >
            ×
          </button>
        </div>
      )}

      {/* Bulk actions toolbar */}
      {selectedIds.size > 0 && (
        <div className="border-b border-surface-800 px-6 py-3 bg-surface-800/40 flex items-center gap-4">
          <span className="text-sm text-text-400">
            {selectedIds.size} selected
          </span>
          <button
            onClick={handleMarkSelectedAsRead}
            className="flex items-center gap-2 px-3 py-1 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded transition-colors"
          >
            <CheckCheck size={16} />
            Mark as Read
          </button>
          <button
            onClick={handleDeleteSelected}
            className="flex items-center gap-2 px-3 py-1 text-sm bg-red-600 hover:bg-red-700 text-white rounded transition-colors"
          >
            <Trash2 size={16} />
            Delete
          </button>
        </div>
      )}

      {/* Notifications list or empty state */}
      <div className="flex-1 overflow-y-auto">
        {isLoading && filteredNotifications.length === 0 ? (
          // Loading state
          <div className="flex items-center justify-center h-full">
            <div className="flex flex-col items-center gap-3 text-text-400">
              <Loader size={32} className="animate-spin" />
              <p>Loading notifications...</p>
            </div>
          </div>
        ) : filteredNotifications.length === 0 ? (
          // Empty state
          <div className="flex items-center justify-center h-full">
            <div className="flex flex-col items-center gap-4 text-text-400">
              <div className="text-6xl opacity-50">📭</div>
              <div className="text-center">
                <p className="font-medium mb-1">No notifications</p>
                <p className="text-sm">
                  {activeFilter === 'unread'
                    ? "You're all caught up!"
                    : 'No notifications in this category'}
                </p>
              </div>
            </div>
          </div>
        ) : (
          // Notification list
          <div className="divide-y divide-surface-800">
            {/* Select all header */}
            {filteredNotifications.length > 0 && (
              <div className="px-6 py-3 bg-surface-800/20 flex items-center gap-3">
                <input
                  type="checkbox"
                  checked={selectedIds.size === filteredNotifications.length}
                  onChange={toggleSelectAll}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 cursor-pointer accent-primary-500"
                />
                <span className="text-xs text-text-400">
                  {selectedIds.size > 0 ? `${selectedIds.size} selected` : 'Select notifications'}
                </span>
              </div>
            )}

            {/* Notifications */}
            {filteredNotifications.map((notification) => (
              <div
                key={notification.id}
                onClick={() => handleNotificationClick(notification)}
                className={`px-6 py-4 hover:bg-surface-800/50 cursor-pointer transition-colors flex items-start gap-4 group ${
                  !notification.read ? 'bg-primary-500/5' : ''
                }`}
              >
                {/* Checkbox */}
                <input
                  type="checkbox"
                  checked={selectedIds.has(notification.id)}
                  onChange={(e) => {
                    e.stopPropagation();
                    toggleSelection(notification.id);
                  }}
                  className="w-4 h-4 rounded border-surface-600 bg-surface-700 cursor-pointer accent-primary-500 mt-1 flex-shrink-0"
                />

                {/* Icon */}
                <div className="flex-shrink-0 mt-1">
                  {getNotificationIcon(notification.type)}
                </div>

                {/* Content */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-start justify-between gap-2 mb-1">
                    <h3 className="font-medium text-text-100 leading-tight">
                      {notification.title}
                    </h3>
                    {!notification.read && (
                      <div className="w-2 h-2 rounded-full bg-primary-500 flex-shrink-0" />
                    )}
                  </div>
                  <p className="text-sm text-text-400 line-clamp-2 mb-2">
                    {notification.body}
                  </p>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-text-500">
                      {getRelativeTime(notification.created_at)}
                    </span>
                    {notification.action_url && (
                      <ArrowRight
                        size={14}
                        className="text-text-500 opacity-0 group-hover:opacity-100 transition-opacity"
                      />
                    )}
                  </div>
                </div>

                {/* Delete button */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteNotification(notification.id);
                  }}
                  className="flex-shrink-0 p-2 hover:bg-surface-700 rounded transition-colors text-text-500 hover:text-red-400"
                  title="Delete notification"
                >
                  <Trash size={16} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Footer with bulk actions */}
      {filteredNotifications.length > 0 && unreadCount > 0 && selectedIds.size === 0 && (
        <div className="border-t border-surface-800 px-6 py-3 bg-surface-800/40 flex items-center justify-between">
          <span className="text-sm text-text-400">
            {unreadCount} unread in {activeFilter === 'all' ? 'all categories' : activeFilter}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => markAllRead()}
              className="px-4 py-2 text-sm bg-primary-600 hover:bg-primary-700 text-white rounded transition-colors"
            >
              Mark all as read
            </button>
            <button
              onClick={() => {
                if (window.confirm('Delete all notifications?')) {
                  deleteAllNotifications();
                }
              }}
              className="px-4 py-2 text-sm bg-surface-700 hover:bg-surface-600 text-text-300 rounded transition-colors"
            >
              Clear all
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default NotificationCenter;