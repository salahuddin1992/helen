/**
 * TransportSelector.tsx — Compact transport selector for call/chat initiation.
 *
 * Features:
 *   - Dropdown/modal for selecting transport
 *   - Shows only detected and available transports
 *   - Displays capability support (voice/video/screen)
 *   - AlertCircle quality badge
 *   - Can be embedded in call/chat UI
 */

import React, { useState } from 'react';
import { ChevronDown, Check, AlertCircle, Wifi, AlertTriangle, Bell } from 'lucide-react';
import type { DetectedTransport, SignalQuality } from '@/stores/transport.store';

interface TransportSelectorProps {
  transports: DetectedTransport[];
  signalQualities: Record<string, SignalQuality>;
  selectedTransportId?: string;
  onSelect?: (transportId: string) => void;
  compact?: boolean;
  showCapabilities?: boolean;
}

const TransportSelector: React.FC<TransportSelectorProps> = ({
  transports,
  signalQualities,
  selectedTransportId,
  onSelect,
  compact = false,
  showCapabilities = true,
}) => {
  const [isOpen, setIsOpen] = useState(false);

  const selectedTransport = transports.find(
    (t) => t.transport_id === selectedTransportId
  );
  const selectedSignal = selectedTransportId
    ? signalQualities[selectedTransportId]
    : null;

  const getMediumIcon = (medium: string) => {
    switch (medium) {
      case 'wireless':
        return <Wifi className="w-4 h-4" />;
      case 'wired':
        return <AlertCircle className="w-4 h-4" />;
      case 'optical':
        return <AlertCircle className="w-4 h-4" />;
      default:
        return null;
    }
  };

  const getSignalColor = (quality: string) => {
    switch (quality) {
      case 'excellent':
        return 'bg-green-100 text-green-700';
      case 'good':
        return 'bg-blue-100 text-blue-700';
      case 'fair':
        return 'bg-amber-100 text-amber-700';
      case 'poor':
        return 'bg-red-100 text-red-700';
      default:
        return 'bg-slate-100 text-slate-700';
    }
  };

  const getCapabilityBadges = () => {
    // In production, check actual capabilities from backend
    return ['voice', 'video', 'screen'];
  };

  if (compact) {
    // Compact button style
    return (
      <div className="relative">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex items-center gap-2 px-3 py-1.5 border border-slate-300 rounded-lg hover:bg-slate-50 transition-colors text-sm"
        >
          {selectedTransport ? (
            <>
              {getMediumIcon(selectedTransport.name)}
              <span className="text-xs font-medium text-slate-900">
                {selectedTransport.interface_name.toUpperCase()}
              </span>
            </>
          ) : (
            <span className="text-xs font-medium text-slate-500">
              Select Transport
            </span>
          )}
          <ChevronDown className="w-3 h-3 text-slate-400" />
        </button>

        {/* Dropdown */}
        {isOpen && (
          <div className="absolute top-full mt-2 w-72 bg-white border border-slate-200 rounded-lg shadow-lg z-50">
            {transports.length === 0 ? (
              <div className="p-4 text-center">
                <AlertCircle className="w-8 h-8 text-slate-300 mx-auto mb-2" />
                <p className="text-sm text-slate-600">
                  No transports detected
                </p>
              </div>
            ) : (
              <div className="divide-y divide-slate-200">
                {transports.map((transport) => {
                  const signal = signalQualities[transport.transport_id];
                  const isSelected = transport.transport_id === selectedTransportId;

                  return (
                    <button
                      key={transport.transport_id}
                      onClick={() => {
                        onSelect?.(transport.transport_id);
                        setIsOpen(false);
                      }}
                      className={`w-full text-left px-4 py-3 hover:bg-slate-50 transition-colors flex items-start justify-between ${
                        isSelected ? 'bg-blue-50' : ''
                      }`}
                    >
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          {getMediumIcon(transport.name)}
                          <span className="font-medium text-slate-900">
                            {transport.interface_name.toUpperCase()}
                          </span>
                          {isSelected && (
                            <Check className="w-4 h-4 text-blue-600 ml-auto" />
                          )}
                        </div>
                        <p className="text-xs text-slate-500 ml-6">
                          {transport.name}
                        </p>
                        {signal && (
                          <span
                            className={`inline-block mt-1 px-2 py-0.5 rounded text-xs font-medium ${getSignalColor(
                              signal.quality_label
                            )}`}
                          >
                            {signal.quality_label}
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // Full card style
  return (
    <div className="bg-white border border-slate-200 rounded-lg p-4">
      <h3 className="font-semibold text-slate-900 mb-3">
        Communication Transport
      </h3>

      {transports.length === 0 ? (
        <div className="text-center py-6">
          <AlertCircle className="w-8 h-8 text-slate-300 mx-auto mb-2" />
          <p className="text-sm text-slate-600">
            No transports detected
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {transports.map((transport) => {
            const signal = signalQualities[transport.transport_id];
            const isSelected = transport.transport_id === selectedTransportId;
            const capabilities = getCapabilityBadges();

            return (
              <button
                key={transport.transport_id}
                onClick={() => onSelect?.(transport.transport_id)}
                className={`w-full text-left px-4 py-3 rounded-lg border-2 transition-all ${
                  isSelected
                    ? 'border-blue-500 bg-blue-50'
                    : 'border-slate-200 hover:border-slate-300'
                }`}
              >
                <div className="flex items-start justify-between mb-2">
                  <div className="flex items-center gap-2">
                    {getMediumIcon(transport.name)}
                    <span className="font-medium text-slate-900">
                      {transport.interface_name.toUpperCase()}
                    </span>
                  </div>
                  {isSelected && (
                    <Check className="w-5 h-5 text-blue-600" />
                  )}
                </div>

                <p className="text-sm text-slate-600 mb-2">
                  {transport.name} • {transport.speed}
                </p>

                {/* AlertCircle Badge */}
                {signal && (
                  <div className="flex items-center gap-2 mb-2">
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${getSignalColor(
                        signal.quality_label
                      )}`}
                    >
                      {signal.quality_label}
                    </span>
                    <span className="text-xs text-slate-500">
                      {signal.quality_score.toFixed(0)}/100
                    </span>
                  </div>
                )}

                {/* Capabilities */}
                {showCapabilities && (
                  <div className="flex items-center gap-2 text-xs text-slate-500">
                    {capabilities.map((cap) => (
                      <span key={cap} className="inline-block">
                        {cap === 'voice' && '🎤'}
                        {cap === 'video' && '🎥'}
                        {cap === 'screen' && '🖥️'}
                      </span>
                    ))}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default TransportSelector;
