/**
 * ErrorRecovery.tsx — Friendly error screen with auto-retry and manual fallback.
 *
 * Displays human-readable error messages with contextual help.
 * Supports auto-retry with exponential backoff and a manual override
 * to enter server address directly.
 */

import React, { useState, useEffect } from 'react';
import { WifiOff, Server, RefreshCw, ChevronDown, ChevronUp, AlertTriangle } from 'lucide-react';
import { t } from '@/i18n';
import { StartupError } from '@/stores/app.store';

interface ErrorRecoveryProps {
  error: StartupError;
  errorMessage: string;
  retryCount: number;
  maxRetries: number;
  onRetry: () => void;
  onManualConnect: (url: string) => void;
  onGoToLogin: () => void;
}

const ErrorRecovery: React.FC<ErrorRecoveryProps> = ({
  error,
  errorMessage,
  retryCount,
  maxRetries,
  onRetry,
  onManualConnect,
  onGoToLogin,
}) => {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [manualUrl, setManualUrl] = useState('');
  const [autoRetrySeconds, setAutoRetrySeconds] = useState(0);
  const [fadeIn, setFadeIn] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(t);
  }, []);

  // Auto-retry countdown (exponential backoff: 5s, 10s, 20s)
  useEffect(() => {
    if (retryCount >= maxRetries) return; // Stop auto-retrying

    const delay = Math.min(5 * Math.pow(2, retryCount), 30);
    setAutoRetrySeconds(delay);

    const interval = setInterval(() => {
      setAutoRetrySeconds((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          onRetry();
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(interval);
  }, [retryCount, maxRetries]);

  const getErrorConfig = () => {
    switch (error) {
      case 'backend_unreachable':
        return {
          icon: <Server size={48} className="text-orange-400" />,
          title: t('error.backend_title') || 'Server is starting up',
          description: t('error.backend_desc') || 'The local server is taking a moment to start. This usually resolves itself.',
          showManual: false,
        };
      case 'no_server_found':
        return {
          icon: <WifiOff size={48} className="text-red-400" />,
          title: t('error.no_server_title') || "Can't find a server",
          description: t('error.no_server_desc') || 'Make sure Helen Server is running on a device connected to the same WiFi network.',
          showManual: true,
        };
      case 'session_expired':
        return {
          icon: <AlertTriangle size={48} className="text-yellow-400" />,
          title: t('error.session_title') || 'Session expired',
          description: t('error.session_desc') || 'Your previous session has expired. Please sign in again.',
          showManual: false,
        };
      case 'network_offline':
        return {
          icon: <WifiOff size={48} className="text-red-400" />,
          title: t('error.offline_title') || 'No network connection',
          description: t('error.offline_desc') || 'Check your WiFi connection and try again.',
          showManual: false,
        };
      default:
        return {
          icon: <AlertTriangle size={48} className="text-gray-400" />,
          title: t('error.unknown_title') || 'Something went wrong',
          description: errorMessage || t('error.unknown_desc') || 'An unexpected error occurred.',
          showManual: true,
        };
    }
  };

  const config = getErrorConfig();
  const canAutoRetry = retryCount < maxRetries;

  return (
    <div
      className={`fixed inset-0 z-[90] flex items-center justify-center bg-gradient-to-br from-surface-950 via-surface-900 to-surface-950 select-none transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      <div className="w-full max-w-md px-6">
        <div className="flex flex-col items-center gap-6">
          {/* Icon */}
          {config.icon}

          {/* Title + description */}
          <div className="text-center">
            <h2 className="text-2xl font-bold text-white mb-2">{config.title}</h2>
            <p className="text-gray-400 text-base">{config.description}</p>
          </div>

          {/* Auto-retry countdown */}
          {canAutoRetry && autoRetrySeconds > 0 && (
            <p className="text-sm text-gray-500">
              {t('error.auto_retry') || 'Retrying automatically in'} {autoRetrySeconds}s
            </p>
          )}

          {/* Retry button */}
          <button
            onClick={onRetry}
            className="w-full py-4 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-colors flex items-center justify-center gap-3 text-lg"
          >
            <RefreshCw size={20} />
            {t('error.retry') || 'Try again'}
          </button>

          {/* Session expired → go to login */}
          {error === 'session_expired' && (
            <button
              onClick={onGoToLogin}
              className="w-full py-3 bg-surface-800 hover:bg-surface-700 text-gray-300 font-medium rounded-xl transition-colors text-base"
            >
              {t('error.go_to_login') || 'Sign in'}
            </button>
          )}

          {/* Advanced: Manual server entry */}
          {config.showManual && (
            <div className="w-full">
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="w-full flex items-center justify-center gap-1 text-xs text-gray-500 hover:text-gray-400 transition-colors"
              >
                {showAdvanced ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                {t('error.manual_entry') || 'Enter server address manually'}
              </button>

              {showAdvanced && (
                <div className="mt-3 p-3 bg-surface-800 rounded-xl border border-surface-700">
                  <label className="block text-xs font-medium text-gray-400 mb-2">
                    {t('auth.server_url') || 'Server Address'}
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="url"
                      value={manualUrl}
                      onChange={(e) => setManualUrl(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && manualUrl) onManualConnect(manualUrl);
                      }}
                      placeholder="http://192.168.1.100:3000"
                      className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                      autoFocus
                    />
                    <button
                      onClick={() => { if (manualUrl) onManualConnect(manualUrl); }}
                      className="px-4 py-2 bg-surface-700 hover:bg-surface-600 text-gray-300 rounded-lg text-sm transition-colors"
                    >
                      {t('login.connect') || 'Connect'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Retry count */}
          {retryCount > 0 && (
            <p className="text-xs text-gray-700">
              {t('error.attempt') || 'Attempt'} {retryCount} / {maxRetries}
            </p>
          )}

          {/* Last-resort reset: clears saved server URL + session and
           *  reloads. Useful when a stale URL in localStorage is pointing
           *  to an unreachable host and retry keeps hitting the same
           *  dead address. */}
          {retryCount >= maxRetries && (
            <button
              onClick={() => {
                try {
                  localStorage.removeItem('commclient_server_url');
                  localStorage.removeItem('commclient_auth_tokens');
                } catch { /* ignore */ }
                window.location.reload();
              }}
              className="text-xs text-gray-600 hover:text-gray-400 underline transition-colors"
            >
              {t('error.reset_and_reload') || 'Reset connection and reload'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default ErrorRecovery;
