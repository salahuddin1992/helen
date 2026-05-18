/**
 * AdvancedDashboard — Diagnostics and system overview page.
 *
 * Available ONLY in Advanced Mode. Shows:
 *   - Real-time connection status
 *   - Socket state and event counters
 *   - Memory usage
 *   - Active peers / participants
 *   - Quick actions (restart discovery, reconnect, clear cache)
 *   - System info (Electron version, Node version, platform)
 *
 * This is the /dashboard route, visible only in Advanced Mode nav.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  AlertCircle, Wifi, WifiOff, Users,
  RefreshCw, Trash2, AlertTriangle,
  Clock, Server,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { useAppModeStore } from '@/stores/app-mode.store';
import { useDiscoveryStore } from '@/stores/discovery.store';
import { socketManager } from '@/services/socket.manager';
import { t } from '@/i18n';

interface SystemMetrics {
  socketConnected: boolean;
  socketTransport: string;
  uptime: string;
  memoryUsed: string;
  platform: string;
  electronVersion: string;
  nodeVersion: string;
  serverUrl: string;
  networkStatus: string;
  discoveryState: string;
}

const AdvancedDashboard: React.FC = () => {
  const isAdvanced = useAppModeStore((s) => s.isAdvanced);
  const serverUrl = useAuthStore((s) => s.serverUrl);
  const networkStatus = useDiscoveryStore((s) => s.networkStatus);
  const discoveryPhase = useDiscoveryStore((s) => s.phase);
  const restartDiscovery = useDiscoveryStore((s) => s.restartDiscovery);

  const [metrics, setMetrics] = useState<SystemMetrics>({
    socketConnected: false,
    socketTransport: 'N/A',
    uptime: '0s',
    memoryUsed: '0 MB',
    platform: navigator.platform,
    electronVersion: 'N/A',
    nodeVersion: 'N/A',
    serverUrl: serverUrl || 'N/A',
    networkStatus: 'unknown',
    discoveryState: 'idle',
  });

  const [serverHealth, setServerHealth] = useState<Record<string, any> | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // ── Refresh Metrics ──────────────────────────────────

  const refreshMetrics = useCallback(async () => {
    setIsRefreshing(true);

    // Socket state
    const connected = socketManager.isConnected();

    // Performance memory (Chrome only)
    let memUsed = 'N/A';
    try {
      const perf = (performance as any).memory;
      if (perf) {
        memUsed = `${(perf.usedJSHeapSize / 1024 / 1024).toFixed(1)} MB`;
      }
    } catch {}

    // Electron/Node version via preload
    let electronVer = 'N/A';
    let nodeVer = 'N/A';
    try {
      const versions = (window as any).electronAPI?.versions;
      if (versions) {
        electronVer = await versions.electron?.() || versions.electron || 'N/A';
        nodeVer = await versions.node?.() || versions.node || 'N/A';
      }
    } catch {}

    // Uptime
    const uptimeMs = performance.now();
    const uptimeSec = Math.floor(uptimeMs / 1000);
    const hours = Math.floor(uptimeSec / 3600);
    const mins = Math.floor((uptimeSec % 3600) / 60);
    const secs = uptimeSec % 60;
    const uptimeStr = hours > 0 ? `${hours}h ${mins}m ${secs}s` : mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;

    // Server health
    try {
      const resp = await fetch(`${serverUrl}/api/health`, { signal: AbortSignal.timeout(3000) });
      if (resp.ok) {
        setServerHealth(await resp.json());
      }
    } catch {
      setServerHealth(null);
    }

    setMetrics({
      socketConnected: connected,
      socketTransport: connected ? 'WebSocket' : 'Disconnected',
      uptime: uptimeStr,
      memoryUsed: memUsed,
      platform: navigator.platform,
      electronVersion: electronVer,
      nodeVersion: nodeVer,
      serverUrl: serverUrl || 'N/A',
      networkStatus: networkStatus || 'unknown',
      discoveryState: discoveryPhase || 'idle',
    });

    setIsRefreshing(false);
  }, [serverUrl, networkStatus, discoveryPhase]);

  useEffect(() => {
    refreshMetrics();
    const interval = setInterval(refreshMetrics, 10_000);  // refresh every 10s
    return () => clearInterval(interval);
  }, [refreshMetrics]);

  if (!isAdvanced) {
    return (
      <div className="flex items-center justify-center h-full text-text-500">
        {t('mode.advanced_required')}
      </div>
    );
  }

  return (
    <div className="w-full h-full bg-surface-950 overflow-y-auto">
      <div className="max-w-3xl mx-auto py-6 px-4">

        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <AlertCircle size={24} className="text-amber-400" />
            <h1 className="text-2xl font-bold text-text-100">{t('mode.dashboard')}</h1>
          </div>
          <button
            onClick={refreshMetrics}
            disabled={isRefreshing}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-surface-800 hover:bg-surface-700 text-text-300 rounded-lg transition-colors"
          >
            <RefreshCw size={14} className={isRefreshing ? 'animate-spin' : ''} />
            {t('mode.refresh')}
          </button>
        </div>

        {/* ── Status Cards AlertCircle ─────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
          <StatusCard
            icon={<Wifi size={18} />}
            label={t('mode.socket')}
            value={metrics.socketConnected ? t('mode.connected') : t('mode.disconnected')}
            color={metrics.socketConnected ? 'green' : 'red'}
          />
          <StatusCard
            icon={<Server size={18} />}
            label={t('mode.server')}
            value={serverHealth ? t('mode.server_online') : t('mode.server_offline')}
            color={serverHealth ? 'green' : 'red'}
          />
          <StatusCard
            icon={<AlertTriangle size={18} />}
            label={t('mode.network')}
            value={metrics.networkStatus === 'online' ? t('mode.on') : metrics.networkStatus}
            color={metrics.networkStatus === 'online' ? 'green' : 'yellow'}
          />
          <StatusCard
            icon={<Clock size={18} />}
            label={t('mode.uptime')}
            value={metrics.uptime}
            color="blue"
          />
        </div>

        {/* ── System Info ───────────────────────────── */}
        <div className="mb-6 p-5 bg-surface-900 border border-surface-800 rounded-2xl">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-text-200 mb-4">
            <AlertCircle size={16} />
            {t('mode.system_info')}
          </h3>
          <div className="grid grid-cols-2 gap-y-3 gap-x-6 text-sm">
            <InfoPair label="Platform" value={metrics.platform} />
            <InfoPair label="Electron" value={metrics.electronVersion} />
            <InfoPair label="Node.js" value={metrics.nodeVersion} />
            <InfoPair label="Memory" value={metrics.memoryUsed} />
            <InfoPair label="Server URL" value={metrics.serverUrl} mono />
            <InfoPair label="Transport" value={metrics.socketTransport} />
            <InfoPair label="Discovery" value={metrics.discoveryState} />
            <InfoPair label="AlertCircle" value={metrics.networkStatus} />
          </div>
        </div>

        {/* ── Server Health (if available) ──────────── */}
        {serverHealth && (
          <div className="mb-6 p-5 bg-surface-900 border border-surface-800 rounded-2xl">
            <h3 className="flex items-center gap-2 text-sm font-semibold text-text-200 mb-4">
              <Server size={16} />
              {t('mode.server_health')}
            </h3>
            <pre className="text-xs font-mono text-text-400 bg-surface-950 border border-surface-700 rounded-lg p-3 overflow-x-auto">
              {JSON.stringify(serverHealth, null, 2)}
            </pre>
          </div>
        )}

        {/* ── Quick Actions ─────────────────────────── */}
        <div className="mb-6 p-5 bg-surface-900 border border-surface-800 rounded-2xl">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-text-200 mb-4">
            <AlertTriangle size={16} />
            {t('mode.quick_actions')}
          </h3>
          <div className="flex flex-wrap gap-3">
            <ActionButton
              icon={<RefreshCw size={14} />}
              label={t('mode.restart_discovery')}
              onClick={() => restartDiscovery?.()}
              color="blue"
            />
            <ActionButton
              icon={<RefreshCw size={14} />}
              label={t('mode.reconnect_socket')}
              onClick={() => {
                socketManager.disconnect();
                const token = useAuthStore.getState().tokens?.access_token;
                if (token) {
                  socketManager.connect(serverUrl, token);
                }
              }}
              color="green"
            />
            <ActionButton
              icon={<Trash2 size={14} />}
              label={t('mode.clear_cache')}
              onClick={() => {
                const keep = ['commclient_auth', 'commclient_admin_pin', 'commclient_app_mode'];
                for (let i = localStorage.length - 1; i >= 0; i--) {
                  const k = localStorage.key(i);
                  if (k && k.startsWith('commclient_') && !keep.includes(k)) localStorage.removeItem(k);
                }
              }}
              color="red"
            />
          </div>
        </div>

        <div className="h-8" />
      </div>
    </div>
  );
};

// ── Status Card ─────────────────────────────────────────

const colorMap: Record<string, string> = {
  green: 'border-green-600/30 bg-green-600/5',
  red: 'border-red-600/30 bg-red-600/5',
  yellow: 'border-yellow-600/30 bg-yellow-600/5',
  blue: 'border-blue-600/30 bg-blue-600/5',
};

const dotMap: Record<string, string> = {
  green: 'bg-green-500',
  red: 'bg-red-500',
  yellow: 'bg-yellow-500',
  blue: 'bg-blue-500',
};

const StatusCard: React.FC<{
  icon: React.ReactNode;
  label: string;
  value: string;
  color: string;
}> = ({ icon, label, value, color }) => (
  <div className={`p-4 rounded-xl border ${colorMap[color] || colorMap.blue}`}>
    <div className="flex items-center gap-2 text-text-400 mb-2">
      {icon}
      <span className="text-xs font-medium">{label}</span>
    </div>
    <div className="flex items-center gap-2">
      <span className={`w-2 h-2 rounded-full ${dotMap[color] || dotMap.blue}`} />
      <span className="text-sm font-semibold text-text-100">{value}</span>
    </div>
  </div>
);

// ── Info Pair ───────────────────────────────────────────

const InfoPair: React.FC<{ label: string; value: string; mono?: boolean }> = ({ label, value, mono }) => (
  <div>
    <span className="text-text-500 text-xs">{label}</span>
    <p className={`text-text-200 ${mono ? 'font-mono text-xs' : 'text-sm'} truncate`}>{value}</p>
  </div>
);

// ── Action Button ───────────────────────────────────────

const ActionButton: React.FC<{
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  color: string;
}> = ({ icon, label, onClick, color }) => {
  const colorClasses: Record<string, string> = {
    blue: 'bg-blue-600/20 hover:bg-blue-600/30 text-blue-400 border-blue-900/30',
    green: 'bg-green-600/20 hover:bg-green-600/30 text-green-400 border-green-900/30',
    red: 'bg-red-600/20 hover:bg-red-600/30 text-red-400 border-red-900/30',
  };

  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-colors ${colorClasses[color] || colorClasses.blue}`}
    >
      {icon}
      {label}
    </button>
  );
};

export default AdvancedDashboard;
