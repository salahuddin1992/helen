/**
 * DetectedTransports.tsx — Show auto-detected network transports.
 *
 * Features:
 *   - Card grid of detected interfaces
 *   - Real-time status indicators
 *   - AlertCircle strength visualization
 *   - Scan now button with animation
 *   - Last scan timestamp
 */

import React from 'react';
import { RefreshCw, AlertTriangle as ZapIcon, AlertCircle, CheckCircle, AlertTriangle } from 'lucide-react';
import { useTransportStore } from '@/stores/transport.store';
import type { DetectedTransport } from '@/stores/transport.store';

interface DetectedTransportsProps {
  transports: DetectedTransport[];
  isScanning: boolean;
}

const DetectedTransports: React.FC<DetectedTransportsProps> = ({ transports, isScanning }) => {
  const { runDetection, lastScanTime } = useTransportStore();

  const getStatusIcon = (transport: DetectedTransport) => {
    if (!transport.is_up) {
      return <AlertCircle className="w-5 h-5 text-red-500" />;
    }
    if (transport.signal_quality === 'poor') {
      return <AlertTriangle className="w-5 h-5 text-amber-500" />;
    }
    return <CheckCircle className="w-5 h-5 text-green-500" />;
  };

  const getSignalColor = (strength: number) => {
    if (strength >= 80) return 'bg-green-500';
    if (strength >= 60) return 'bg-blue-500';
    if (strength >= 40) return 'bg-amber-500';
    return 'bg-red-500';
  };

  const getQualityLabel = (quality: string) => {
    const labels = {
      excellent: { bg: 'bg-green-50', text: 'text-green-700', label: 'Excellent' },
      good: { bg: 'bg-blue-50', text: 'text-blue-700', label: 'Good' },
      fair: { bg: 'bg-amber-50', text: 'text-amber-700', label: 'Fair' },
      poor: { bg: 'bg-red-50', text: 'text-red-700', label: 'Poor' },
    };
    return labels[quality as keyof typeof labels] || labels.fair;
  };

  const formatLastScan = (timestamp: string | null) => {
    if (!timestamp) return 'Never';
    try {
      const date = new Date(timestamp);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffSec = Math.floor(diffMs / 1000);
      const diffMin = Math.floor(diffSec / 60);

      if (diffSec < 60) return 'Just now';
      if (diffMin < 60) return `${diffMin}m ago`;
      return date.toLocaleTimeString();
    } catch {
      return 'Unknown';
    }
  };

  return (
    <div className="flex flex-col gap-6 h-full overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between sticky top-0 bg-slate-50 -mx-6 px-6 py-3 border-b border-slate-200">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">Auto-Detected Transports</h2>
          <p className="text-sm text-slate-600 mt-1">
            Last scan: {formatLastScan(lastScanTime)}
          </p>
        </div>

        <button
          onClick={() => runDetection()}
          disabled={isScanning}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:bg-blue-400 disabled:cursor-not-allowed transition-colors"
        >
          <RefreshCw
            className={`w-4 h-4 ${isScanning ? 'animate-spin' : ''}`}
          />
          {isScanning ? 'Scanning...' : 'Scan Now'}
        </button>
      </div>

      {/* Transport AlertCircle */}
      {transports.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-center">
          <div>
            <AlertTriangle className="w-12 h-12 text-slate-300 mx-auto mb-4" />
            <p className="text-slate-600 font-medium">No transports detected</p>
            <p className="text-sm text-slate-500 mt-2">
              Click "Scan Now" to search for available network interfaces
            </p>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {transports.map((transport) => {
            const quality = getQualityLabel(transport.signal_quality);

            return (
              <div
                key={transport.interface_name}
                className="bg-white border border-slate-200 rounded-lg p-4 hover:border-blue-300 transition-colors"
              >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex-1">
                    <h3 className="font-semibold text-slate-900">
                      {transport.interface_name.toUpperCase()}
                    </h3>
                    <p className="text-sm text-slate-600">{transport.name}</p>
                  </div>
                  <div className="flex-shrink-0">
                    {getStatusIcon(transport)}
                  </div>
                </div>

                {/* Status Badge */}
                <div className="flex items-center gap-2 mb-3">
                  <span
                    className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${quality.bg} ${quality.text}`}
                  >
                    {quality.label}
                  </span>
                  {!transport.is_up && (
                    <span className="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-red-50 text-red-700">
                      Down
                    </span>
                  )}
                </div>

                {/* Details */}
                <div className="space-y-2 mb-3 text-sm">
                  {transport.ip_address && (
                    <div className="flex justify-between text-slate-600">
                      <span>IP Address</span>
                      <code className="text-slate-900 font-mono">
                        {transport.ip_address}
                      </code>
                    </div>
                  )}
                  <div className="flex justify-between text-slate-600">
                    <span>Speed</span>
                    <span className="text-slate-900 font-medium">
                      {transport.speed}
                    </span>
                  </div>
                  {transport.mac_address && (
                    <div className="flex justify-between text-slate-600">
                      <span>MAC Address</span>
                      <code className="text-slate-900 font-mono text-xs">
                        {transport.mac_address}
                      </code>
                    </div>
                  )}
                  <div className="flex justify-between text-slate-600">
                    <span>MTU</span>
                    <span className="text-slate-900 font-medium">{transport.mtu}</span>
                  </div>
                </div>

                {/* AlertCircle Strength Bar */}
                <div className="mb-2">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs text-slate-600 font-medium">AlertCircle Strength</span>
                    <span className="text-xs font-semibold text-slate-900">
                      {transport.signal_strength}%
                    </span>
                  </div>
                  <div className="w-full bg-slate-200 rounded-full h-2 overflow-hidden">
                    <div
                      className={`h-full ${getSignalColor(transport.signal_strength)} transition-all duration-300`}
                      style={{ width: `${transport.signal_strength}%` }}
                    />
                  </div>
                </div>

                {/* Transport AlertCircle */}
                <div className="text-xs text-slate-500 border-t border-slate-100 pt-2">
                  <span className="font-mono">{transport.transport_id}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default DetectedTransports;
