/**
 * Handle — renders a user's @-handle.
 *
 * Prefers the 64-char `share_code` (mathematically unique, alphanumeric)
 * over the short `username`. Truncates to `@<first8>…<last4>` for layout
 * but exposes the full string via the `title` attribute, so hover and
 * long-press still reveal the full code.
 *
 * Use this everywhere the desktop client previously hard-coded
 * `@{user.username}`. See `formatHandle` for the underlying logic.
 */
import React from 'react';

export interface HandleUser {
    username?: string | null;
    share_code?: string | null;
    code?: string | null;
}

export function handleFull(u: HandleUser | null | undefined): string {
    if (!u) return '';
    const code = u.share_code || u.code || '';
    if (code) return '@' + code;
    return '@' + (u.username || '');
}

export function handleShort(u: HandleUser | null | undefined): string {
    const full = handleFull(u);
    const body = full.slice(1);
    if (body.length <= 14) return full;
    return '@' + body.slice(0, 8) + '…' + body.slice(-4);
}

interface HandleProps {
    user: HandleUser | null | undefined;
    className?: string;
    /** Force `username`-only — useful in places where we *only* have a username. */
    short?: boolean;
}

export const Handle: React.FC<HandleProps> = ({ user, className, short = true }) => {
    const display = short ? handleShort(user) : handleFull(user);
    const full = handleFull(user);
    return (
        <span className={className} title={full}>
            {display}
        </span>
    );
};
