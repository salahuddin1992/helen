/**
 * ServerDiscoveryScreen.tsx — Auto-detect server + create/join room.
 *
 * Goals:
 *   - Auto-discover LAN servers silently (user sees a friendly animation)
 *   - If found: show server info, let user proceed
 *   - If not found: offer manual entry or "create server" guidance
 *   - Create/Join room choice (for first-time social context)
 *   - No IP addresses or ports shown in simple view
 *   - Everything feels automatic and effortless
 *
 * Discovery Flow:
 *   searching → found / not_found
 *   found → show server card → auto-select → continue
 *   not_found → show help + manual entry option
 *
 * Integration:
 *   Uses the existing discoveryStore (UDP broadcast + mDNS).
 *   Also tries localhost:3000 as fallback for self-hosted setups.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Wifi, WifiOff, Server, Search, Check, ChevronDown, ChevronUp,
  ArrowRight, ArrowLeft, Plus, Users, Loader2, Globe,
} from 'lucide-react';
import { t } from '@/i18n';
import { useOnboardingStore } from '@/stores/onboarding.store';
import { useDiscoveryStore, isServerCode } from '@/stores/discovery.store';

// ── Server Card Component ───────────────────────────────────

interface ServerCardProps {
  url: string;
  name: string;
  userCount: number;
  isSelected: boolean;
  onSelect: () => void;
}

const ServerCard: React.FC<ServerCardProps> = ({
  url,
  name,
  userCount,
  isSelected,
  onSelect,
}) => (
  <button
    onClick={onSelect}
    className={`w-full p-4 rounded-xl border transition-all duration-200 text-start ${
      isSelected
        ? 'border-blue-500/60 bg-blue-500/10 shadow-lg shadow-blue-500/5'
        : 'border-surface-700 bg-surface-800/50 hover:bg-surface-800 hover:border-surface-600'
    }`}
  >
    <div className="flex items-center gap-3">
      <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
        isSelected ? 'bg-blue-600/20' : 'bg-surface-700'
      }`}>
        <Server size={20} className={isSelected ? 'text-blue-400' : 'text-gray-500'} />
      </div>
      <div className="flex-1 min-w-0">
        <h3 className="text-sm font-semibold text-white truncate">{name}</h3>
        <p className="text-xs text-gray-500">
          {userCount > 0
            ? `${userCount} ${t('ob.disc_users_online')}`
            : t('ob.disc_no_users')
          }
        </p>
      </div>
      {isSelected && (
        <div className="w-6 h-6 rounded-full bg-blue-600 flex items-center justify-center shrink-0">
          <Check size={14} className="text-white" />
        </div>
      )}
    </div>
  </button>
);

// ── Searching Animation ─────────────────────────────────────

const SearchingAnimation: React.FC = () => (
  <div className="flex flex-col items-center gap-4 py-8">
    <div className="relative">
      <div className="w-20 h-20 rounded-full bg-blue-600/10 flex items-center justify-center">
        <Search size={32} className="text-blue-400 animate-pulse" />
      </div>
      <div className="absolute inset-0 rounded-full border-2 border-blue-400/20 animate-ping" style={{ animationDuration: '2s' }} />
    </div>
    <div className="text-center">
      <p className="text-base text-gray-300 font-medium">{t('ob.disc_searching')}</p>
      <p className="text-sm text-gray-500 mt-1">{t('ob.disc_searching_hint')}</p>
    </div>
  </div>
);

// ── Not Found State ─────────────────────────────────────────

interface NotFoundProps {
  onManualEntry: () => void;
}

const NotFoundState: React.FC<NotFoundProps> = ({ onManualEntry }) => (
  <div className="flex flex-col items-center gap-4 py-6">
    <div className="w-16 h-16 rounded-full bg-orange-500/10 flex items-center justify-center">
      <WifiOff size={28} className="text-orange-400" />
    </div>
    <div className="text-center">
      <p className="text-base text-gray-300 font-medium">{t('ob.disc_not_found')}</p>
      <p className="text-sm text-gray-500 mt-1 max-w-xs mx-auto leading-relaxed">
        {t('ob.disc_not_found_hint')}
      </p>
    </div>
    <button
      onClick={onManualEntry}
      className="mt-2 px-5 py-2.5 bg-surface-800 hover:bg-surface-700 text-gray-300 font-medium rounded-lg transition-colors text-sm flex items-center gap-2"
    >
      <Globe size={16} />
      {t('ob.disc_enter_manually')}
    </button>
  </div>
);

// ── Main Screen ─────────────────────────────────────────────

const ServerDiscoveryScreen: React.FC = () => {
  const {
    discoveryStatus,
    discoveredServers,
    selectedServerUrl,
    manualServerUrl,
    selectedLanguage,
    setDiscoveryStatus,
    addDiscoveredServer,
    selectServer,
    setManualServerUrl,
    nextStep,
    prevStep,
  } = useOnboardingStore();

  const discovery = useDiscoveryStore();

  const [fadeIn, setFadeIn] = useState(false);
  const [showManual, setShowManual] = useState(false);
  const [manualConnecting, setManualConnecting] = useState(false);
  const [manualError, setManualError] = useState('');

  useEffect(() => {
    const timer = setTimeout(() => setFadeIn(true), 50);
    return () => clearTimeout(timer);
  }, []);

  // ── Start discovery on mount ───────────────────────
  useEffect(() => {
    setDiscoveryStatus('searching');

    // Try localhost first (self-hosted scenario)
    const tryLocalhost = async () => {
      try {
        const res = await fetch('http://localhost:3000/api/health', {
          signal: AbortSignal.timeout(3000),
        });
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          addDiscoveredServer({
            url: 'http://127.0.0.1:3000',
            name: data.serverName || t('ob.disc_local_server'),
            userCount: data.onlineUsers || 0,
            verified: true,
          });
          setDiscoveryStatus('found');
          selectServer('http://127.0.0.1:3000');
        }
      } catch {
        // localhost not available, continue with network discovery
      }
    };

    tryLocalhost();

    // Start LAN discovery via the existing discovery store
    discovery.startSearching();

    // Timeout: if nothing found in 8 seconds, show not_found
    const timeout = setTimeout(() => {
      if (discoveredServers.length === 0) {
        setDiscoveryStatus('not_found');
      }
    }, 8000);

    return () => {
      clearTimeout(timeout);
      discovery.stopSearching();
    };
  }, []);

  // Watch discovery store for found servers
  useEffect(() => {
    if (discovery.bestServer?.verified && discovery.bestServer.url) {
      addDiscoveredServer({
        url: discovery.bestServer.url,
        name: discovery.bestServer.name || 'Helen Server',
        userCount: 0,
        verified: true,
      });
      setDiscoveryStatus('found');
      if (!selectedServerUrl) {
        selectServer(discovery.bestServer.url);
      }
    }
  }, [discovery.bestServer?.verified, discovery.bestServer?.url]);

  // ── Manual server entry ────────────────────────────
  // Accepts either a URL (http://ip:port) or a 64-char server code. The
  // code path goes through the main-process UDP matcher so the user gets
  // auto-resolution to the right IP without knowing the LAN topology.
  const handleManualConnect = useCallback(async () => {
    const raw = manualServerUrl.trim();
    if (!raw) return;

    setManualConnecting(true);
    setManualError('');

    try {
      if (isServerCode(raw)) {
        const server = await useDiscoveryStore.getState().findServerByCode(raw);
        if (server && server.url) {
          addDiscoveredServer({
            url: server.url,
            name: server.name || server.url,
            userCount: server.users_online || 0,
            verified: !!server.verified,
          });
          setDiscoveryStatus('found');
          selectServer(server.url);
          setShowManual(false);
        } else {
          setManualError(t('ob.disc_code_not_found'));
        }
        return;
      }

      const normalized = raw.replace(/\/+$/, '');
      const res = await fetch(`${normalized}/api/health`, {
        signal: AbortSignal.timeout(5000),
      });

      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        addDiscoveredServer({
          url: normalized,
          name: data.serverName || normalized,
          userCount: data.onlineUsers || 0,
          verified: true,
        });
        setDiscoveryStatus('found');
        selectServer(normalized);
        setShowManual(false);
      } else {
        setManualError(t('ob.disc_manual_error'));
      }
    } catch {
      setManualError(t('ob.disc_manual_unreachable'));
    } finally {
      setManualConnecting(false);
    }
  }, [manualServerUrl]);

  const handleContinue = () => {
    if (selectedServerUrl) {
      // AlertTriangle server URL for the registration step
      try {
        localStorage.setItem('commclient_server_url', selectedServerUrl);
      } catch {}
      nextStep();
    }
  };

  const isRTL = selectedLanguage === 'ar';
  const ArrowForward = isRTL ? ArrowLeft : ArrowRight;

  return (
    <div
      className={`flex flex-col items-center min-h-screen px-6 py-10 transition-opacity duration-500 ${
        fadeIn ? 'opacity-100' : 'opacity-0'
      }`}
    >
      {/* ── Header ─────────────────────────────── */}
      <div className="w-14 h-14 rounded-full bg-blue-600/15 flex items-center justify-center mb-5">
        <Wifi size={28} className="text-blue-400" />
      </div>
      <div className="text-center mb-8">
        <h1 className="text-2xl font-bold text-white mb-2">
          {t('ob.disc_title')}
        </h1>
        <p className="text-gray-400 text-base max-w-sm mx-auto">
          {t('ob.disc_subtitle')}
        </p>
      </div>

      {/* ── Discovery Content ──────────────────── */}
      <div className="w-full max-w-sm">
        {/* Searching state */}
        {discoveryStatus === 'searching' && discoveredServers.length === 0 && (
          <SearchingAnimation />
        )}

        {/* Not found state */}
        {discoveryStatus === 'not_found' && discoveredServers.length === 0 && (
          <NotFoundState onManualEntry={() => setShowManual(true)} />
        )}

        {/* Found servers */}
        {discoveredServers.length > 0 && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              <p className="text-sm text-gray-400 font-medium">
                {t('ob.disc_found')} ({discoveredServers.length})
              </p>
            </div>
            {discoveredServers.map((server) => (
              <ServerCard
                key={server.url}
                url={server.url}
                name={server.name}
                userCount={server.userCount}
                isSelected={selectedServerUrl === server.url}
                onSelect={() => selectServer(server.url)}
              />
            ))}
          </div>
        )}

        {/* Manual entry (collapsible) */}
        {(showManual || discoveryStatus === 'not_found') && (
          <div className="mt-4">
            {discoveredServers.length > 0 && (
              <button
                onClick={() => setShowManual(!showManual)}
                className="w-full flex items-center justify-center gap-1 text-xs text-gray-500 hover:text-gray-400 transition-colors mb-3"
              >
                {showManual ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                {t('ob.disc_enter_manually')}
              </button>
            )}

            <div className="p-4 bg-surface-800/70 rounded-xl border border-surface-700">
              <label className="block text-xs font-medium text-gray-400 mb-2">
                {t('ob.disc_manual_label')}
              </label>
              <div className="flex gap-2">
                <input
                  type="url"
                  value={manualServerUrl}
                  onChange={(e) => {
                    setManualServerUrl(e.target.value);
                    setManualError('');
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleManualConnect();
                  }}
                  placeholder="http://192.168.1.100:3000  —  or paste 64-char server code"
                  className="flex-1 px-3 py-2.5 bg-surface-900 border border-surface-700 rounded-lg text-white placeholder-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  autoFocus={discoveryStatus === 'not_found'}
                />
                <button
                  onClick={handleManualConnect}
                  disabled={manualConnecting || !manualServerUrl.trim()}
                  className="px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 disabled:text-gray-500 text-white rounded-lg text-sm transition-colors flex items-center gap-1.5"
                >
                  {manualConnecting ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <ArrowForward size={14} />
                  )}
                </button>
              </div>
              {manualError && (
                <p className="mt-2 text-xs text-red-400">{manualError}</p>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ── Navigation ─────────────────────────── */}
      <div className="w-full max-w-sm mt-auto pt-8 space-y-3">
        {selectedServerUrl && (
          <button
            onClick={handleContinue}
            className="w-full py-4 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl transition-all flex items-center justify-center gap-3 text-lg shadow-lg shadow-blue-600/20"
          >
            {t('ob.continue')}
            <ArrowForward size={20} />
          </button>
        )}

        <button
          onClick={prevStep}
          className="w-full py-2.5 text-gray-500 hover:text-gray-300 text-sm transition-colors"
        >
          {t('ob.back')}
        </button>
      </div>
    </div>
  );
};

export default ServerDiscoveryScreen;
