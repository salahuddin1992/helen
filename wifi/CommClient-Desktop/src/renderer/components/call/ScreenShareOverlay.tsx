/**
 * ScreenShareOverlay — full-screen presenter view with PiP camera and controls.
 *
 * Renders:
 *   - Full-area presenter screen video
 *   - PiP (picture-in-picture) self-camera thumbnail (draggable)
 *   - Presenter info bar (who's sharing, duration)
 *   - Screen share controls (stop, pause, switch source, quality)
 *   - Viewer controls (exit fullscreen, minimize)
 *   - Group presenter queue indicator
 */

import React, { useEffect, useRef, useState, useCallback } from 'react';
import { MoreVertical, Maximize2, Minimize2, Users, AlertCircle, Pause, Play } from 'lucide-react';

// ── Types ───────────────────────────────────────────

interface PresenterInfo {
  userId: string;
  displayName: string;
  isLocal: boolean;
}

interface ScreenShareOverlayProps {
  /** Screen share stream to display */
  screenStream: MediaStream | null;
  /** Local camera stream for PiP */
  cameraStream: MediaStream | null;
  /** Who is presenting */
  presenter: PresenterInfo | null;
  /** Is the local user the presenter */
  isLocalPresenter: boolean;
  /** Current sharing duration in seconds */
  duration: number;
  /** Current quality preset name */
  qualityPreset: string;
  /** Available quality presets */
  qualityPresets: Array<{ key: string; label: string }>;
  /** Queue position (0 = not queued) */
  queuePosition: number;
  /** Queue length */
  queueLength: number;
  /** Whether sharing is paused */
  isPaused: boolean;
  /** Number of viewers */
  viewerCount?: number;
  /** Current bitrate in Mbps */
  currentBitrate?: number;
  /** Whether screen audio is captured */
  hasAudio?: boolean;
  /** Callbacks */
  onStop: () => void;
  onPause: () => void;
  onResume: () => void;
  onSwitchSource: () => void;
  onChangeQuality: (preset: string) => void;
  onRequestPresenter: () => void;
  onCancelRequest: () => void;
  onExitFullscreen: () => void;
}

// ── Component ───────────────────────────────────────

const ScreenShareOverlay: React.FC<ScreenShareOverlayProps> = ({
  screenStream,
  cameraStream,
  presenter,
  isLocalPresenter,
  duration,
  qualityPreset,
  qualityPresets,
  queuePosition,
  queueLength,
  isPaused,
  viewerCount = 0,
  currentBitrate = 0,
  hasAudio = false,
  onStop,
  onPause,
  onResume,
  onSwitchSource,
  onChangeQuality,
  onRequestPresenter,
  onCancelRequest,
  onExitFullscreen,
}) => {
  const screenVideoRef = useRef<HTMLVideoElement>(null);
  const cameraVideoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [showControls, setShowControls] = useState(true);
  const [showQualityMenu, setShowQualityMenu] = useState(false);
  const [pipPosition, setPipPosition] = useState({ x: 20, y: 20 });
  const [isDragging, setIsDragging] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showToolbar, setShowToolbar] = useState(true);
  const [cursorHidden, setCursorHidden] = useState(false);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursorHideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Attach screen stream to video element
  useEffect(() => {
    if (screenVideoRef.current && screenStream) {
      screenVideoRef.current.srcObject = screenStream;
    }
  }, [screenStream]);

  // Attach camera stream to PiP video
  useEffect(() => {
    if (cameraVideoRef.current && cameraStream) {
      cameraVideoRef.current.srcObject = cameraStream;
    }
  }, [cameraStream]);

  // Auto-hide controls after inactivity
  const resetHideTimer = useCallback(() => {
    setShowControls(true);
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => setShowControls(false), 4000);
  }, []);

  // Auto-hide cursor after inactivity
  const resetCursorHideTimer = useCallback(() => {
    setCursorHidden(false);
    if (cursorHideTimer.current) clearTimeout(cursorHideTimer.current);
    cursorHideTimer.current = setTimeout(() => setCursorHidden(true), 3000);
  }, []);

  useEffect(() => {
    resetHideTimer();
    return () => {
      if (hideTimer.current) clearTimeout(hideTimer.current);
    };
  }, [resetHideTimer]);

  useEffect(() => {
    resetCursorHideTimer();
    return () => {
      if (cursorHideTimer.current) clearTimeout(cursorHideTimer.current);
    };
  }, [resetCursorHideTimer]);

  // Format duration
  const formatDuration = (secs: number): string => {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  // PiP drag handlers
  const handlePipMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    const startX = e.clientX - pipPosition.x;
    const startY = e.clientY - pipPosition.y;

    const handleMove = (ev: MouseEvent) => {
      setPipPosition({
        x: Math.max(0, Math.min(window.innerWidth - 200, ev.clientX - startX)),
        y: Math.max(0, Math.min(window.innerHeight - 150, ev.clientY - startY)),
      });
    };

    const handleUp = () => {
      setIsDragging(false);
      document.removeEventListener('mousemove', handleMove);
      document.removeEventListener('mouseup', handleUp);
    };

    document.addEventListener('mousemove', handleMove);
    document.addEventListener('mouseup', handleUp);
  };

  // Toggle fullscreen
  const handleToggleFullscreen = async () => {
    if (!containerRef.current) return;
    try {
      if (!isFullscreen) {
        if (containerRef.current.requestFullscreen) {
          await containerRef.current.requestFullscreen();
          setIsFullscreen(true);
        }
      } else {
        if (document.fullscreenElement) {
          await document.exitFullscreen();
          setIsFullscreen(false);
        }
      }
    } catch (err) {
      console.error('Fullscreen error:', err);
    }
  };

  // Handle keyboard shortcuts
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (isFullscreen) {
        document.exitFullscreen().then(() => setIsFullscreen(false));
      } else {
        onExitFullscreen();
      }
    } else if (e.code === 'Space' && isLocalPresenter) {
      e.preventDefault();
      isPaused ? onResume() : onPause();
    } else if (e.key === 'q' || e.key === 'Q') {
      setShowQualityMenu(!showQualityMenu);
    }
  };

  // Double-click to toggle fullscreen
  const handleDoubleClick = () => {
    handleToggleFullscreen();
  };

  if (!screenStream && !presenter) return null;

  return (
    <div
      ref={containerRef}
      className={`fixed inset-0 z-50 bg-black flex flex-col ${cursorHidden ? 'cursor-none' : ''}`}
      onMouseMove={() => {
        resetHideTimer();
        resetCursorHideTimer();
      }}
      onClick={() => {
        resetHideTimer();
        resetCursorHideTimer();
      }}
      onKeyDown={handleKeyDown}
      onDoubleClick={handleDoubleClick}
      tabIndex={0}
    >
      {/* ── Screen Video ──────────────────────────── */}
      <div className="flex-1 relative overflow-hidden">
        {screenStream ? (
          <video
            ref={screenVideoRef}
            autoPlay
            playsInline
            muted
            className="w-full h-full object-contain"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-gray-400">
            <div className="text-center">
              <svg className="w-16 h-16 mx-auto mb-4 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
              <p className="text-lg">
                {presenter ? `Waiting for ${presenter.displayName}'s screen...` : 'No screen shared'}
              </p>
            </div>
          </div>
        )}

        {/* Paused overlay */}
        {isPaused && (
          <div className="absolute inset-0 bg-black bg-opacity-60 flex items-center justify-center">
            <div className="text-white text-center">
              <svg className="w-12 h-12 mx-auto mb-2" fill="currentColor" viewBox="0 0 24 24">
                <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
              </svg>
              <p className="text-lg">Screen sharing paused</p>
            </div>
          </div>
        )}

        {/* ── PiP Camera ─────────────────────────── */}
        {cameraStream && (
          <div
            className={`absolute rounded-lg overflow-hidden shadow-2xl border-2 border-gray-700 cursor-move transition-shadow ${isDragging ? 'shadow-lg border-blue-500' : ''}`}
            style={{
              width: 192,
              height: 144,
              left: pipPosition.x,
              bottom: pipPosition.y,
            }}
            onMouseDown={handlePipMouseDown}
          >
            <video
              ref={cameraVideoRef}
              autoPlay
              playsInline
              muted
              className="w-full h-full object-cover"
              style={{ transform: 'scaleX(-1)' }}
            />
          </div>
        )}
      </div>

      {/* ── Top Bar (presenter info) ─────────────── */}
      <div
        className={`absolute top-0 left-0 right-0 p-4 flex items-center justify-between transition-opacity duration-300 ${showControls ? 'opacity-100' : 'opacity-0'}`}
        style={{ background: 'linear-gradient(180deg, rgba(0,0,0,0.7) 0%, transparent 100%)' }}
      >
        <div className="flex items-center gap-4">
          {/* Red recording dot */}
          <span className="w-3 h-3 bg-red-500 rounded-full animate-pulse" />
          <span className="text-white text-sm font-medium">
            {isLocalPresenter
              ? 'You are sharing your screen'
              : presenter
                ? `${presenter.displayName} is sharing`
                : 'Screen Share'}
          </span>
          <span className="text-gray-400 text-sm">
            {formatDuration(duration)}
          </span>

          {/* Viewer count badge */}
          {viewerCount > 0 && (
            <div className="flex items-center gap-1 px-2 py-1 bg-blue-600/40 rounded text-white text-xs">
              <Users size={14} />
              <span>{viewerCount}</span>
            </div>
          )}

          {/* Connection quality indicator */}
          {currentBitrate > 0 && (
            <div className="flex items-center gap-1 px-2 py-1 bg-slate-600/40 rounded text-white text-xs">
              <AlertCircle size={14} />
              <span>{currentBitrate.toFixed(1)} Mbps</span>
            </div>
          )}

          {/* Audio capture indicator */}
          {hasAudio && (
            <div className="w-12 h-6 bg-slate-700/60 rounded flex items-center justify-center">
              <div className="flex gap-0.5">
                <div className="w-0.5 h-2 bg-green-400 rounded-full animate-pulse" />
                <div className="w-0.5 h-3 bg-green-400 rounded-full animate-pulse animation-delay-100" />
                <div className="w-0.5 h-2 bg-green-400 rounded-full animate-pulse animation-delay-200" />
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={handleToggleFullscreen}
            className="text-gray-300 hover:text-white p-1.5 rounded hover:bg-white/10 transition-colors"
            title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          >
            {isFullscreen ? <Minimize2 size={20} /> : <Maximize2 size={20} />}
          </button>
          <button
            onClick={onExitFullscreen}
            className="text-gray-300 hover:text-white p-1.5 rounded hover:bg-white/10 transition-colors"
            title="Exit screen share"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {/* ── Bottom Toolbar ───────────────────────── */}
      {showToolbar && isLocalPresenter && showControls && (
        <div
          className="absolute bottom-20 left-1/2 transform -translate-x-1/2 p-3 flex items-center gap-3 bg-gray-900/90 rounded-lg border border-gray-700 transition-opacity duration-300 z-40"
        >
          <button
            title="Pen tool (coming soon)"
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
            disabled
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21H3v-3.5L16.732 2.732z" />
            </svg>
          </button>
          <div className="w-px h-6 bg-gray-700" />
          <button
            title="Zoom in"
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
            disabled
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 7v6m3-3H7" />
            </svg>
          </button>
          <button
            title="Zoom out"
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
            disabled
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM13 10H7" />
            </svg>
          </button>
          <button
            title="Fit to window"
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
            disabled
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 1v4m0 0h-4m4 0l-5-5" />
            </svg>
          </button>
        </div>
      )}

      {/* ── Bottom Controls ──────────────────────── */}
      <div
        className={`absolute bottom-0 left-0 right-0 p-4 flex items-center justify-center gap-3 transition-opacity duration-300 ${showControls ? 'opacity-100' : 'opacity-0'}`}
        style={{ background: 'linear-gradient(0deg, rgba(0,0,0,0.8) 0%, transparent 100%)' }}
      >
        {isLocalPresenter ? (
          /* Presenter controls */
          <>
            {/* Pause / Resume */}
            <button
              onClick={isPaused ? onResume : onPause}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white text-sm transition-colors"
              title={isPaused ? "Resume sharing (Space)" : "Pause sharing (Space)"}
            >
              {isPaused ? (
                <>
                  <Play size={16} />
                  Resume
                </>
              ) : (
                <>
                  <Pause size={16} />
                  Pause
                </>
              )}
            </button>

            {/* Switch Source */}
            <button
              onClick={onSwitchSource}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white text-sm transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Switch Source
            </button>

            {/* Quality */}
            <div className="relative">
              <button
                onClick={() => setShowQualityMenu(!showQualityMenu)}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white text-sm transition-colors"
                title="Quality preset (Q)"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                Quality
              </button>

              {showQualityMenu && (
                <div className="absolute bottom-full mb-2 left-0 bg-gray-800 rounded-lg shadow-xl py-1 min-w-[180px]">
                  {qualityPresets.map((p) => (
                    <button
                      key={p.key}
                      onClick={() => {
                        onChangeQuality(p.key);
                        setShowQualityMenu(false);
                      }}
                      className={`w-full text-left px-4 py-2 text-sm hover:bg-gray-700 transition-colors ${
                        qualityPreset === p.key ? 'text-blue-400' : 'text-gray-300'
                      }`}
                    >
                      {qualityPreset === p.key && '✓ '}
                      {p.label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Stop Sharing */}
            <button
              onClick={onStop}
              className="flex items-center gap-2 px-6 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium transition-colors"
              title="Stop sharing (ESC)"
            >
              <AlertCircle size={16} />
              Stop Sharing
            </button>
          </>
        ) : (
          /* Viewer controls */
          <>
            {queuePosition > 0 ? (
              <button
                onClick={onCancelRequest}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-yellow-600 hover:bg-yellow-700 text-white text-sm transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                In queue (#{queuePosition}) — Cancel
              </button>
            ) : (
              <button
                onClick={onRequestPresenter}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                </svg>
                Request to Present
                {queueLength > 0 && ` (${queueLength} waiting)`}
              </button>
            )}
          </>
        )}
      </div>

      <style>{`
        .animation-delay-100 {
          animation-delay: 0.1s;
        }
        .animation-delay-200 {
          animation-delay: 0.2s;
        }
      `}</style>
    </div>
  );
};

export default ScreenShareOverlay;
