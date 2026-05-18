import React, { useMemo } from 'react';
import { Users, Clock, Share2, AlertCircle, X, ChevronDown, Star } from 'lucide-react';

interface PresenterPanelProps {
  callId: string;
  currentPresenter: { userId: string; displayName: string; startedAt: number } | null;
  queue: Array<{ userId: string; displayName: string; position: number }>;
  localRequestStatus: 'idle' | 'requesting' | 'granted' | 'denied' | 'queued';
  queuePosition: number;
  isLocalPresenter: boolean;
  viewerCount: number;
  onRequestPresenter: () => void;
  onReleasePresenter: () => void;
  onCancelRequest: () => void;
  onForceStop?: (userId: string) => void;
  onHandoff?: (userId: string) => void;
}

const PresenterPanel: React.FC<PresenterPanelProps> = ({
  callId,
  currentPresenter,
  queue,
  localRequestStatus,
  queuePosition,
  isLocalPresenter,
  viewerCount,
  onRequestPresenter,
  onReleasePresenter,
  onCancelRequest,
  onForceStop,
  onHandoff,
}) => {
  const presenterDuration = useMemo(() => {
    if (!currentPresenter) return 0;
    return Math.floor((Date.now() - currentPresenter.startedAt) / 1000);
  }, [currentPresenter]);

  const formatDuration = (secs: number): string => {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  const estimatedWaitTime = (position: number): string => {
    // Assume avg 5 min per presenter
    const mins = position * 5;
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    const remainMins = mins % 60;
    return `${hours}h ${remainMins}m`;
  };

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-700 overflow-hidden flex flex-col max-h-96">
      {/* Current Presenter Section */}
      {currentPresenter ? (
        <div className="bg-gradient-to-r from-blue-600/20 to-blue-600/5 border-b border-slate-700 p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3 flex-1">
              <div className="w-10 h-10 rounded-full bg-blue-500/30 flex items-center justify-center">
                <Share2 size={18} className="text-blue-400" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-text-100 truncate">
                  {isLocalPresenter ? 'You' : currentPresenter.displayName}
                </p>
                <p className="text-xs text-text-400">Currently presenting</p>
              </div>
            </div>
            {isLocalPresenter && (
              <button
                onClick={onReleasePresenter}
                className="ml-2 px-3 py-1.5 bg-red-600/80 hover:bg-red-700 text-white text-xs font-medium rounded-lg transition-colors"
              >
                Release
              </button>
            )}
            {onForceStop && !isLocalPresenter && (
              <button
                onClick={() => onForceStop(currentPresenter.userId)}
                className="ml-2 px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-text-100 text-xs font-medium rounded-lg transition-colors"
              >
                Stop
              </button>
            )}
          </div>

          {/* Timer */}
          <div className="flex items-center gap-2 text-xs text-text-400">
            <Clock size={14} className="text-blue-400" />
            <span className="font-mono">{formatDuration(presenterDuration)}</span>
          </div>
        </div>
      ) : (
        <div className="bg-slate-800/50 border-b border-slate-700 p-4">
          <p className="text-sm text-text-400">No one presenting</p>
        </div>
      )}

      {/* Request Status Section */}
      {!isLocalPresenter && (
        <div className="bg-slate-800/30 border-b border-slate-700 p-3">
          {localRequestStatus === 'idle' ? (
            <button
              onClick={onRequestPresenter}
              className="w-full px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
            >
              <Share2 size={16} />
              Request to Present
            </button>
          ) : localRequestStatus === 'requesting' ? (
            <div className="flex items-center gap-2 text-yellow-400 text-sm">
              <div className="w-2 h-2 bg-yellow-400 rounded-full animate-pulse" />
              <span>Requesting...</span>
            </div>
          ) : localRequestStatus === 'denied' ? (
            <div className="flex items-center gap-2 text-red-400 text-sm">
              <X size={14} />
              <span>Request denied</span>
              <button
                onClick={onRequestPresenter}
                className="ml-auto text-xs underline hover:no-underline"
              >
                Try again
              </button>
            </div>
          ) : localRequestStatus === 'queued' ? (
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-blue-400 text-sm">
                <Star size={14} className="animate-pulse" />
                <span>In queue</span>
              </div>
              <button
                onClick={onCancelRequest}
                className="text-xs text-red-400 hover:text-red-300 transition-colors"
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-green-400 text-sm">
              <div className="w-2 h-2 bg-green-400 rounded-full" />
              <span>Request approved</span>
            </div>
          )}
        </div>
      )}

      {/* Queue Section */}
      {queue.length > 0 && (
        <div className="flex-1 overflow-y-auto border-b border-slate-700">
          <div className="p-3 bg-slate-800/20">
            <div className="flex items-center gap-2 mb-2">
              <Users size={14} className="text-slate-400" />
              <p className="text-xs font-semibold text-text-300">Queue ({queue.length})</p>
            </div>

            <div className="space-y-2">
              {queue.map((person, idx) => (
                <div
                  key={person.userId}
                  className={`flex items-center gap-2 p-2 rounded-lg transition-colors ${
                    queuePosition === idx + 1
                      ? 'bg-blue-600/20 border border-blue-500/40'
                      : 'bg-slate-700/30'
                  }`}
                >
                  <div className="flex-shrink-0 w-6 h-6 rounded-full bg-slate-600 flex items-center justify-center text-xs font-semibold text-text-100">
                    {idx + 1}
                  </div>

                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-text-200 truncate">
                      {person.displayName}
                    </p>
                    {idx === 0 && (
                      <p className="text-xs text-text-400">Next up</p>
                    )}
                  </div>

                  <div className="flex-shrink-0 text-xs text-text-400">
                    ~{estimatedWaitTime(idx)}
                  </div>

                  {/* Handoff button */}
                  {isLocalPresenter && onHandoff && (
                    <button
                      onClick={() => onHandoff(person.userId)}
                      className="flex-shrink-0 p-1 text-slate-400 hover:text-blue-400 transition-colors"
                      title="Hand off to this person"
                    >
                      <ChevronDown size={14} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Viewers Section */}
      {viewerCount > 0 && (
        <div className="bg-slate-800/20 border-t border-slate-700 p-3">
          <div className="flex items-center gap-2 text-xs text-text-400">
            <Users size={14} className="text-slate-500" />
            <span>{viewerCount} {viewerCount === 1 ? 'viewer' : 'viewers'}</span>
          </div>
        </div>
      )}
    </div>
  );
};

export default PresenterPanel;
