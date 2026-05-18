import React, { useEffect, useRef, useCallback } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { Phone, PhoneOff } from 'lucide-react';
import { t } from '@/i18n';

const IncomingCall: React.FC = () => {
  const { incomingCall, acceptCall, rejectCall } = useCallStore();
  const ringtoneRef = useRef<{ stop: () => void } | null>(null);

  const stopRingtone = useCallback(() => {
    if (ringtoneRef.current) {
      ringtoneRef.current.stop();
      ringtoneRef.current = null;
    }
  }, []);

  useEffect(() => {
    // Generate ringtone using Web Audio API (440Hz sine wave, pulsing pattern)
    let audioCtx: AudioContext | null = null;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    try {
      audioCtx = new AudioContext();
      let oscillator: OscillatorNode | null = null;
      let gainNode: GainNode | null = null;

      const playTone = () => {
        if (!audioCtx || audioCtx.state === 'closed') return;

        oscillator = audioCtx.createOscillator();
        gainNode = audioCtx.createGain();

        oscillator.type = 'sine';
        oscillator.frequency.setValueAtTime(440, audioCtx.currentTime);
        gainNode.gain.setValueAtTime(0.3, audioCtx.currentTime);
        // Fade out after 400ms
        gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.4);

        oscillator.connect(gainNode);
        gainNode.connect(audioCtx.destination);

        oscillator.start();
        oscillator.stop(audioCtx.currentTime + 0.4);
      };

      // Play immediately, then every 1.5s (ring-pause pattern)
      playTone();
      intervalId = setInterval(playTone, 1500);

      ringtoneRef.current = {
        stop: () => {
          if (intervalId) clearInterval(intervalId);
          intervalId = null;
          if (audioCtx && audioCtx.state !== 'closed') {
            audioCtx.close().catch(() => {});
          }
          audioCtx = null;
        },
      };
    } catch {
      // Web Audio API not available
    }

    return () => {
      stopRingtone();
    };
  }, [stopRingtone]);

  if (!incomingCall) return null;

  const callTypeLabel =
    incomingCall.media_type === 'video' ? t('call.video_call') : t('call.audio_call');
  const callerDisplayName = incomingCall.caller_name || 'Unknown Caller';

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm z-50 flex items-center justify-center animate-fadeIn">
      {/* Modal */}
      <div className="bg-surface-900 rounded-2xl shadow-2xl p-8 max-w-md w-full mx-4 animate-slideUp">
        {/* Caller info */}
        <div className="flex flex-col items-center mb-8">
          {/* Avatar */}
          <div className="w-24 h-24 rounded-full bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center mb-6 shadow-lg">
            <span className="text-4xl font-bold text-white">
              {callerDisplayName.charAt(0).toUpperCase()}
            </span>
          </div>

          {/* Caller name */}
          <h2 className="text-2xl font-bold text-text-100 text-center mb-2">
            {callerDisplayName}
          </h2>

          {/* Call type badge */}
          <div className="inline-flex items-center gap-2 px-4 py-2 bg-surface-800 rounded-full">
            <Phone size={16} className="text-blue-400" />
            <span className="text-sm font-medium text-text-300">{callTypeLabel}</span>
          </div>
        </div>

        {/* Call status */}
        <div className="text-center mb-8">
          <p className="text-text-400 text-sm animate-pulse">
            {t('call.incoming')}
          </p>
        </div>

        {/* Action buttons */}
        <div className="flex gap-4">
          {/* Reject button */}
          <button
            onClick={() => { stopRingtone(); rejectCall(); }}
            className="flex-1 flex items-center justify-center gap-3 px-6 py-4 bg-red-600 hover:bg-red-700 active:scale-95 text-white font-semibold rounded-xl transition-all duration-200 shadow-lg hover:shadow-red-500/20"
          >
            <PhoneOff size={20} />
            <span>{t('call.reject')}</span>
          </button>

          {/* Accept button */}
          <button
            onClick={() => { stopRingtone(); acceptCall(); }}
            className="flex-1 flex items-center justify-center gap-3 px-6 py-4 bg-green-600 hover:bg-green-700 active:scale-95 text-white font-semibold rounded-xl transition-all duration-200 shadow-lg hover:shadow-green-500/20 animate-pulse"
          >
            <Phone size={20} />
            <span>{t('call.accept')}</span>
          </button>
        </div>

        {/* Additional info */}
        {incomingCall.media_type === 'video' && (
          <p className="text-xs text-text-500 text-center mt-4">
            {t('call.video_call_incoming')}
          </p>
        )}
      </div>

      {/* Floating pulsing ring animation */}
      <style>{`
        @keyframes slideUp {
          from {
            opacity: 0;
            transform: translateY(20px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }

        @keyframes fadeIn {
          from {
            opacity: 0;
          }
          to {
            opacity: 1;
          }
        }

        @keyframes pulse {
          0%, 100% {
            opacity: 1;
          }
          50% {
            opacity: 0.7;
          }
        }

        .animate-slideUp {
          animation: slideUp 0.3s ease-out;
        }

        .animate-fadeIn {
          animation: fadeIn 0.2s ease-out;
        }

        .animate-pulse {
          animation: pulse 1.5s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }

        @keyframes ringPulse {
          0% {
            box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7);
          }
          70% {
            box-shadow: 0 0 0 20px rgba(34, 197, 94, 0);
          }
          100% {
            box-shadow: 0 0 0 0 rgba(34, 197, 94, 0);
          }
        }
      `}</style>
    </div>
  );
};

export default IncomingCall;
