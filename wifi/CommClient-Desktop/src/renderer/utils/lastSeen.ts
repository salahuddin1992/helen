/**
 * Format an ISO timestamp as "X minutes/hours/days ago".
 *
 * Centralised so every UI surface (Presence component, contact tooltip,
 * call screen status line, etc.) renders the same phrasing.
 */

export function formatLastSeen(iso: string | null | undefined): string {
    if (!iso) return 'a while ago';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return 'a while ago';

    const diffSec = Math.max(1, Math.floor((Date.now() - t) / 1000));

    if (diffSec < 60)        return 'just now';
    if (diffSec < 3_600)     return `${Math.floor(diffSec / 60)} min ago`;
    if (diffSec < 86_400)    return `${Math.floor(diffSec / 3_600)} h ago`;
    if (diffSec < 604_800)   return `${Math.floor(diffSec / 86_400)} d ago`;

    // Beyond a week: show the date.
    const d = new Date(t);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
