import React from 'react';
import type { UserStatus } from '@/types';
import AuthorizedImage from './AuthorizedImage';

interface AvatarProps {
  src?: string | null;
  name: string;
  status?: UserStatus;
  size?: 'sm' | 'md' | 'lg';
}

export const Avatar: React.FC<AvatarProps> = ({
  src,
  name,
  status = 'offline',
  size = 'md',
}) => {
  const sizeClasses = {
    sm: 'w-8 h-8 text-xs',
    md: 'w-10 h-10 text-sm',
    lg: 'w-16 h-16 text-lg',
  };

  const statusDotSizes = {
    sm: 'w-2 h-2',
    md: 'w-2.5 h-2.5',
    lg: 'w-3 h-3',
  };

  const statusColors: Record<UserStatus, string> = {
    online: 'bg-green-500',
    offline: 'bg-slate-400',
    away: 'bg-amber-400',
    busy: 'bg-red-500',
    dnd: 'bg-red-500',
    in_call: 'bg-blue-500',
  };

  const getInitials = (displayName: string): string => {
    return displayName
      .split(' ')
      .map((word) => word[0])
      .join('')
      .toUpperCase()
      .slice(0, 2);
  };

  const fallback = (
    <div
      className={`${sizeClasses[size]} rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center font-semibold text-white flex-shrink-0`}
    >
      {getInitials(name)}
    </div>
  );

  // Server-hosted images (e.g. profile photos at /api/users/.../image) require
  // an auth header — fetch them through AuthorizedImage. External URLs can use
  // a plain <img>.
  const needsAuth = src && src.startsWith('/api/');

  return (
    <div className="relative inline-block">
      {src ? (
        needsAuth ? (
          <AuthorizedImage
            path={src}
            alt={name}
            className={`${sizeClasses[size]} rounded-full object-cover bg-surface-800 flex-shrink-0`}
            fallback={fallback}
          />
        ) : (
          <img
            src={src}
            alt={name}
            className={`${sizeClasses[size]} rounded-full object-cover bg-surface-800 flex-shrink-0`}
          />
        )
      ) : (
        fallback
      )}

      {/* Status indicator dot — pulses with a soft ping when the
          peer is online so the operator can spot reachable contacts
          at a glance, even when scanning a long channel/contact
          list. Other states render the dot statically. */}
      <span
        className={`${statusDotSizes[size]} rounded-full absolute bottom-0 right-0 ring-2 ring-surface-900 inline-flex items-center justify-center`}
      >
        {status === 'online' && (
          <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
        )}
        <span
          className={`relative w-full h-full rounded-full ${statusColors[status]}`}
        />
      </span>
    </div>
  );
};
