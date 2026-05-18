/**
 * OfflineBanner.tsx — Thin connection status banner at top of app.
 *
 * Shows when offline or connecting. Auto-hides when connected.
 */

import React, { useEffect, useState } from 'react';
import { WifiOff, Wifi } from 'lucide-react';

interface OfflineBannerProps {
  isOnline: boolean;
  isConnecting: boolean;
}

const OfflineBanner: React.FC<OfflineBannerProps> = ({ isOnline, isConnecting }) => {
  const [isVisible, setIsVisible] = useState(!isOnline || isConnecting);

  useEffect(() => {
    setIsVisible(!isOnline || isConnecting);
  }, [isOnline, isConnecting]);

  if (isVisible && isOnline && !isConnecting) {
    return null;
  }

  return (
    <div
      className={`fixed top-0 left-0 right-0 z-40 px-4 py-2 text-sm font-medium transition-all duration-300 transform ${
        isVisible ? 'translate-y-0 opacity-100' : '-translate-y-full opacity-0'
      } ${isOnline && isConnecting ? 'bg-yellow-500/20 text-yellow-300 border-b border-yellow-500/30' : 'bg-red-500/20 text-red-300 border-b border-red-500/30'}`}
    >
      <div className="flex items-center gap-2 justify-center max-w-full">
        {isOnline && isConnecting ? (
          <>
            <Wifi size={16} className="animate-pulse flex-shrink-0" />
            <span>Reconnecting...</span>
          </>
        ) : (
          <>
            <WifiOff size={16} className="flex-shrink-0" />
            <span>You are offline</span>
          </>
        )}
      </div>
    </div>
  );
};

export default OfflineBanner;
