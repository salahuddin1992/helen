/**
 * FriendlyConnectionBanner.tsx — User-friendly connection status display.
 *
 * Replaces the technical ConnectionTracker with a 5-state friendly banner
 * powered by ConnectionResilience service.
 *
 * States and their UI:
 *   connected  → No banner (invisible)
 *   connecting → Blue banner: "Connecting..." with spinner
 *   slow       → Yellow banner: "Connection is slow" with RTT
 *   offline    → Red banner: "You're offline" with retry countdown
 *   no_server  → Red banner: "Can't find server" with "Try Again" button
 *
 * Features:
 *   - Auto-hides when connected (smooth slide-up animation)
 *   - Shows retry countdown in offline state
 *   - One-tap "Try Again" button
 *   - RTL-aware layout
 *   - Never shows IPs, ports, or error codes
 */

import React, { useState, useEffect, useCallback } from 'react';
import { Wifi, WifiOff, Loader2, RefreshCw, AlertTriangle } from 'lucide-react';
import { t } from '@/i18n';
import {
  connectionResilience,
  type ConnectionStatus,
  type FriendlyConnectionState,
} from '@/services/product';

// ── State Visual Config ─────────────────────────────────────

const STATE_CONFIG: Record<FriendlyConnectionState, {
  bgClass: string;
  textClass: string;
  icon: React.ReactNode;
  showRetry: boolean;
  showCountdown: boolean;
}> = {
  connected: {
    bgClass: 'bg-green-600/90',
    textClass: 'text-green-100',
    icon: <Wifi size={14} />,
    showRetry: false,
    showCountdown: false,
  },
  connecting: {
    bgClass: 'bg-blue-600/90',
    textClass: 'text-blue-100',
    icon: <Loader2 size={14} className="animate-spin" />,
    showRetry: false,
    showCountdown: false,
  },
  slow: {
    bgClass: 'bg-yellow-600/90',
    textClass: 'text-yellow-100',
    icon: <AlertTriangle size={14} />,
    showRetry: false,
    showCountdown: false,
  },
  offline: {
    bgClass: 'bg-red-600/90',
    textClass: 'text-red-100',
    icon: <WifiOff size={14} />,
    showRetry: true,
    showCountdown: true,
  },
  no_server: {
    bgClass: 'bg-red-600/90',
    textClass: 'text-red-100',
    icon: <WifiOff size={14} />,
    showRetry: true,
    showCountdown: false,
  },
};

// ── Component ───────────────────────────────────────────────

const FriendlyConnectionBanner: React.FC = () => {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [visible, setVisible] = useState(false);
  const [justConnected, setJustConnected] = useState(false);

  useEffect(() => {
    const unsub = connectionResilience.onChange((newStatus) => {
      setStatus(newStatus);

      if (newStatus.state === 'connected') {
        // Brief "connected" flash, then hide
        setJustConnected(true);
        setVisible(true);
        const timer = setTimeout(() => {
          setVisible(false);
          setJustConnected(false);
        }, 2000);
        return () => clearTimeout(timer);
      } else {
        setJustConnected(false);
        setVisible(true);
      }
    });

    return unsub;
  }, []);

  const handleRetry = useCallback(() => {
    connectionResilience.retryNow();
  }, []);

  // Don't render if no status or connected (after flash)
  if (!status) return null;
  if (status.state === 'connected' && !visible) return null;

  const config = STATE_CONFIG[justConnected ? 'connected' : status.state];

  return (
    <div
      className={`fixed top-8 left-0 right-0 z-50 transition-all duration-300 ${
        visible ? 'translate-y-0 opacity-100' : '-translate-y-full opacity-0'
      }`}
    >
      <div className={`${config.bgClass} text-center py-2 px-4`}>
        <div className={`flex items-center justify-center gap-2 text-xs font-medium ${config.textClass}`}>
          {config.icon}
          <span>{t(status.messageKey)}</span>

          {/* Retry countdown */}
          {config.showCountdown && status.retryCountdown > 0 && (
            <span className="opacity-75">
              · {t('conn.retrying_in')} {status.retryCountdown}s
            </span>
          )}

          {/* Retry button */}
          {config.showRetry && (
            <button
              onClick={handleRetry}
              className="ms-2 px-2.5 py-0.5 bg-white/20 hover:bg-white/30 rounded text-xs font-medium transition-colors flex items-center gap-1"
            >
              <RefreshCw size={10} />
              {t('conn.try_again')}
            </button>
          )}
        </div>

        {/* Hint text */}
        {status.hintKey && !justConnected && (
          <p className={`text-[10px] mt-0.5 opacity-70 ${config.textClass}`}>
            {t(status.hintKey)}
          </p>
        )}
      </div>
    </div>
  );
};

export default FriendlyConnectionBanner;
