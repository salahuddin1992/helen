/**
 * AdvancedSettingsView — Extended settings for Advanced/Admin Mode.
 *
 * Includes everything from Simple Mode PLUS:
 *   - Manual server URL / port configuration
 *   - AlertCircle diagnostics (ping, latency, connected peers)
 *   - Live logs viewer
 *   - Storage & cache management
 *   - Backup & restore (export/import settings + contacts)
 *   - Discovery protocol details
 *   - Media device testing
 *   - Auto-lock timeout config
 *   - PIN management (change PIN)
 *   - Switch back to Simple Mode
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Server, Wifi as WifiIcon, FileText, AlertCircle, Download, Upload,
  Lock, ChevronDown, ChevronUp, Wifi,
  RefreshCw, Trash2, Copy, Check, AlertTriangle, Eye,
  Clock, ArrowLeft, Settings,
  Volume2, Mic, Camera, Volume2 as SpeakerIcon, Globe, Moon, Sun,
  Bell, BellOff, LogOut, Edit, X,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { useSettingsStore } from '@/stores/settings.store';
import { useAppModeStore } from '@/stores/app-mode.store';
import { useDiscoveryStore } from '@/stores/discovery.store';
import { t, setLanguage } from '@/i18n';

// ── Section Collapse State ──────────────────────────────

const useCollapse = (initial = false) => {
  const [open, setOpen] = useState(initial);
  return { open, toggle: () => setOpen(!open) };
};

// ── Main Component ──────────────────────────────────────

const AdvancedSettingsView: React.FC = () => {
  const { user, logout, serverUrl, setServerUrl: setAuthServerUrl,
          rendezvousUrl, setRendezvousUrl } = useAuthStore();
  const settings = useSettingsStore((s) => s.settings);
  const updateSettings = useSettingsStore((s) => s.update);
  const lockAdvanced = useAppModeStore((s) => s.lockAdvanced);
  const verifyPin = useAppModeStore((s) => s.verifyPin);
  const setPin = useAppModeStore((s) => s.setPin);
  const autoLockTimeout = useAppModeStore((s) => s.autoLockTimeout);

  // Section states
  const serverSection = useCollapse(true);
  const networkSection = useCollapse(false);
  const logsSection = useCollapse(false);
  const storageSection = useCollapse(false);
  const backupSection = useCollapse(false);
  const securitySection = useCollapse(false);
  const mediaSection = useCollapse(false);

  // Server config
  const [editServerUrl, setEditServerUrl] = useState(serverUrl);
  const [serverStatus, setServerStatus] = useState<'unknown' | 'checking' | 'online' | 'offline'>('unknown');
  // Rendezvous tunnel config — lets the client reach the server across
  // networks / NAT. Verified by probing /api/health through the URL.
  const [editRendezvousUrl, setEditRendezvousUrl] = useState(rendezvousUrl);
  const [rendezvousStatus, setRendezvousStatus] = useState<'unknown' | 'checking' | 'online' | 'offline'>('unknown');

  // Logs
  const [logs, setLogs] = useState<string[]>([]);
  const logsEndRef = useRef<HTMLDivElement>(null);

  // AlertCircle diagnostics
  const [pingResult, setPingResult] = useState<string>('');
  const [isPinging, setIsPinging] = useState(false);

  // Storage
  const [storageInfo, setStorageInfo] = useState({ cache: '0 MB', data: '0 MB', total: '0 MB' });

  // PIN change
  const [showPinChange, setShowPinChange] = useState(false);
  const [oldPin, setOldPin] = useState('');
  const [newPin, setNewPin] = useState('');
  const [pinError, setPinError] = useState('');
  const [pinSuccess, setPinSuccess] = useState(false);

  // Copy feedback
  const [copied, setCopied] = useState('');

  // ── Server Health Check ─────────────────────────────

  const checkServerHealth = useCallback(async () => {
    setServerStatus('checking');
    try {
      const url = editServerUrl || serverUrl;
      const resp = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(3000) });
      setServerStatus(resp.ok ? 'online' : 'offline');
    } catch {
      setServerStatus('offline');
    }
  }, [editServerUrl, serverUrl]);

  useEffect(() => { checkServerHealth(); }, []);

  // ── Ping Test ────────────────────────────────────────

  const runPingTest = useCallback(async () => {
    setIsPinging(true);
    setPingResult('');
    try {
      const url = editServerUrl || serverUrl;
      const times: number[] = [];
      for (let i = 0; i < 5; i++) {
        const start = performance.now();
        await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(3000) });
        times.push(performance.now() - start);
      }
      const avg = times.reduce((a, b) => a + b, 0) / times.length;
      const min = Math.min(...times);
      const max = Math.max(...times);
      setPingResult(`5 requests → avg ${avg.toFixed(1)}ms, min ${min.toFixed(1)}ms, max ${max.toFixed(1)}ms`);
    } catch {
      setPingResult('Ping failed — server unreachable');
    }
    setIsPinging(false);
  }, [editServerUrl, serverUrl]);

  // ── Log Capture ──────────────────────────────────────

  useEffect(() => {
    if (!logsSection.open) return;

    const originalLog = console.log;
    const originalWarn = console.warn;
    const originalError = console.error;

    const capture = (level: string, ...args: any[]) => {
      const msg = `[${new Date().toISOString().slice(11, 23)}] [${level}] ${args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')}`;
      setLogs(prev => [...prev.slice(-200), msg]);
    };

    console.log = (...args) => { originalLog(...args); capture('INFO', ...args); };
    console.warn = (...args) => { originalWarn(...args); capture('WARN', ...args); };
    console.error = (...args) => { originalError(...args); capture('ERROR', ...args); };

    return () => {
      console.log = originalLog;
      console.warn = originalWarn;
      console.error = originalError;
    };
  }, [logsSection.open]);

  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // ── Storage Info ─────────────────────────────────────

  useEffect(() => {
    if (!storageSection.open) return;
    try {
      let totalSize = 0;
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key) {
          totalSize += (localStorage.getItem(key) || '').length;
        }
      }
      const kb = (totalSize / 1024).toFixed(1);
      setStorageInfo({
        cache: `${kb} KB`,
        data: `${kb} KB`,
        total: `${(totalSize / 1024).toFixed(1)} KB`,
      });
    } catch {}
  }, [storageSection.open]);

  // ── Copy to Clipboard ────────────────────────────────

  const copyToClipboard = (text: string, key: string) => {
    navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(''), 2000);
  };

  // ── Apply Server URL ─────────────────────────────────

  const applyServerUrl = () => {
    try {
      new URL(editServerUrl); // validate
      setAuthServerUrl(editServerUrl);
      checkServerHealth();
    } catch {
      // invalid URL
    }
  };

  // ── Rendezvous tunnel URL ────────────────────────────
  // The tunnel URL (e.g. http://rendezvous.example:9090/t/<public_id>) is
  // saved independently of the primary server URL so the refresh flow can
  // fall through to it when LAN discovery comes up empty. Probing hits
  // /api/health, which routes through the rendezvous to the real server.

  const checkRendezvousHealth = useCallback(async () => {
    const url = (editRendezvousUrl || '').trim().replace(/\/+$/, '');
    if (!url) { setRendezvousStatus('unknown'); return; }
    setRendezvousStatus('checking');
    try {
      const resp = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(5000) });
      setRendezvousStatus(resp.ok ? 'online' : 'offline');
    } catch {
      setRendezvousStatus('offline');
    }
  }, [editRendezvousUrl]);

  const applyRendezvousUrl = () => {
    const trimmed = (editRendezvousUrl || '').trim().replace(/\/+$/, '');
    if (trimmed === '') {
      setRendezvousUrl('');
      setRendezvousStatus('unknown');
      return;
    }
    try {
      new URL(trimmed); // validate
      setRendezvousUrl(trimmed);
      checkRendezvousHealth();
    } catch { /* invalid URL */ }
  };

  // ── Export Backup ────────────────────────────────────

  const exportBackup = () => {
    try {
      const backup: Record<string, string | null> = {};
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.startsWith('commclient_')) {
          backup[key] = localStorage.getItem(key);
        }
      }
      const blob = new Blob([JSON.stringify(backup, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `commclient-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Backup export failed:', err);
    }
  };

  // ── Import Backup ────────────────────────────────────

  const importBackup = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const data = JSON.parse(text);
        for (const [key, value] of Object.entries(data)) {
          if (key.startsWith('commclient_') && typeof value === 'string') {
            localStorage.setItem(key, value);
          }
        }
        window.location.reload();
      } catch (err) {
        console.error('Backup import failed:', err);
      }
    };
    input.click();
  };

  // ── Clear Cache ──────────────────────────────────────

  const clearCache = () => {
    const keysToKeep = ['commclient_auth', 'commclient_admin_pin', 'commclient_app_mode'];
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const key = localStorage.key(i);
      if (key && key.startsWith('commclient_') && !keysToKeep.includes(key)) {
        localStorage.removeItem(key);
      }
    }
    setStorageInfo({ cache: '0 KB', data: '0 KB', total: '0 KB' });
  };

  // ── Change PIN ───────────────────────────────────────

  const handleChangePin = () => {
    setPinError('');
    setPinSuccess(false);
    if (!verifyPin(oldPin)) {
      setPinError(t('mode.pin_wrong'));
      return;
    }
    if (newPin.length < 4 || !/^\d+$/.test(newPin)) {
      setPinError(t('mode.pin_too_short'));
      return;
    }
    setPin(newPin);
    setPinSuccess(true);
    setOldPin('');
    setNewPin('');
    setTimeout(() => { setPinSuccess(false); setShowPinChange(false); }, 2000);
  };

  // ── Server Status Indicator ──────────────────────────

  const statusDot = serverStatus === 'online' ? 'bg-green-500' :
                    serverStatus === 'offline' ? 'bg-red-500' :
                    serverStatus === 'checking' ? 'bg-yellow-500 animate-pulse' :
                    'bg-gray-500';

  const statusLabel = serverStatus === 'online' ? t('mode.server_online') :
                      serverStatus === 'offline' ? t('mode.server_offline') :
                      serverStatus === 'checking' ? t('mode.server_checking') :
                      t('mode.server_unknown');

  return (
    <div className="w-full h-full bg-surface-950 overflow-y-auto">
      <div className="max-w-2xl mx-auto py-6 px-4">

        {/* Header with mode badge and lock button */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-text-100">{t('nav.settings')}</h1>
            <span className="px-2.5 py-1 text-xs font-semibold bg-amber-600/20 text-amber-400 rounded-full border border-amber-600/30">
              {t('mode.advanced')}
            </span>
          </div>
          <button
            onClick={lockAdvanced}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-surface-800 hover:bg-surface-700 text-text-300 rounded-lg transition-colors"
          >
            <Lock size={14} />
            {t('mode.switch_to_simple')}
          </button>
        </div>

        {/* ═══ SERVER CONFIGURATION ═══════════════════════ */}
        <CollapsibleSection
          title={t('mode.server_config')}
          icon={<Server size={20} />}
          {...serverSection}
          badge={<span className={`w-2.5 h-2.5 rounded-full ${statusDot}`} />}
        >
          <div className="space-y-4">
            {/* Server URL */}
            <div>
              <label className="block text-sm font-medium text-text-300 mb-2">{t('mode.server_url')}</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={editServerUrl}
                  onChange={(e) => setEditServerUrl(e.target.value)}
                  className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  placeholder="http://192.168.1.100:3000"
                />
                <button onClick={applyServerUrl} className="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors">
                  {t('mode.apply')}
                </button>
              </div>
            </div>

            {/* Server Status */}
            <div className="flex items-center justify-between p-3 bg-surface-800 rounded-lg">
              <div className="flex items-center gap-2">
                <span className={`w-2.5 h-2.5 rounded-full ${statusDot}`} />
                <span className="text-sm text-text-200">{statusLabel}</span>
              </div>
              <button onClick={checkServerHealth} className="p-1.5 hover:bg-surface-700 rounded-lg text-text-400 transition-colors">
                <RefreshCw size={14} className={serverStatus === 'checking' ? 'animate-spin' : ''} />
              </button>
            </div>

            {/* Server URL copy */}
            <div className="flex items-center gap-2 text-xs text-text-500">
              <span className="font-mono">{serverUrl}</span>
              <button onClick={() => copyToClipboard(serverUrl, 'url')} className="p-1 hover:bg-surface-800 rounded">
                {copied === 'url' ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
              </button>
            </div>

            {/* Rendezvous tunnel URL — cross-network fallback */}
            <div className="pt-3 border-t border-surface-800">
              <label className="block text-sm font-medium text-text-300 mb-1">
                Rendezvous tunnel (cross-network fallback)
              </label>
              <p className="text-xs text-text-500 mb-2">
                عنوان نفق Helen-Rendezvous (VPS عام). يُستخدم تلقائياً حين يفشل اكتشاف الشبكة المحلية.
                مثال: <code className="font-mono text-[10px]">http://your-vps:9090/t/&lt;public_id&gt;</code>
              </p>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={editRendezvousUrl}
                  onChange={(e) => setEditRendezvousUrl(e.target.value)}
                  className="flex-1 px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500/50"
                  placeholder="http://rendezvous.example:9090/t/abc123def"
                />
                <button onClick={applyRendezvousUrl} className="px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors">
                  {t('mode.apply')}
                </button>
                <button onClick={checkRendezvousHealth} className="px-3 py-2 bg-surface-800 hover:bg-surface-700 text-white text-sm rounded-lg transition-colors" title="اختبار الاتصال">
                  <RefreshCw size={14} className={rendezvousStatus === 'checking' ? 'animate-spin' : ''} />
                </button>
              </div>
              <div className="flex items-center gap-2 mt-2 text-xs">
                <span className={`w-2 h-2 rounded-full ${
                  rendezvousStatus === 'online' ? 'bg-green-500' :
                  rendezvousStatus === 'offline' ? 'bg-red-500' :
                  rendezvousStatus === 'checking' ? 'bg-yellow-500' : 'bg-gray-500'}`} />
                <span className="text-text-400 font-mono">
                  {rendezvousStatus === 'online' ? 'tunnel reachable · /api/health = ok' :
                   rendezvousStatus === 'offline' ? 'tunnel unreachable' :
                   rendezvousStatus === 'checking' ? 'probing…' :
                   rendezvousUrl ? `saved: ${rendezvousUrl}` : '(not configured)'}
                </span>
              </div>
            </div>
          </div>
        </CollapsibleSection>

        {/* ═══ NETWORK DIAGNOSTICS ════════════════════════ */}
        <CollapsibleSection
          title={t('mode.network_diagnostics')}
          icon={<AlertCircle size={20} />}
          {...networkSection}
        >
          <div className="space-y-4">
            {/* Ping Test */}
            <div>
              <button
                onClick={runPingTest}
                disabled={isPinging}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 text-white text-sm rounded-lg transition-colors"
              >
                <Wifi size={14} />
                {isPinging ? t('mode.pinging') : t('mode.run_ping')}
              </button>
              {pingResult && (
                <p className="mt-2 text-sm font-mono text-text-300 bg-surface-800 p-3 rounded-lg">{pingResult}</p>
              )}
            </div>

            {/* Discovery Info */}
            <div>
              <h4 className="text-sm font-medium text-text-300 mb-2">{t('mode.discovery_info')}</h4>
              <div className="space-y-1 text-xs font-mono text-text-400 bg-surface-800 p-3 rounded-lg">
                <p>UDP Broadcast Port: 41234</p>
                <p>mDNS Service: _commclient._tcp.local</p>
                <p>Server ID: {serverUrl ? `SHA256(${serverUrl})`.slice(0, 32) + '…' : 'N/A'}</p>
              </div>
            </div>
          </div>
        </CollapsibleSection>

        {/* ═══ LIVE LOGS ═════════════════════════════════ */}
        <CollapsibleSection
          title={t('mode.live_logs')}
          icon={<AlertCircle size={20} />}
          {...logsSection}
        >
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-text-500">{logs.length} {t('mode.entries')}</span>
              <div className="flex gap-2">
                <button
                  onClick={() => copyToClipboard(logs.join('\n'), 'logs')}
                  className="flex items-center gap-1 px-2 py-1 text-xs bg-surface-800 hover:bg-surface-700 text-text-400 rounded transition-colors"
                >
                  {copied === 'logs' ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
                  {t('mode.copy_logs')}
                </button>
                <button
                  onClick={() => setLogs([])}
                  className="flex items-center gap-1 px-2 py-1 text-xs bg-surface-800 hover:bg-surface-700 text-text-400 rounded transition-colors"
                >
                  <Trash2 size={12} />
                  {t('mode.clear_logs')}
                </button>
              </div>
            </div>
            <div className="h-64 overflow-y-auto bg-surface-950 border border-surface-700 rounded-lg p-3 font-mono text-xs text-text-400 leading-5">
              {logs.length === 0 ? (
                <p className="text-text-600 italic">{t('mode.logs_empty')}</p>
              ) : (
                logs.map((line, i) => (
                  <div key={i} className={`${
                    line.includes('[ERROR]') ? 'text-red-400' :
                    line.includes('[WARN]') ? 'text-yellow-400' :
                    'text-text-400'
                  }`}>
                    {line}
                  </div>
                ))
              )}
              <div ref={logsEndRef} />
            </div>
          </div>
        </CollapsibleSection>

        {/* ═══ STORAGE & CACHE ═══════════════════════════ */}
        <CollapsibleSection
          title={t('mode.storage')}
          icon={<AlertCircle size={20} />}
          {...storageSection}
        >
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3">
              <InfoCard label={t('mode.cache_size')} value={storageInfo.cache} />
              <InfoCard label={t('mode.data_size')} value={storageInfo.data} />
              <InfoCard label={t('mode.total_size')} value={storageInfo.total} />
            </div>
            <button
              onClick={clearCache}
              className="flex items-center gap-2 px-4 py-2 bg-red-600/20 hover:bg-red-600/30 text-red-400 text-sm rounded-lg transition-colors border border-red-900/30"
            >
              <Trash2 size={14} />
              {t('mode.clear_cache')}
            </button>
          </div>
        </CollapsibleSection>

        {/* ═══ BACKUP & RESTORE ══════════════════════════ */}
        <CollapsibleSection
          title={t('mode.backup_restore')}
          icon={<AlertCircle size={20} />}
          {...backupSection}
        >
          <div className="space-y-3">
            <p className="text-sm text-text-400">{t('mode.backup_desc')}</p>
            <div className="flex gap-3">
              <button
                onClick={exportBackup}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors"
              >
                <Download size={16} />
                {t('mode.export_backup')}
              </button>
              <button
                onClick={importBackup}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-3 bg-surface-800 hover:bg-surface-700 text-text-200 text-sm rounded-lg transition-colors border border-surface-700"
              >
                <Upload size={16} />
                {t('mode.import_backup')}
              </button>
            </div>
            <p className="text-xs text-text-600 flex items-center gap-1">
              <AlertTriangle size={12} />
              {t('mode.backup_warning')}
            </p>
          </div>
        </CollapsibleSection>

        {/* ═══ SECURITY (PIN) ════════════════════════════ */}
        <CollapsibleSection
          title={t('mode.security')}
          icon={<Lock size={20} />}
          {...securitySection}
        >
          <div className="space-y-4">
            {/* Auto-lock timeout */}
            <div>
              <label className="block text-sm font-medium text-text-300 mb-2">
                {t('mode.auto_lock_timeout')}
              </label>
              <p className="text-sm text-text-400">{Math.floor(autoLockTimeout / 60000)} {t('mode.minutes')}</p>
            </div>

            {/* Change PIN */}
            {showPinChange ? (
              <div className="space-y-3 p-4 bg-surface-800 rounded-lg">
                <input
                  type="password"
                  inputMode="numeric"
                  placeholder={t('mode.current_pin')}
                  value={oldPin}
                  onChange={(e) => setOldPin(e.target.value.replace(/\D/g, ''))}
                  maxLength={8}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-text-100 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500/50"
                />
                <input
                  type="password"
                  inputMode="numeric"
                  placeholder={t('mode.new_pin')}
                  value={newPin}
                  onChange={(e) => setNewPin(e.target.value.replace(/\D/g, ''))}
                  maxLength={8}
                  className="w-full px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-text-100 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500/50"
                />
                {pinError && <p className="text-xs text-red-400">{pinError}</p>}
                {pinSuccess && <p className="text-xs text-green-400">{t('mode.pin_changed')}</p>}
                <div className="flex gap-2">
                  <button onClick={handleChangePin} className="px-3 py-2 bg-amber-600 hover:bg-amber-700 text-white text-sm rounded-lg transition-colors">{t('common.save')}</button>
                  <button onClick={() => { setShowPinChange(false); setPinError(''); }} className="px-3 py-2 bg-surface-700 hover:bg-surface-600 text-text-200 text-sm rounded-lg transition-colors">{t('common.cancel')}</button>
                </div>
              </div>
            ) : (
              <button
                onClick={() => setShowPinChange(true)}
                className="flex items-center gap-2 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-text-200 text-sm rounded-lg transition-colors"
              >
                <Lock size={14} />
                {t('mode.change_pin')}
              </button>
            )}
          </div>
        </CollapsibleSection>

        {/* ═══ MEDIA DEVICES ═════════════════════════════ */}
        <CollapsibleSection
          title={t('mode.media_devices')}
          icon={<SpeakerIcon size={20} />}
          {...mediaSection}
        >
          <div className="space-y-4">
            <DeviceSelector
              icon={<Mic size={16} />}
              label={t('settings.audio_input')}
              currentDevice={settings.audioInputDevice}
              kind="audioinput"
              onSelect={(id) => updateSettings({ audioInputDevice: id })}
            />
            <DeviceSelector
              icon={<Volume2 size={16} />}
              label={t('settings.audio_output')}
              currentDevice={settings.audioOutputDevice}
              kind="audiooutput"
              onSelect={(id) => updateSettings({ audioOutputDevice: id })}
            />
            <DeviceSelector
              icon={<Camera size={16} />}
              label={t('settings.video_input')}
              currentDevice={settings.videoInputDevice}
              kind="videoinput"
              onSelect={(id) => updateSettings({ videoInputDevice: id })}
            />
          </div>
        </CollapsibleSection>

        {/* ═══ BASIC SETTINGS (theme, language, notifications) ════ */}
        <CollapsibleSection
          title={t('mode.basic_settings')}
          icon={<Settings size={20} />}
          open={true}
          toggle={() => {}}
        >
          <div className="space-y-3">
            <ToggleRow
              label={t('settings.theme')}
              value={settings.theme === 'dark' ? t('settings.dark') : t('settings.light')}
              onClick={() => updateSettings({ theme: settings.theme === 'dark' ? 'light' : 'dark' })}
            />
            <ToggleRow
              label={t('settings.language')}
              value={settings.language === 'en' ? 'English' : 'العربية'}
              onClick={() => { const next = settings.language === 'en' ? 'ar' as const : 'en' as const; updateSettings({ language: next }); setLanguage(next); }}
            />
            <ToggleRow
              label={t('settings.notifications')}
              value={settings.notifications ? t('mode.on') : t('mode.off')}
              onClick={() => updateSettings({ notifications: !settings.notifications })}
              valueColor={settings.notifications ? 'text-green-400' : 'text-red-400'}
            />
          </div>
        </CollapsibleSection>

        {/* Sign Out */}
        <button
          onClick={() => logout()}
          className="w-full flex items-center justify-center gap-2 p-4 bg-surface-900 border border-red-900/30 rounded-2xl text-red-400 hover:bg-red-600/10 font-medium transition-colors mb-8"
        >
          <LogOut size={18} />
          {t('settings.logout')}
        </button>

        <div className="h-8" />
      </div>
    </div>
  );
};

// ── Collapsible Section ─────────────────────────────────

const CollapsibleSection: React.FC<{
  title: string;
  icon: React.ReactNode;
  open: boolean;
  toggle: () => void;
  badge?: React.ReactNode;
  children: React.ReactNode;
}> = ({ title, icon, open, toggle, badge, children }) => (
  <div className="mb-4 bg-surface-900 border border-surface-800 rounded-2xl overflow-hidden">
    <button
      onClick={toggle}
      className="w-full flex items-center gap-3 px-5 py-4 hover:bg-surface-800/50 transition-colors text-left"
    >
      <span className="text-text-400">{icon}</span>
      <span className="flex-1 font-semibold text-text-100">{title}</span>
      {badge}
      {open ? <ChevronUp size={16} className="text-text-500" /> : <ChevronDown size={16} className="text-text-500" />}
    </button>
    {open && (
      <div className="px-5 pb-5 animate-fadeIn">
        {children}
      </div>
    )}
  </div>
);

// ── Info Card ───────────────────────────────────────────

const InfoCard: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="p-3 bg-surface-800 rounded-lg text-center">
    <p className="text-lg font-bold text-text-100">{value}</p>
    <p className="text-xs text-text-500">{label}</p>
  </div>
);

// ── Toggle Row ──────────────────────────────────────────

const ToggleRow: React.FC<{
  label: string;
  value: string;
  onClick: () => void;
  valueColor?: string;
}> = ({ label, value, onClick, valueColor = 'text-text-300' }) => (
  <button
    onClick={onClick}
    className="w-full flex items-center justify-between px-4 py-3 bg-surface-800 hover:bg-surface-700 rounded-lg transition-colors"
  >
    <span className="text-sm text-text-200">{label}</span>
    <span className={`text-sm font-medium ${valueColor}`}>{value}</span>
  </button>
);

// ── Device Selector ─────────────────────────────────────

const DeviceSelector: React.FC<{
  icon: React.ReactNode;
  label: string;
  currentDevice: string;
  kind: string;
  onSelect: (id: string) => void;
}> = ({ icon, label, currentDevice, kind, onSelect }) => {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);

  useEffect(() => {
    const load = async () => {
      try {
        const all = await navigator.mediaDevices.enumerateDevices();
        setDevices(all.filter(d => d.kind === kind));
      } catch {}
    };
    load();
    navigator.mediaDevices.addEventListener('devicechange', load);
    return () => navigator.mediaDevices.removeEventListener('devicechange', load);
  }, [kind]);

  return (
    <div>
      <label className="flex items-center gap-2 text-sm font-medium text-text-300 mb-2">
        {icon} {label}
      </label>
      <select
        value={currentDevice}
        onChange={(e) => onSelect(e.target.value)}
        className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/50"
      >
        <option value="default">Default</option>
        {devices.map(d => (
          <option key={d.deviceId} value={d.deviceId}>
            {d.label || `${kind} ${d.deviceId.slice(0, 8)}`}
          </option>
        ))}
      </select>
    </div>
  );
};

export default AdvancedSettingsView;
