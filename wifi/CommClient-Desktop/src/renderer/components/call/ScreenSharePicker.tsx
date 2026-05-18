import React, { useEffect, useState, useRef } from 'react';
import { X, AlertCircle, RefreshCw, Clock } from 'lucide-react';
import { t } from '@/i18n';

interface DesktopSource {
  id: string;
  name: string;
  thumbnail: string;
  type: 'screen' | 'window';
}

interface ScreenSharePickerProps {
  onSelect: (sourceId: string) => void;
  onCancel: () => void;
}

type FilterTab = 'all' | 'screens' | 'windows';

const ScreenSharePicker: React.FC<ScreenSharePickerProps> = ({
  onSelect,
  onCancel,
}) => {
  const [sources, setSources] = useState<DesktopSource[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filterTab, setFilterTab] = useState<FilterTab>('all');
  const [shareAudio, setShareAudio] = useState(false);
  const [qualityPreset, setQualityPreset] = useState<string>('1080p');
  const [hoverPreview, setHoverPreview] = useState<string | null>(null);
  const [fetchTimeout, setFetchTimeout] = useState(false);
  const fetchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recentSourcesKey = 'commclient_recent_share_sources';

  const fetchSources = async () => {
    try {
      setIsLoading(true);
      setError(null);
      setFetchTimeout(false);

      // Set 10s timeout for source fetching
      fetchTimeoutRef.current = setTimeout(() => {
        setFetchTimeout(true);
        setIsLoading(false);
        setError(t('call.screen_share_timeout') || 'Screen fetch timeout');
      }, 10000);

      // Call Electron API to get available screens and windows
      const desktopSources =
        await window.electronAPI?.getDesktopSources?.();

      if (fetchTimeoutRef.current) {
        clearTimeout(fetchTimeoutRef.current);
      }

      if (!desktopSources) {
        setError(t('call.screen_share_error'));
        return;
      }

      const formattedSources: DesktopSource[] = desktopSources.map(
        (source: any) => ({
          id: source.id,
          name: source.name,
          thumbnail: source.thumbnail || '',
          type: source.id.startsWith('screen') ? 'screen' : 'window',
        })
      );

      setSources(formattedSources);
      if (formattedSources.length > 0) {
        setSelectedId(formattedSources[0].id);
      }
    } catch (err) {
      console.error('Error fetching desktop sources:', err);
      setError(t('call.screen_share_error'));
    } finally {
      setIsLoading(false);
      if (fetchTimeoutRef.current) {
        clearTimeout(fetchTimeoutRef.current);
      }
    }
  };

  useEffect(() => {
    fetchSources();
    return () => {
      if (fetchTimeoutRef.current) {
        clearTimeout(fetchTimeoutRef.current);
      }
    };
  }, []);

  const handleSelect = () => {
    if (selectedId) {
      // Store recently shared sources
      const selected = sources.find(s => s.id === selectedId);
      if (selected) {
        const recent = JSON.parse(sessionStorage.getItem(recentSourcesKey) || '[]') as Array<{ id: string; name: string }>;
        const updated = [{ id: selected.id, name: selected.name }, ...recent.filter(r => r.id !== selectedId)].slice(0, 3);
        sessionStorage.setItem(recentSourcesKey, JSON.stringify(updated));
      }
      onSelect(selectedId);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    const filtered = filterTab === 'all' ? sources : filterTab === 'screens' ? screens : windows;
    const currentIndex = filtered.findIndex(s => s.id === selectedId);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (currentIndex < filtered.length - 1) {
        setSelectedId(filtered[currentIndex + 1].id);
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (currentIndex > 0) {
        setSelectedId(filtered[currentIndex - 1].id);
      }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      handleSelect();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    }
  };

  const screens = sources.filter((s) => s.type === 'screen');
  const windows = sources.filter((s) => s.type === 'window');

  const filteredSources =
    filterTab === 'screens' ? screens :
    filterTab === 'windows' ? windows :
    sources;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4" onKeyDown={handleKeyDown}>
      <div className="bg-surface-900 rounded-2xl shadow-2xl max-w-3xl w-full max-h-[90vh] flex flex-col animate-slideUp">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
          <h2 className="text-xl font-bold text-text-100">
            {t('call.select_screen_to_share')}
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchSources}
              disabled={isLoading}
              className="p-2 hover:bg-surface-800 disabled:opacity-50 rounded-lg transition-colors"
              title="Refresh sources"
            >
              <RefreshCw size={18} className={`text-text-400 ${isLoading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={onCancel}
              className="p-2 hover:bg-surface-800 rounded-lg transition-colors"
            >
              <X size={20} className="text-text-400" />
            </button>
          </div>
        </div>

        {/* Filter Tabs */}
        {sources.length > 0 && (
          <div className="flex gap-2 px-6 pt-4 border-b border-surface-800">
            {(['all', 'screens', 'windows'] as FilterTab[]).map((tab) => (
              <button
                key={tab}
                onClick={() => setFilterTab(tab)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  filterTab === tab
                    ? 'bg-blue-600 text-white'
                    : 'bg-surface-800 text-text-400 hover:bg-surface-700'
                }`}
              >
                {tab === 'all' ? 'All' : tab === 'screens' ? `Screens (${screens.length})` : `Windows (${windows.length})`}
              </button>
            ))}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {isLoading ? (
            <div className="flex items-center justify-center h-64">
              <div className="flex flex-col items-center gap-3">
                <div className="w-10 h-10 border-3 border-surface-700 border-t-blue-500 rounded-full animate-spin" />
                <p className="text-text-400">{t('call.loading_screens')}</p>
              </div>
            </div>
          ) : fetchTimeout ? (
            <div className="flex items-center justify-center h-64">
              <div className="text-center">
                <Clock className="w-12 h-12 mx-auto mb-2 text-yellow-400" />
                <p className="text-yellow-400 font-medium mb-2">Fetch timeout</p>
                <p className="text-sm text-text-500 mb-4">
                  Source fetching took too long. Try refreshing.
                </p>
                <button
                  onClick={fetchSources}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
                >
                  Try Again
                </button>
              </div>
            </div>
          ) : error ? (
            <div className="flex items-center justify-center h-64">
              <div className="text-center">
                <p className="text-red-400 font-medium mb-2">{error}</p>
                <p className="text-sm text-text-500">
                  {t('call.screen_share_error_hint')}
                </p>
              </div>
            </div>
          ) : filteredSources.length === 0 ? (
            <div className="flex items-center justify-center h-64">
              <p className="text-text-400">{t('call.no_screens_available')}</p>
            </div>
          ) : (
            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {filteredSources.map((source) => (
                  <ScreenThumbnailWithPreview
                    key={source.id}
                    source={source}
                    isSelected={selectedId === source.id}
                    isHovered={hoverPreview === source.id}
                    onSelect={() => setSelectedId(source.id)}
                    onHover={() => setHoverPreview(source.id)}
                    onLeave={() => setHoverPreview(null)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-surface-800 p-6 space-y-4">
          {/* Quality Preset & Audio Toggle */}
          <div className="flex items-center gap-4">
            <div className="flex-1">
              <label className="block text-xs font-semibold text-text-300 mb-2">
                Quality Preset
              </label>
              <select
                value={qualityPreset}
                onChange={(e) => setQualityPreset(e.target.value)}
                className="w-full px-3 py-2 bg-surface-800 border border-surface-700 rounded-lg text-text-100 text-sm hover:border-surface-600 transition-colors"
              >
                <option value="lan-max">LAN (Max Quality)</option>
                <option value="1080p">1080p (Detail)</option>
                <option value="1080p-detail">1080p (Ultra Detail)</option>
                <option value="720p">720p (Standard)</option>
                <option value="motion">Motion (Low-latency)</option>
              </select>
            </div>
            <div className="flex items-center gap-2 pt-4">
              <input
                type="checkbox"
                id="shareAudio"
                checked={shareAudio}
                onChange={(e) => setShareAudio(e.target.checked)}
                className="w-4 h-4 rounded cursor-pointer"
              />
              <label htmlFor="shareAudio" className="text-sm text-text-300 cursor-pointer">
                Share system audio
              </label>
            </div>
          </div>

          {/* Action Buttons */}
          <div className="flex gap-3">
            <button
              onClick={onCancel}
              className="flex-1 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-text-100 font-medium rounded-lg transition-colors"
            >
              {t('common.cancel')}
            </button>
            <button
              onClick={handleSelect}
              disabled={!selectedId || isLoading}
              className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-surface-700 disabled:text-text-400 text-white font-medium rounded-lg transition-colors"
            >
              {t('call.share')}
            </button>
          </div>
        </div>
      </div>

      <style>{`
        @keyframes slideUp {
          from {
            opacity: 0;
            transform: translateY(20px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
        .animate-slideUp {
          animation: slideUp 0.3s ease-out;
        }
      `}</style>
    </div>
  );
};

const ScreenThumbnailWithPreview: React.FC<{
  source: DesktopSource;
  isSelected: boolean;
  isHovered: boolean;
  onSelect: () => void;
  onHover: () => void;
  onLeave: () => void;
}> = ({ source, isSelected, isHovered, onSelect, onHover, onLeave }) => {
  return (
    <div className="relative" onMouseEnter={onHover} onMouseLeave={onLeave}>
      <button
        onClick={onSelect}
        className={`relative rounded-lg overflow-hidden transition-all duration-200 w-full ${
          isSelected
            ? 'ring-2 ring-blue-500 shadow-lg shadow-blue-500/30'
            : 'hover:ring-1 hover:ring-surface-700'
        }`}
      >
        <div className="relative w-full pt-[56.25%] bg-surface-950">
          {source.thumbnail && (
            <img
              src={source.thumbnail}
              alt={source.name}
              className="absolute inset-0 w-full h-full object-cover"
            />
          )}
          <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent flex flex-col items-end justify-between p-3">
            <span
              className={`px-2 py-1 rounded text-xs font-medium ${
                source.type === 'screen'
                  ? 'bg-blue-500/80 text-white'
                  : 'bg-purple-500/80 text-white'
              }`}
            >
              {source.type === 'screen'
                ? t('call.screen')
                : t('call.window')}
            </span>
            <p className="text-white text-sm font-medium text-left w-full truncate">
              {source.name}
            </p>
          </div>
        </div>
        {isSelected && (
          <div className="absolute inset-0 bg-blue-500/10 pointer-events-none" />
        )}
      </button>

      {/* Hover Preview Tooltip */}
      {isHovered && (
        <div className="absolute -top-2 -right-2 z-10 rounded-lg overflow-hidden shadow-2xl border-2 border-blue-500 bg-black animate-fadeIn"
             style={{ width: 320, height: 180 }}>
          {source.thumbnail && (
            <img
              src={source.thumbnail}
              alt={source.name}
              className="w-full h-full object-cover"
            />
          )}
          <div className="absolute inset-0 bg-gradient-to-t from-black/70 to-transparent flex items-end p-2">
            <p className="text-white text-xs font-medium">{source.name}</p>
          </div>
        </div>
      )}

      <style>{`
        @keyframes fadeIn {
          from { opacity: 0; transform: scale(0.95); }
          to { opacity: 1; transform: scale(1); }
        }
        .animate-fadeIn {
          animation: fadeIn 0.2s ease-out;
        }
      `}</style>
    </div>
  );
};

const ScreenThumbnail: React.FC<{
  source: DesktopSource;
  isSelected: boolean;
  onSelect: () => void;
}> = ({ source, isSelected, onSelect }) => {
  return (
    <button
      onClick={onSelect}
      className={`relative rounded-lg overflow-hidden transition-all duration-200 ${
        isSelected
          ? 'ring-2 ring-blue-500 shadow-lg shadow-blue-500/30'
          : 'hover:ring-1 hover:ring-surface-700'
      }`}
    >
      <div className="relative w-full pt-[56.25%] bg-surface-950">
        {source.thumbnail && (
          <img
            src={source.thumbnail}
            alt={source.name}
            className="absolute inset-0 w-full h-full object-cover"
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent flex flex-col items-end justify-between p-3">
          <span
            className={`px-2 py-1 rounded text-xs font-medium ${
              source.type === 'screen'
                ? 'bg-blue-500/80 text-white'
                : 'bg-purple-500/80 text-white'
            }`}
          >
            {source.type === 'screen'
              ? t('call.screen')
              : t('call.window')}
          </span>
          <p className="text-white text-sm font-medium text-left w-full truncate">
            {source.name}
          </p>
        </div>
      </div>
      {isSelected && (
        <div className="absolute inset-0 bg-blue-500/10 pointer-events-none" />
      )}
    </button>
  );
};

export default ScreenSharePicker;
