/**
 * VoicePlayer — Inline audio player with waveform display, play/pause,
 * seek by tapping, playback speed control, duration display.
 */
import React, { useRef, useState, useEffect } from 'react';
import { Play, Pause, Volume2 } from 'lucide-react';

interface WaveformData {
  peaks: number[];
  duration: number;
}

interface VoicePlayerProps {
  audioUrl: string;
  waveformData?: WaveformData;
  fileName?: string;
  onPlay?: () => void;
  onPause?: () => void;
}

export const VoicePlayer: React.FC<VoicePlayerProps> = ({
  audioUrl,
  waveformData,
  fileName = 'Voice Message',
  onPlay,
  onPause,
}) => {
  const audioRef = useRef<HTMLAudioElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playbackSpeed, setPlaybackSpeed] = useState<1 | 1.5 | 2>(1);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const handleLoadedMetadata = () => {
      setDuration(audio.duration);
      setIsLoading(false);
    };

    const handleTimeUpdate = () => {
      setCurrentTime(audio.currentTime);
    };

    const handleEnded = () => {
      setIsPlaying(false);
      setCurrentTime(0);
    };

    audio.addEventListener('loadedmetadata', handleLoadedMetadata);
    audio.addEventListener('timeupdate', handleTimeUpdate);
    audio.addEventListener('ended', handleEnded);

    return () => {
      audio.removeEventListener('loadedmetadata', handleLoadedMetadata);
      audio.removeEventListener('timeupdate', handleTimeUpdate);
      audio.removeEventListener('ended', handleEnded);
    };
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.playbackRate = playbackSpeed;
  }, [playbackSpeed]);

  const togglePlay = () => {
    if (!audioRef.current) return;

    if (isPlaying) {
      audioRef.current.pause();
      setIsPlaying(false);
      onPause?.();
    } else {
      audioRef.current.play();
      setIsPlaying(true);
      onPlay?.();
    }
  };

  const handleSeek = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!audioRef.current || duration === 0) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const percentage = Math.max(0, Math.min(1, x / canvas.width));
    audioRef.current.currentTime = percentage * duration;
    setCurrentTime(percentage * duration);
  };

  const drawWaveform = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.fillStyle = '#1f2937';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Draw peaks from waveform data or synthetic visualization
    const peaks = waveformData?.peaks || generateSyntheticPeaks(64);
    const barWidth = canvas.width / peaks.length;
    const centerY = canvas.height / 2;

    // Played portion
    const playedRatio = duration > 0 ? currentTime / duration : 0;

    for (let i = 0; i < peaks.length; i++) {
      const isPast = i / peaks.length < playedRatio;
      const height = peaks[i] * canvas.height;
      const x = i * barWidth + 1;
      const y = centerY - height / 2;

      ctx.fillStyle = isPast ? '#3b82f6' : '#6b7280';
      ctx.fillRect(x, y, barWidth - 2, height);
    }

    // Current position indicator
    ctx.fillStyle = '#ef4444';
    ctx.fillRect(playedRatio * canvas.width - 1, 0, 2, canvas.height);
  };

  useEffect(() => {
    drawWaveform();
  }, [currentTime, duration, waveformData]);

  const formatTime = (seconds: number) => {
    if (!isFinite(seconds)) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const generateSyntheticPeaks = (count: number): number[] => {
    return Array.from({ length: count }, (_, i) => {
      const t = i / count;
      return 0.3 + 0.5 * Math.sin(t * Math.PI * 4) * (1 - Math.abs(0.5 - t) * 2);
    });
  };

  return (
    <div className="flex flex-col gap-2 rounded-lg bg-gray-800 p-3">
      <audio ref={audioRef} src={audioUrl} />

      <div className="flex items-center gap-3">
        <button
          onClick={togglePlay}
          disabled={isLoading}
          className="flex-shrink-0 rounded-full bg-blue-600 p-2 text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {isLoading ? (
            <Volume2 size={16} />
          ) : isPlaying ? (
            <Pause size={16} />
          ) : (
            <Play size={16} className="ml-0.5" />
          )}
        </button>

        <div className="flex-1">
          <p className="truncate text-xs font-medium text-gray-300">
            {fileName}
          </p>
          <canvas
            ref={canvasRef}
            width={200}
            height={32}
            onClick={handleSeek}
            className="h-8 w-full cursor-pointer rounded bg-gray-900"
          />
        </div>

        <div className="flex flex-col items-end gap-1">
          <div className="text-xs text-gray-400">
            {formatTime(currentTime)} / {formatTime(duration)}
          </div>
          <select
            value={playbackSpeed}
            onChange={(e) =>
              setPlaybackSpeed(parseFloat(e.target.value) as 1 | 1.5 | 2)
            }
            className="rounded bg-gray-700 px-2 py-1 text-xs text-gray-300"
          >
            <option value={1}>1x</option>
            <option value={1.5}>1.5x</option>
            <option value={2}>2x</option>
          </select>
        </div>
      </div>
    </div>
  );
};
