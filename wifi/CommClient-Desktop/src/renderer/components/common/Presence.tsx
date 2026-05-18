/**
 * Presence — unified online/offline indicator.
 *
 * Resolves the visual contract everywhere a user identity is rendered:
 *   • Online → small green dot + "Online" (or no caption when compact).
 *   • Offline → small red dot + "Last seen X minutes/hours/days ago".
 *
 * The component reads live presence from the contacts store, but accepts
 * `lastSeenAt` as a prop so non-store callers (search results, member
 * lists from API responses) can still render correctly without
 * subscribing.
 */

import React from 'react';
import { useContactsStore } from '@/stores/contacts.store';
import { formatLastSeen } from '@/utils/lastSeen';

export interface PresenceProps {
    /** Stable user id to look up in the contacts store. */
    userId: string;
    /** ISO timestamp; used to format "last seen X ago" when offline. */
    lastSeenAt?: string | null;
    /** When true, render only the dot — no caption. */
    compact?: boolean;
    /** Override the resolved status (rare; for previews / fixtures). */
    overrideStatus?: 'online' | 'offline';
    className?: string;
}

export const Presence: React.FC<PresenceProps> = ({
    userId,
    lastSeenAt,
    compact = false,
    overrideStatus,
    className,
}) => {
    const status = useContactsStore((s) =>
        overrideStatus ?? (s.getUserStatus(userId) === 'online' ? 'online' : 'offline'),
    );
    const isOnline = status === 'online';

    const dotColor = isOnline ? '#22c55e' /* green-500 */ : '#ef4444' /* red-500 */;
    const dotShadow = isOnline
        ? '0 0 0 2px rgba(34, 197, 94, 0.18)'
        : '0 0 0 2px rgba(239, 68, 68, 0.16)';

    const caption = isOnline
        ? 'Online'
        : `Last seen ${formatLastSeen(lastSeenAt)}`;

    return (
        <span
            className={className}
            style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                fontSize: 12, color: 'var(--app-muted, #94a3b8)',
                whiteSpace: 'nowrap',
            }}
            aria-label={caption}
            title={caption}
        >
            <span
                aria-hidden
                style={{
                    width: 8, height: 8, borderRadius: '50%',
                    background: dotColor, boxShadow: dotShadow,
                    flexShrink: 0,
                }}
            />
            {!compact && <span>{caption}</span>}
        </span>
    );
};
