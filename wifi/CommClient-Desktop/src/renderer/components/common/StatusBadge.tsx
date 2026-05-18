import React from 'react';
import type { UserStatus } from '@/types';
import { t } from '@/i18n';

interface StatusBadgeProps {
  status: UserStatus;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({ status }) => {
  const statusColors: Record<UserStatus, { dot: string; text: string }> = {
    online: {
      dot: 'bg-green-500',
      text: 'text-green-400',
    },
    offline: {
      dot: 'bg-slate-400',
      text: 'text-slate-400',
    },
    away: {
      dot: 'bg-amber-400',
      text: 'text-amber-400',
    },
    busy: {
      dot: 'bg-red-500',
      text: 'text-red-400',
    },
    dnd: {
      dot: 'bg-red-500',
      text: 'text-red-400',
    },
    in_call: {
      dot: 'bg-blue-500',
      text: 'text-blue-400',
    },
  };

  const colors = statusColors[status];
  const statusKey = `status.${status}`;

  return (
    <div className="flex items-center gap-2">
      <div className={`${colors.dot} w-2 h-2 rounded-full`} />
      <span className={`text-xs font-medium ${colors.text}`}>
        {t(statusKey)}
      </span>
    </div>
  );
};
