/**
 * Call history page — shows past calls with duration and type.
 * Features: type icons, status colors, relative time with hover, pagination
 */
import React, { useEffect, useState } from 'react';
import { Phone, Video, Monitor, PhoneIncoming, PhoneOutgoing, PhoneMissed, RefreshCw, Trash2 } from 'lucide-react';
import { api } from '@/services/api.client';
import { t } from '@/i18n';

interface CallLog {
  id: string;
  initiator_id: string;
  call_type: string;
  routing: string;
  status: string;
  duration_seconds: number | null;
  participant_count: number;
  created_at: string;
}

const ITEMS_PER_PAGE = 20;

export const CallHistoryPage: React.FC = () => {
  const [calls, setCalls] = useState<CallLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  useEffect(() => {
    fetchCallHistory();
  }, []);

  const fetchCallHistory = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getCallHistory();
      setCalls(data.calls || []);
      setPage(0);
    } catch (err) {
      setError('Failed to load call history');
      console.error('[CallHistoryPage] Error:', err);
    } finally {
      setLoading(false);
    }
  };

  // Optimistic delete: remove from view immediately so the row collapses,
  // then call the server. On failure we re-fetch to restore the row.
  const handleDeleteOne = async (id: string) => {
    if (!window.confirm('Delete this call from history?')) return;
    setCalls((prev) => prev.filter((c) => c.id !== id));
    try {
      await api.deleteCall(id);
    } catch (err) {
      console.error('[CallHistoryPage] deleteCall failed:', err);
      setError('Failed to delete — reloading');
      await fetchCallHistory();
    }
  };

  const handleClearAll = async () => {
    if (!calls.length) return;
    if (!window.confirm(`Clear all ${calls.length} calls from history? This cannot be undone.`)) return;
    const before = calls;
    setCalls([]);
    try {
      await api.clearCallHistory();
    } catch (err) {
      console.error('[CallHistoryPage] clearCallHistory failed:', err);
      setError('Failed to clear — reloading');
      setCalls(before);
    }
  };

  const formatDuration = (seconds: number | null): string => {
    if (!seconds) return '--:--';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const formatTimeAgo = (iso: string): string => {
    const now = new Date();
    const then = new Date(iso);
    const diffMs = now.getTime() - then.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return t('common.timeAgo.justNow');
    if (diffMins < 60) return `${diffMins} ${t('common.timeAgo.minutesAgo')}`;
    if (diffHours < 24) return `${diffHours} ${t('common.timeAgo.hoursAgo')}`;
    if (diffDays < 30) return `${diffDays} ${t('common.timeAgo.daysAgo')}`;

    return then.toLocaleDateString();
  };

  const formatFullDateTime = (iso: string): string => {
    const d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  const TypeIcon = ({ type }: { type: string }) => {
    if (type === 'video') return <Video size={16} className="text-blue-400" />;
    if (type === 'screen_share') return <Monitor size={16} className="text-purple-400" />;
    return <Phone size={16} className="text-green-400" />;
  };

  const StatusIcon = ({ status }: { status: string }) => {
    if (status === 'missed') return <PhoneMissed size={14} className="text-red-400" />;
    if (status === 'rejected') return <PhoneMissed size={14} className="text-yellow-400" />;
    if (status === 'completed') return <PhoneIncoming size={14} className="text-green-400" />;
    return <PhoneOutgoing size={14} className="text-gray-400" />;
  };

  const getStatusColor = (status: string): string => {
    if (status === 'missed') return 'bg-red-500/10 border-red-500/30';
    if (status === 'rejected') return 'bg-yellow-500/10 border-yellow-500/30';
    if (status === 'completed') return 'bg-green-500/10 border-green-500/30';
    return 'bg-surface-700/50 border-surface-600';
  };

  const getStatusBadgeColor = (status: string): string => {
    if (status === 'missed') return 'bg-red-500/20 text-red-200';
    if (status === 'rejected') return 'bg-yellow-500/20 text-yellow-200';
    if (status === 'completed') return 'bg-green-500/20 text-green-200';
    return 'bg-surface-700 text-surface-300';
  };

  const paginatedCalls = calls.slice(page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE);
  const totalPages = Math.ceil(calls.length / ITEMS_PER_PAGE);

  const skeletonRows = (
    <>
      {[...Array(5)].map((_, i) => (
        <div key={`skeleton-${i}`} className="flex items-center gap-4 p-4 rounded-lg bg-surface-800 animate-pulse">
          <div className="w-10 h-10 rounded-full bg-surface-700" />
          <div className="flex-1">
            <div className="h-3 bg-surface-700 rounded w-24 mb-2" />
            <div className="h-2 bg-surface-700 rounded w-48" />
          </div>
          <div className="w-16 text-right">
            <div className="h-3 bg-surface-700 rounded w-12 ml-auto mb-2" />
            <div className="h-2 bg-surface-700 rounded w-12 ml-auto" />
          </div>
        </div>
      ))}
    </>
  );

  if (loading && calls.length === 0) {
    return (
      <div className="flex-1 flex flex-col h-full bg-surface-900">
        <div className="p-6 border-b border-surface-800">
          <h1 className="text-xl font-semibold">{t('nav.calls')}</h1>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          <div className="space-y-2 max-w-4xl mx-auto">{skeletonRows}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col h-full bg-surface-900">
      {/* Header */}
      <div className="p-6 border-b border-surface-800 flex items-center justify-between">
        <h1 className="text-xl font-semibold">{t('nav.calls')}</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={handleClearAll}
            disabled={loading || calls.length === 0}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm bg-red-500/10 text-red-300 hover:bg-red-500/20 disabled:opacity-40 transition-colors"
            title="Clear all call history"
          >
            <Trash2 size={14} />
            Clear all
          </button>
          <button
            onClick={fetchCallHistory}
            disabled={loading}
            className="p-2 rounded-lg hover:bg-surface-800 transition-colors disabled:opacity-50"
            title="Refresh"
          >
            <RefreshCw size={20} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-4 mt-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-200 text-sm flex items-center justify-between">
          <span>{error}</span>
          <button
            onClick={fetchCallHistory}
            className="ml-2 px-2 py-1 rounded bg-red-500/20 hover:bg-red-500/30 transition-colors text-xs"
          >
            {t('common.retry')}
          </button>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {calls.length === 0 ? (
          <div className="text-center text-surface-300 mt-20">
            <Phone size={48} className="mx-auto mb-4 opacity-30" />
            <p className="text-sm">{t('nav.calls')} history is empty</p>
          </div>
        ) : (
          <div className="space-y-2 max-w-4xl mx-auto">
            {/* Table-like header */}
            <div className="px-4 py-3 grid grid-cols-[auto_1fr_auto_auto_auto] gap-4 text-xs text-surface-400 font-semibold uppercase tracking-wider">
              <div></div>
              <div>Details</div>
              <div className="text-right">Duration</div>
              <div className="text-right">Status</div>
              <div></div>
            </div>

            {/* Call rows */}
            {paginatedCalls.map((call) => (
              <div
                key={call.id}
                className={`group grid grid-cols-[auto_1fr_auto_auto_auto] gap-4 items-center p-4 rounded-lg border transition-all hover:bg-surface-800/50 ${getStatusColor(call.status)}`}
              >
                {/* Type icon */}
                <div className="w-10 h-10 rounded-full bg-surface-700/50 flex items-center justify-center flex-shrink-0">
                  <TypeIcon type={call.call_type} />
                </div>

                {/* Call details */}
                <div className="min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium capitalize">{call.call_type} Call</span>
                    <span className="text-xs px-2 py-0.5 rounded bg-surface-600/50 text-surface-300 uppercase">
                      {call.routing}
                    </span>
                  </div>
                  <div
                    className="text-xs text-surface-400 cursor-help hover:text-surface-300"
                    title={formatFullDateTime(call.created_at)}
                  >
                    {formatTimeAgo(call.created_at)}
                  </div>
                  <div className="text-xs text-surface-400 mt-0.5">
                    {call.participant_count} {call.participant_count === 1 ? 'participant' : 'participants'}
                  </div>
                </div>

                {/* Duration */}
                <div className="text-right">
                  <div className="text-sm font-mono font-semibold text-surface-100">
                    {formatDuration(call.duration_seconds)}
                  </div>
                </div>

                {/* Status badge */}
                <div className="text-right flex-shrink-0">
                  <span
                    className={`inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium ${getStatusBadgeColor(call.status)}`}
                  >
                    <StatusIcon status={call.status} />
                    <span className="capitalize">{call.status}</span>
                  </span>
                </div>

                {/* Per-row delete (visible on hover) */}
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteOne(call.id); }}
                  className="opacity-0 group-hover:opacity-100 p-2 rounded-lg text-surface-400 hover:text-red-400 hover:bg-red-500/10 transition-all"
                  title="Delete this call"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Pagination footer */}
      {calls.length > ITEMS_PER_PAGE && (
        <div className="border-t border-surface-800 p-4 flex items-center justify-between bg-surface-950">
          <div className="text-xs text-surface-400">
            Showing {Math.min((page + 1) * ITEMS_PER_PAGE, calls.length)} of {calls.length} calls
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setPage(Math.max(0, page - 1))}
              disabled={page === 0}
              className="px-3 py-1 rounded-lg bg-surface-800 hover:bg-surface-700 disabled:opacity-50 text-sm transition-colors"
            >
              ← Previous
            </button>
            <div className="flex items-center gap-1 px-2">
              <span className="text-xs text-surface-400">
                Page {page + 1} of {totalPages}
              </span>
            </div>
            <button
              onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
              disabled={page >= totalPages - 1}
              className="px-3 py-1 rounded-lg bg-surface-800 hover:bg-surface-700 disabled:opacity-50 text-sm transition-colors"
            >
              Next →
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
