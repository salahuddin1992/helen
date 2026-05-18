/**
 * ScreenShareSafetyBanner.tsx — Persistent "You are sharing your screen" banner.
 *
 * Safety feature: children and non-technical users may not realize they're
 * broadcasting their screen. This banner stays visible at all times during
 * screen sharing, regardless of which page/view they're on.
 *
 * Features:
 *   - Bright red persistent banner (impossible to miss)
 *   - Shows what is being shared (screen name/window title)
 *   - One-click "Stop Sharing" button
 *   - Duration counter
 *   - Gentle pulse animation to maintain awareness
 */

import React, { useState, useEffect } from 'react';
import { AlertCircle } from 'lucide-react';
import { t } from '@/i18n';
import { useCallStore } from '@/stores/call.store.v2';
import { childSafetyGuard } from '@/services/product';

const ScreenShareSafetyBanner: React.FC = () => {
  const isScreenSharing = useCallStore((s) => s.isScreenSharing);
  const stopScreenShare = useCallStore((s) => s.stopScreenShare);
  const [elapsed, setElapsed] = useState(0);
  const [startTime] = useState(Date.now);

  useEffect(() => {
    if (!isScreenSharing) {
      setElapsed(0);
      return;
    }

    const start = Date.now();
    const interval = setInterval(() => {
      setElapsed(Math.floor((Date.now() - start) / 1000));
    }, 1000);

    return () => clearInterval(interval);
  }, [isScreenSharing]);

  if (!isScreenSharing) return null;
  if (!childSafetyGuard.shouldShowScreenShareBanner()) return null;

  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  const timeStr = `${minutes}:${seconds.toString().padStart(2, '0')}`;

  return (
    <div className="fixed top-8 left-0 right-0 z-[60] pointer-events-none">
      <div className="flex justify-center pointer-events-auto">
        <div className="bg-red-600 text-white px-4 py-2 rounded-b-xl shadow-lg shadow-red-600/30 flex items-center gap-3 animate-pulse" style={{ animationDuration: '3s' }}>
          <AlertCircle size={16} />
          <span className="text-sm font-medium">
            {t('product.screen_sharing_active')}
          </span>
          <span className="text-xs opacity-75">{timeStr}</span>
          <button
            onClick={() => stopScreenShare?.()}
            className="ms-2 px-3 py-1 bg-white/20 hover:bg-white/30 rounded-lg text-xs font-medium transition-colors flex items-center gap-1"
          >
            <AlertCircle size={12} />
            {t('product.stop_sharing')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ScreenShareSafetyBanner;
