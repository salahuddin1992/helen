/**
 * ConnectionOverlay.tsx — Full-screen reconnection/disconnection overlay.
 *
 * Displays connection states: connecting, reconnecting, disconnected, server_shutdown,
 * network_offline with appropriate messaging, retry capability, and discovery integration.
 */

import React from 'react';
import { Wifi, WifiOff, AlertCircle, Server, RefreshCw } from 'lucide-react';
import { t } from '@/i18n';

interface ConnectionOverlayProps {
  status: 'connected' | 'connecting' | 'reconnecting' | 'disconnected' | 'server_shutdown' | 'network_offline';
  attempt?: number;
  onRetry?: () => void;
  onRestartDiscovery?: () => void;
}

const ConnectionOverlay: React.FC<ConnectionOverlayProps> = ({
  status,
  attempt = 0,
  onRetry,
  onRestartDiscovery,
}) => {
  // Only show overlay if not connected
  if (status === 'connected') {
    return null;
  }

  const getConfig = () => {
    switch (status) {
      case 'connecting':
        return {
          icon: <Wifi size={48} className="text-blue-400 animate-pulse" />,
          title: t('connection.connecting') || 'Connecting...',
          subtitle: t('login.searching') || 'Establishing connection',
          showRetry: false,
          showRestartDiscovery: false,
          hint: '',
        };
      case 'reconnecting':
        return {
          icon: <Wifi size={48} className="text-yellow-400 animate-pulse" />,
          title: t('connection.reconnecting') || 'Reconnecting...',
          subtitle: attempt > 0 ? `${t('common.retry') || 'Attempt'} ${attempt}` : '',
          showRetry: false,
          showRestartDiscovery: false,
          hint: t('network.reconnecting') || 'Attempting to reconnect... Do not close the application',
        };
      case 'disconnected':
        return {
          icon: <WifiOff size={48} className="text-red-400" />,
          title: t('connection.lost') || 'Connection Lost',
          subtitle: t('common.no_connection') || 'Unable to reach the server',
          showRetry: true,
          showRestartDiscovery: true,
          hint: t('network.check_wifi') || 'Check your network connection and try again',
        };
      case 'network_offline':
        return {
          icon: <WifiOff size={48} className="text-red-400" />,
          title: t('network.offline') || 'No network connection',
          subtitle: t('network.wifi_lost') || 'WiFi connection lost',
          showRetry: false,
          showRestartDiscovery: true,
          hint: t('network.check_wifi') || 'Check your WiFi connection and try again',
        };
      case 'server_shutdown':
        return {
          icon: <Server size={48} className="text-orange-400" />,
          title: t('connection.serverRestarting') || 'Server Restarting',
          subtitle: '',
          showRetry: false,
          showRestartDiscovery: false,
          hint: t('network.restored') || 'Your connection will be restored shortly',
        };
      default:
        return {
          icon: <AlertCircle size={48} className="text-gray-400" />,
          title: t('common.error') || 'Connection Issue',
          subtitle: '',
          showRetry: true,
          showRestartDiscovery: false,
          hint: '',
        };
    }
  };

  const config = getConfig();

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="flex flex-col items-center gap-6 p-8 max-w-md">
        {/* Icon */}
        <div className="flex justify-center">{config.icon}</div>

        {/* Title */}
        <div className="text-center">
          <h2 className="text-2xl font-bold text-text-100 mb-2">{config.title}</h2>
          {config.subtitle && <p className="text-text-400">{config.subtitle}</p>}
        </div>

        {/* Spinner for connecting/reconnecting */}
        {(status === 'connecting' || status === 'reconnecting' || status === 'server_shutdown') && (
          <div className="flex gap-1 justify-center items-end h-8">
            <div
              className="w-1 bg-primary-500 rounded-full animate-bounce"
              style={{ animationDelay: '0ms' }}
            />
            <div
              className="w-1 bg-primary-500 rounded-full animate-bounce"
              style={{ animationDelay: '150ms' }}
            />
            <div
              className="w-1 bg-primary-500 rounded-full animate-bounce"
              style={{ animationDelay: '300ms' }}
            />
          </div>
        )}

        {/* Action buttons */}
        <div className="flex flex-col gap-3 w-full">
          {/* Retry button */}
          {config.showRetry && onRetry && (
            <button
              onClick={onRetry}
              className="px-6 py-3 bg-primary-600 hover:bg-primary-700 text-white font-medium rounded-lg transition-colors duration-200 w-full"
            >
              {t('common.retry') || 'Try Again'}
            </button>
          )}

          {/* Restart discovery button */}
          {config.showRestartDiscovery && onRestartDiscovery && (
            <button
              onClick={onRestartDiscovery}
              className="px-6 py-3 bg-surface-700 hover:bg-surface-600 text-gray-300 font-medium rounded-lg transition-colors duration-200 w-full flex items-center justify-center gap-2"
            >
              <RefreshCw size={16} />
              {t('login.searching') ? t('login.searching').split('...')[0] : 'Search for server'}
            </button>
          )}
        </div>

        {/* Hint text */}
        {config.hint && (
          <p className="text-xs text-text-500 text-center">{config.hint}</p>
        )}
      </div>
    </div>
  );
};

export default ConnectionOverlay;
