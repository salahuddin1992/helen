import React from 'react';

interface UnreadBadgeProps {
  count: number;
  className?: string;
  pulsing?: boolean;
}

export const UnreadBadge: React.FC<UnreadBadgeProps> = ({ count, className = '', pulsing = false }) => {
  if (count === 0) {
    return null;
  }

  const displayCount = count > 99 ? '99+' : count.toString();

  return (
    <div
      className={`absolute top-0 right-0 bg-red-500 text-white text-xs font-bold rounded-full min-w-5 h-5 flex items-center justify-center transform translate-x-1 -translate-y-1 ${pulsing ? 'animate-pulse' : ''} ${className}`}
      aria-label={`${count} unread messages`}
      role="status"
    >
      {displayCount}
    </div>
  );
};
