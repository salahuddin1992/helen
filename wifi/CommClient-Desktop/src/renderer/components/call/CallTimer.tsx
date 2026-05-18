/**
 * CallTimer.tsx — Call duration display component.
 *
 * Displays elapsed time since call started (MM:SS or H:MM:SS format).
 * Updates every second while active.
 */

import React, { useState, useEffect } from 'react';

interface CallTimerProps {
  startTime: number; // Epoch milliseconds (Date.now())
  isActive: boolean;
}

const CallTimer: React.FC<CallTimerProps> = ({ startTime, isActive }) => {
  const [duration, setDuration] = useState<string>('00:00');

  useEffect(() => {
    if (!isActive) {
      return;
    }

    const updateDuration = () => {
      const now = Date.now();
      const elapsed = Math.floor((now - startTime) / 1000);

      const hours = Math.floor(elapsed / 3600);
      const minutes = Math.floor((elapsed % 3600) / 60);
      const seconds = elapsed % 60;

      if (hours > 0) {
        setDuration(
          `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
        );
      } else {
        setDuration(
          `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
        );
      }
    };

    // Update immediately
    updateDuration();

    // Then update every second
    const interval = setInterval(updateDuration, 1000);

    return () => clearInterval(interval);
  }, [startTime, isActive]);

  return (
    <span className="text-surface-300 font-mono text-sm tabular-nums">
      {duration}
    </span>
  );
};

export default CallTimer;
