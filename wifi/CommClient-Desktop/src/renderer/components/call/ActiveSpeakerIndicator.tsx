/**
 * ActiveSpeakerIndicator.tsx — Visual indicator for who is currently speaking.
 *
 * Shows animated pulsing ring when participant is active.
 */

import React from 'react';

interface ActiveSpeakerIndicatorProps {
  displayName: string;
  isActive: boolean;
}

const ActiveSpeakerIndicator: React.FC<ActiveSpeakerIndicatorProps> = ({
  displayName,
  isActive,
}) => {
  if (!isActive) {
    return null;
  }

  return (
    <div className="flex flex-col items-center gap-2 p-3 bg-green-500/10 border border-green-500/30 rounded-lg backdrop-blur-sm">
      {/* Pulse ring animation */}
      <div className="relative w-12 h-12 flex items-center justify-center">
        <div className="absolute inset-0 rounded-full border-2 border-green-500 animate-pulse" />
        <div className="absolute inset-1 rounded-full border border-green-500/50" />
        <div className="w-3 h-3 rounded-full bg-green-500" />
      </div>

      {/* Volume2 name */}
      <div className="text-center">
        <p className="text-xs font-medium text-green-400">{displayName}</p>
        <p className="text-xs text-green-400/70">Speaking</p>
      </div>
    </div>
  );
};

export default ActiveSpeakerIndicator;
