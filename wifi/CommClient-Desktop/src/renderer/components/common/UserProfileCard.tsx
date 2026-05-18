import React, { useEffect, useRef } from 'react';
import { X, MessageSquare, Phone, Video, Ban } from 'lucide-react';
import type { User } from '@/types';
import { Avatar } from './Avatar';
import { StatusBadge } from './StatusBadge';
import { Handle } from './Handle';

interface UserProfileCardProps {
  user: User;
  isOpen: boolean;
  onClose: () => void;
  onAction: (action: 'message' | 'audio_call' | 'video_call' | 'block', userId: string) => void;
  position?: { top: number; left: number };
}

export const UserProfileCard: React.FC<UserProfileCardProps> = ({
  user,
  isOpen,
  onClose,
  onAction,
  position = { top: 0, left: 0 },
}) => {
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (cardRef.current && !cardRef.current.contains(e.target as Node)) {
        onClose();
      }
    };

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      document.addEventListener('keydown', handleEscape);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isOpen, onClose]);

  if (!isOpen) {
    return null;
  }

  const formatLastSeen = (lastSeen: string | undefined): string => {
    if (!lastSeen) {
      return 'Unknown';
    }

    const date = new Date(lastSeen);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return 'Now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString();
  };

  return (
    <div
      ref={cardRef}
      className="fixed z-40 bg-surface-800 border border-surface-700 rounded-xl shadow-2xl w-80 p-6"
      style={{
        top: `${position.top}px`,
        left: `${position.left}px`,
      }}
      role="dialog"
      aria-label={`Profile of ${user.display_name}`}
    >
      {/* Close button */}
      <button
        onClick={onClose}
        className="absolute top-4 right-4 text-surface-400 hover:text-white transition-colors p-1 -m-1"
        aria-label="Close profile"
      >
        <X size={18} />
      </button>

      {/* Avatar */}
      <div className="flex justify-center mb-4">
        <Avatar src={user.avatar_url} name={user.display_name} status={user.status} size="lg" />
      </div>

      {/* User info */}
      <div className="text-center mb-4">
        <h2 className="text-lg font-semibold text-white">{user.display_name}</h2>
        <Handle user={user} className="text-sm text-surface-400 mt-1 block" />
      </div>

      {/* Status */}
      <div className="flex justify-center mb-4">
        <StatusBadge status={user.status} />
      </div>

      {/* Bio */}
      {user.bio && <p className="text-sm text-surface-300 text-center mb-4 line-clamp-2">{user.bio}</p>}

      {/* Last seen */}
      {user.status === 'offline' && user.last_seen && (
        <p className="text-xs text-surface-500 text-center mb-4">Last seen {formatLastSeen(user.last_seen)}</p>
      )}

      {/* Action buttons */}
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => onAction('message', user.id)}
          className="flex items-center justify-center gap-2 bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium py-2 rounded transition-colors"
          aria-label="Send message"
        >
          <MessageSquare size={16} />
          Message
        </button>

        <button
          onClick={() => onAction('audio_call', user.id)}
          className="flex items-center justify-center gap-2 bg-surface-700 hover:bg-surface-600 text-white text-sm font-medium py-2 rounded transition-colors"
          aria-label="Audio call"
        >
          <Phone size={16} />
          Call
        </button>

        <button
          onClick={() => onAction('video_call', user.id)}
          className="flex items-center justify-center gap-2 bg-surface-700 hover:bg-surface-600 text-white text-sm font-medium py-2 rounded transition-colors"
          aria-label="Video call"
        >
          <Video size={16} />
          Video
        </button>

        <button
          onClick={() => onAction('block', user.id)}
          className="flex items-center justify-center gap-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 text-sm font-medium py-2 rounded transition-colors"
          aria-label="Block user"
        >
          <Ban size={16} />
          Block
        </button>
      </div>
    </div>
  );
};
