import React, { useState } from 'react';
import { AlertCircle, Pause, Play, RefreshCw, Volume2, VolumeX, ChevronDown } from 'lucide-react';

interface ScreenShareControlsProps {
  isSharing: boolean;
  isPaused: boolean;
  hasAudio: boolean;
  isDualStream: boolean;
  currentPreset: string;
  duration: number;
  onStartShare: () => void;
  onStopShare: () => void;
  onPause: () => void;
  onResume: () => void;
  onSwitchSource: () => void;
  onToggleAudio: () => void;
  onSetPreset: (preset: string) => void;
}

const QUALITY_PRESETS = [
  { key: 'lan-max', label: 'LAN (Max Quality)' },
  { key: '1080p', label: '1080p (Detail)' },
  { key: '1080p-detail', label: '1080p (Ultra Detail)' },
  { key: '720p', label: '720p (Standard)' },
  { key: 'motion', label: 'Motion (Low-latency)' },
];

const ScreenShareControls: React.FC<ScreenShareControlsProps> = ({
  isSharing,
  isPaused,
  hasAudio,
  isDualStream,
  currentPreset,
  duration,
  onStartShare,
  onStopShare,
  onPause,
  onResume,
  onSwitchSource,
  onToggleAudio,
  onSetPreset,
}) => {
  const [showPresetMenu, setShowPresetMenu] = useState(false);

  const formatDuration = (secs: number): string => {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  const currentPresetLabel = QUALITY_PRESETS.find(p => p.key === currentPreset)?.label || currentPreset;

  return (
    <div className="flex items-center gap-2 bg-slate-900 rounded-lg p-2 border border-slate-700">
      {/* Active Indicator */}
      {isSharing && (
        <div className="flex items-center gap-1.5 px-2 py-1 bg-red-600/20 rounded border border-red-600/40">
          <div className="w-2 h-2 bg-red-500 rounded-full animate-pulse" />
          <span className="text-xs font-medium text-red-400">SHARING</span>
        </div>
      )}

      {!isSharing ? (
        /* Start sharing button */
        <button
          onClick={onStartShare}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium rounded transition-colors"
          title="Start screen share"
        >
          <AlertCircle size={16} />
          <span className="hidden sm:inline">Share</span>
        </button>
      ) : (
        /* Presenter controls */
        <>
          {/* Duration Timer */}
          <div className="flex items-center gap-1 px-2 py-1 text-xs text-text-400 font-mono">
            {formatDuration(duration)}
          </div>

          {/* Pause / Resume */}
          <button
            onClick={isPaused ? onResume : onPause}
            className="p-1.5 text-text-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
            title={isPaused ? 'Resume' : 'Pause'}
          >
            {isPaused ? (
              <Play size={16} />
            ) : (
              <Pause size={16} />
            )}
          </button>

          {/* Switch Source */}
          <button
            onClick={onSwitchSource}
            className="p-1.5 text-text-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
            title="Switch source"
          >
            <RefreshCw size={16} />
          </button>

          {/* Audio Toggle */}
          <button
            onClick={onToggleAudio}
            className={`p-1.5 rounded transition-colors ${
              hasAudio
                ? 'text-green-400 hover:text-green-300 hover:bg-slate-700'
                : 'text-text-500 hover:text-text-300 hover:bg-slate-700'
            }`}
            title={hasAudio ? 'Audio enabled' : 'Audio disabled'}
          >
            {hasAudio ? (
              <Volume2 size={16} />
            ) : (
              <VolumeX size={16} />
            )}
          </button>

          {/* Quality Preset Dropdown */}
          <div className="relative">
            <button
              onClick={() => setShowPresetMenu(!showPresetMenu)}
              className="flex items-center gap-1 px-2 py-1 text-xs text-text-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
              title="Quality preset"
            >
              <span className="hidden sm:inline">{currentPresetLabel}</span>
              <ChevronDown size={14} />
            </button>

            {showPresetMenu && (
              <div className="absolute bottom-full right-0 mb-1 bg-slate-800 border border-slate-700 rounded-lg shadow-xl py-1 min-w-[160px]">
                {QUALITY_PRESETS.map((preset) => (
                  <button
                    key={preset.key}
                    onClick={() => {
                      onSetPreset(preset.key);
                      setShowPresetMenu(false);
                    }}
                    className={`w-full text-left px-3 py-1.5 text-xs hover:bg-slate-700 transition-colors ${
                      currentPreset === preset.key
                        ? 'text-blue-400 font-medium'
                        : 'text-text-300'
                    }`}
                  >
                    {currentPreset === preset.key && '✓ '}
                    {preset.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Stop Sharing */}
          <button
            onClick={onStopShare}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-600/80 hover:bg-red-700 text-white text-sm font-medium rounded transition-colors ml-1"
            title="Stop screen share"
          >
            <AlertCircle size={16} />
            <span className="hidden sm:inline">Stop</span>
          </button>
        </>
      )}
    </div>
  );
};

export default ScreenShareControls;
