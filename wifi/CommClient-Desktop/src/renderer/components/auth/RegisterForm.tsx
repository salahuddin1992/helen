import React, { useState, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { Loader2, Wifi, WifiOff, Server, ChevronDown, ChevronUp } from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { useDiscoveryStore, DiscoveredServer } from '@/stores/discovery.store';
import { t } from '@/i18n';

export const RegisterForm: React.FC = () => {
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [manualUrl, setManualUrl] = useState('');

  const { register, isLoading, error, setServerUrl: setAuthServerUrl, clearError } =
    useAuthStore();
  const {
    servers,
    phase,
    bestServer,
    autoConnectUrl,
    isManualMode,
    startSearching,
    stopSearching,
    addManualServer,
    enableManualMode,
    disableManualMode,
  } = useDiscoveryStore();

  const [validationError, setValidationError] = useState<string | null>(null);

  // Start discovery on mount
  useEffect(() => {
    startSearching();
    return () => stopSearching();
  }, []);

  // Compute effective server URL
  const effectiveUrl = isManualMode
    ? manualUrl
    : autoConnectUrl || 'http://127.0.0.1:3000';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    setValidationError(null);

    // Client-side validation
    if (password !== confirmPassword) {
      setValidationError(t('register.password_mismatch') || 'Passwords do not match');
      return;
    }

    if (password.length < 6) {
      setValidationError(t('register.password_too_short') || 'Password must be at least 6 characters');
      return;
    }

    try {
      setAuthServerUrl(effectiveUrl);
      await register(username, displayName, password);
      navigate('/chats');
    } catch (e) {
      console.error('Registration failed:', e);
    }
  };

  const handleManualConnect = async () => {
    if (!manualUrl) return;
    const server = await addManualServer(manualUrl);
    if (server) {
      setManualUrl(server.url);
    }
  };

  // Discovery status indicator (same as LoginForm)
  const renderDiscoveryStatus = () => {
    if (isManualMode) return null;

    switch (phase) {
      case 'searching':
        return (
          <div className="flex items-center gap-2 text-sm text-blue-400 animate-pulse">
            <Wifi size={16} className="animate-pulse" />
            <span>{t('login.searching') || 'Looking for the server on your network...'}</span>
          </div>
        );
      case 'found':
        return (
          <div className="flex items-center gap-2 text-sm text-yellow-400">
            <Server size={16} />
            <span>{t('login.verifying') || 'Found a server, verifying...'}</span>
          </div>
        );
      case 'verified':
        return (
          <div className="flex items-center gap-2 text-sm text-green-400">
            <Wifi size={16} />
            <span>
              {bestServer
                ? `${t('login.connected_to') || 'Connected to'} ${bestServer.name}`
                : t('login.server_found') || 'Server found'}
              {bestServer && bestServer.users_online > 0 && (
                <span className="text-gray-400 ml-1">
                  ({bestServer.users_online} {t('login.online') || 'online'})
                </span>
              )}
            </span>
          </div>
        );
      case 'failed':
        return (
          <div className="flex items-center gap-2 text-sm text-red-400">
            <WifiOff size={16} />
            <span>{t('login.not_found') || "Can't find a server. Check your WiFi or enter the address below."}</span>
          </div>
        );
      default:
        return null;
    }
  };

  // Server list (when multiple found)
  const verifiedServers = servers.filter((s) => s.verified);
  const displayError = validationError || error;

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-surface-950 to-surface-900 px-4">
      <div className="w-full max-w-md">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-white mb-2">
            {t('app.name')}
          </h1>
          <p className="text-gray-400">
            {t('auth.register')}
          </p>
        </div>

        {/* Form card */}
        <div className="bg-surface-900 rounded-lg border border-surface-800 shadow-xl p-8">
          {/* Discovery status */}
          <div className="mb-5">
            {renderDiscoveryStatus()}
          </div>

          {/* Multi-server selector (only if >1 verified server) */}
          {!isManualMode && verifiedServers.length > 1 && (
            <div className="mb-5 p-3 bg-surface-800 rounded-lg border border-surface-700">
              <label className="block text-xs font-medium text-gray-400 mb-2">
                {t('login.choose_server') || 'Multiple servers found — choose one:'}
              </label>
              {verifiedServers.map((s) => (
                <button
                  key={s.server_id}
                  onClick={() => {
                    setAuthServerUrl(s.url);
                    useDiscoveryStore.setState({
                      bestServer: s,
                      autoConnectUrl: s.url,
                    });
                  }}
                  className={`w-full text-left px-3 py-2 rounded-lg mb-1 flex items-center gap-3 transition-colors ${
                    bestServer?.server_id === s.server_id
                      ? 'bg-blue-600/20 border border-blue-500/30 text-white'
                      : 'hover:bg-surface-700 text-gray-300'
                  }`}
                >
                  <Server size={14} />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{s.name}</div>
                    <div className="text-xs text-gray-500">{s.host}:{s.port}</div>
                  </div>
                  <div className="text-xs text-gray-500">
                    {s.users_online} {t('login.online') || 'online'}
                  </div>
                </button>
              ))}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Username field */}
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.username')}
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={t('auth.username_placeholder') || 'Enter your name'}
                className="w-full px-4 py-3 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-base"
                required
                autoFocus
              />
            </div>

            {/* Display name field */}
            <div>
              <label htmlFor="displayName" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.display_name')}
              </label>
              <input
                id="displayName"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder={t('auth.display_name_placeholder') || 'Your display name'}
                className="w-full px-4 py-3 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-base"
                required
              />
            </div>

            {/* Password field */}
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-300 mb-2">
                {t('auth.password')}
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full px-4 py-3 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-base"
                required
              />
            </div>

            {/* Confirm password field */}
            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-gray-300 mb-2">
                {t('register.confirm_password') || 'Confirm Password'}
              </label>
              <input
                id="confirmPassword"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full px-4 py-3 bg-surface-800 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all text-base"
                required
              />
            </div>

            {/* Error message */}
            {displayError && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg">
                <p className="text-sm text-red-400">{displayError}</p>
              </div>
            )}

            {/* Submit button */}
            <button
              type="submit"
              disabled={isLoading || (phase === 'searching' && !isManualMode)}
              className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-600/50 disabled:cursor-not-allowed text-white font-semibold rounded-lg transition-colors flex items-center justify-center gap-2 text-base"
            >
              {isLoading && <Loader2 size={18} className="animate-spin" />}
              {isLoading ? t('auth.registering') : t('auth.register')}
            </button>
          </form>

          {/* Advanced: Manual server entry (hidden by default) */}
          <div className="mt-4">
            <button
              onClick={() => {
                setShowAdvanced(!showAdvanced);
                if (!showAdvanced && phase === 'failed') {
                  enableManualMode();
                }
              }}
              className="w-full flex items-center justify-center gap-1 text-xs text-gray-500 hover:text-gray-400 transition-colors"
            >
              {showAdvanced ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              {t('login.advanced') || 'Advanced: Enter server address manually'}
            </button>

            {showAdvanced && (
              <div className="mt-3 p-3 bg-surface-800 rounded-lg border border-surface-700">
                <label className="block text-xs font-medium text-gray-400 mb-2">
                  {t('auth.server_url') || 'Server Address'}
                </label>
                <div className="flex gap-2">
                  <input
                    type="url"
                    value={manualUrl}
                    onChange={(e) => setManualUrl(e.target.value)}
                    placeholder="http://192.168.1.100:3000"
                    className="flex-1 px-3 py-2 bg-surface-900 border border-surface-700 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      enableManualMode();
                      handleManualConnect();
                    }}
                    className="px-3 py-2 bg-surface-700 hover:bg-surface-600 text-gray-300 rounded-lg text-sm transition-colors"
                  >
                    {t('login.connect') || 'Connect'}
                  </button>
                </div>
                {isManualMode && (
                  <button
                    onClick={() => {
                      disableManualMode();
                      setManualUrl('');
                      startSearching();
                    }}
                    className="mt-2 text-xs text-blue-400 hover:text-blue-300"
                  >
                    {t('login.back_to_auto') || '← Back to automatic discovery'}
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Auto-connect info */}
          {!showAdvanced && phase === 'verified' && (
            <p className="mt-4 text-center text-xs text-gray-500">
              {t('login.auto_connect') || 'The app connects automatically to your local network'}
            </p>
          )}

          {/* Login link */}
          <div className="mt-6 text-center">
            <p className="text-sm text-gray-400">
              {t('auth.has_account')}{' '}
              <Link
                to="/login"
                className="text-blue-400 hover:text-blue-300 font-medium transition-colors"
              >
                {t('auth.login')}
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
