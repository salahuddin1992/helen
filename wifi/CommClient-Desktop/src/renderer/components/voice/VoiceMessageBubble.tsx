/**
 * VoiceMessageBubble — Chat bubble wrapper that renders VoicePlayer
 * inside a message bubble with sender info and timestamp.
 */
import React from 'react';
import { VoicePlayer } from './VoicePlayer';

interface VoiceMessageBubbleProps {
  senderName: string;
  senderAvatar?: string | null;
  audioUrl: string;
  timestamp: string;
  isOwn?: boolean;
  waveformData?: { peaks: number[]; duration: number };
}

export const VoiceMessageBubble: React.FC<VoiceMessageBubbleProps> = ({
  senderName,
  senderAvatar,
  audioUrl,
  timestamp,
  isOwn = false,
  waveformData,
}) => {
  const formatTime = (isoString: string) => {
    const date = new Date(isoString);
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();

    if (sameDay) {
      return date.toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      } as any);
    }

    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    } as any);
  };

  return (
    <div className={`flex gap-2 ${isOwn ? 'flex-row-reverse' : ''}`}>
      {!isOwn && senderAvatar ? (
        <img
          src={senderAvatar}
          alt={senderName}
          className="h-8 w-8 flex-shrink-0 rounded-full object-cover"
        />
      ) : (
        <div className="h-8 w-8 flex-shrink-0 rounded-full bg-gray-400" />
      )}

      <div className={`flex flex-col gap-1 ${isOwn ? 'items-end' : ''}`}>
        <div className="flex items-baseline gap-2">
          {!isOwn && (
            <p className="text-xs font-semibold text-gray-700">{senderName}</p>
          )}
          <span className="text-xs text-gray-500">{formatTime(timestamp)}</span>
        </div>

        <div
          className={`rounded-lg px-3 py-2 ${
            isOwn ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-900'
          }`}
        >
          <VoicePlayer
            audioUrl={audioUrl}
            waveformData={waveformData}
            fileName="Voice Message"
          />
        </div>
      </div>
    </div>
  );
};
