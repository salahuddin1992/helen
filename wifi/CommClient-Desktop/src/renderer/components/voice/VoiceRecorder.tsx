/**
 * VoiceRecorder — Hold-to-record component with animated waveform
 * visualization, timer, cancel by swiping. After release, mounts a
 * VoicePreviewPanel so the user can listen, cancel, or send before
 * the upload fires.
 */
import React, { useRef, useState, useEffect } from 'react';
import { Mic, X } from 'lucide-react';
import { api } from '../../services/api.client';
import { socketManager } from '../../services/socket.manager';
import { VoicePreviewPanel } from './VoicePreviewPanel';

interface VoiceRecorderProps {
  channelId: string;
  onRecordStart?: () => void;
  onRecordEnd?: () => void;
  onError?: (error: string) => void;
}

export const VoiceRecorder: React.FC<VoiceRecorderProps> = ({
  channelId,
  onRecordStart,
  onRecordEnd,
  onError,
}) => {
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const analyzerRef = useRef<AnalyserNode | null>(null);
  const animationRef = useRef<number>();

  const [isRecording, setIsRecording] = useState(false);
  const [duration, setDuration] = useState(0);
  const [amplitudes, setAmplitudes] = useState<number[]>(
    Array(32).fill(0)
  );
  const [isSwiped, setIsSwiped] = useState(false);
  const [dragOffset, setDragOffset] = useState(0);
  // Preview state — set after onstop fires; clears on cancel/send.
  const [pendingBlob, setPendingBlob] = useState<Blob | null>(null);
  const [pendingDuration, setPendingDuration] = useState(0);
  const [isSending, setIsSending] = useState(false);

  const durationRef = useRef(0);
  const startTimeRef = useRef<number>();
  const startXRef = useRef(0);

  useEffect(() => {
    return () => {
      if (animationRef.current) cancelAnimationFrame(animationRef.current);
      if (audioStreamRef.current) {
        audioStreamRef.current.getTracks().forEach((t) => t.stop());
      }
    };
  }, []);

  const initializeRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioStreamRef.current = stream;

      const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      const source = audioContext.createMediaStreamSource(stream);
      source.connect(analyser);
      analyzerRef.current = analyser;

      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          audioChunksRef.current.push(e.data);
        }
      };

      mediaRecorder.start();
      setIsRecording(true);
      startTimeRef.current = Date.now();
      durationRef.current = 0;
      setDuration(0);
      setIsSwiped(false);
      setDragOffset(0);
      onRecordStart?.();

      drawWaveform();
    } catch (error) {
      onError?.(error instanceof Error ? error.message : 'Failed to access microphone');
    }
  };

  const drawWaveform = () => {
    const canvas = canvasRef.current;
    if (!canvas || !analyzerRef.current) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const bufferLength = analyzerRef.current.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    analyzerRef.current.getByteFrequencyData(dataArray);

    // Draw bars for amplitudes
    ctx.fillStyle = '#1f2937';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const barWidth = canvas.width / 32;
    const maxHeight = canvas.height;

    for (let i = 0; i < 32; i++) {
      const dataIndex = Math.floor((i / 32) * bufferLength);
      const value = dataArray[dataIndex] / 255;
      const barHeight = value * maxHeight;
      const y = maxHeight - barHeight;

      const hue = 200 + (i / 32) * 60;
      ctx.fillStyle = `hsl(${hue}, 100%, 45%)`;
      ctx.fillRect(i * barWidth + 2, y, barWidth - 4, barHeight);

      setAmplitudes((prev) => {
        const next = [...prev];
        next[i] = value;
        return next;
      });
    }

    animationRef.current = requestAnimationFrame(drawWaveform);
  };

  const stopRecording = async (isCancelled: boolean = false) => {
    if (!mediaRecorderRef.current || !isRecording) return;

    if (animationRef.current) cancelAnimationFrame(animationRef.current);

    return new Promise<void>((resolve) => {
      const recorder = mediaRecorderRef.current;
      if (!recorder) {
        resolve();
        return;
      }

      // Release the microphone IMMEDIATELY rather than waiting for the
      // async `onstop` callback. The browser holds the device open until
      // every track is stopped — if the user cancels and instantly tries
      // to record again, the second `getUserMedia` call would fail with
      // NotReadableError because the tracks were still alive in the
      // pending onstop frame.
      audioStreamRef.current?.getTracks().forEach((t) => t.stop());

      recorder.onstop = async () => {
        // Defensive: tracks already stopped above, but if onstop fired
        // independently (browser quirk) make sure they really are gone.
        audioStreamRef.current?.getTracks().forEach((t) => t.stop());

        if (!isCancelled && audioChunksRef.current.length > 0) {
          // Hand the blob off to VoicePreviewPanel for listen-before-
          // send. The actual upload + emit happens inside
          // ``handleSendPending`` once the user clicks ✓.
          const audioBlob = new Blob(audioChunksRef.current, {
            type: 'audio/wav',
          });
          setPendingBlob(audioBlob);
          setPendingDuration(durationRef.current);
        }

        setIsRecording(false);
        setDuration(0);
        durationRef.current = 0;
        onRecordEnd?.();
        resolve();
      };

      recorder.stop();
    });
  };

  const handleSendPending = async () => {
    if (!pendingBlob || isSending) return;
    setIsSending(true);
    try {
      const audioFile = new File(
        [pendingBlob], 'voice_message.wav',
        { type: 'audio/wav' },
      );
      const response = await api.uploadFile(audioFile);
      socketManager.emit('message:send', {
        channel_id: channelId,
        content: '',
        type: 'voice',
        file_id: response.file_id,
      });
      setPendingBlob(null);
      setPendingDuration(0);
    } catch (error) {
      onError?.(
        error instanceof Error
          ? error.message
          : 'Failed to upload voice message',
      );
    } finally {
      setIsSending(false);
    }
  };

  const handleCancelPending = () => {
    if (isSending) return;
    setPendingBlob(null);
    setPendingDuration(0);
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    startXRef.current = e.clientX;
    initializeRecording();
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isRecording) return;

    const offset = e.clientX - startXRef.current;
    setDragOffset(offset);

    // Swipe to cancel if dragged > 100px left
    if (offset < -100) {
      setIsSwiped(true);
    } else {
      setIsSwiped(false);
    }
  };

  const handleMouseUp = async () => {
    await stopRecording(isSwiped);
  };

  useEffect(() => {
    if (!isRecording) return;

    const interval = setInterval(() => {
      durationRef.current += 10;
      setDuration(Math.floor(durationRef.current / 1000));
    }, 10);

    return () => clearInterval(interval);
  }, [isRecording]);

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  // Preview mode swallows the whole recorder UI — the user is
  // listening to/deciding about the just-recorded clip.
  if (pendingBlob) {
    return (
      <VoicePreviewPanel
        blob={pendingBlob}
        durationSec={pendingDuration}
        onCancel={handleCancelPending}
        onSend={handleSendPending}
        isSending={isSending}
      />
    );
  }

  return (
    <div className="flex flex-col items-center gap-2">
      <div
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        className={`relative cursor-pointer transition-all ${
          isRecording
            ? isSwiped
              ? 'bg-red-500'
              : 'bg-blue-500'
            : 'bg-gray-300 hover:bg-gray-400'
        } rounded-full p-3 text-white`}
        style={
          isRecording
            ? { transform: `translateX(${dragOffset}px)` }
            : undefined
        }
      >
        {isRecording && isSwiped ? (
          <X size={20} />
        ) : (
          <Mic size={20} />
        )}
      </div>

      {isRecording && (
        <>
          <div className="text-sm font-medium text-gray-700">
            {formatTime(duration)}
          </div>
          <canvas
            ref={canvasRef}
            width={200}
            height={40}
            className="rounded bg-gray-900"
          />
          <div className="text-xs text-gray-500">
            {isSwiped ? 'Release to cancel' : 'Release to send'}
          </div>
        </>
      )}
    </div>
  );
};
