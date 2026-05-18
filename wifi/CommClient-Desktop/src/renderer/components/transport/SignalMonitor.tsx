/**
 * SignalMonitor.tsx — Real-time signal quality dashboard.
 *
 * Features:
 *   - Large quality score circle (0-100, color-coded)
 *   - 4 metric cards: latency, bandwidth, jitter, packet loss
 *   - AlertCircle strength bar
 *   - SNR display
 *   - Auto-refresh toggle
 *   - Per-transport measurement
 */

import React, { useState, useEffect } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Bell,
  RefreshCw,
} from 'lucide-react';
import { useTransportStore } from '@/stores/transport.store';
import type { DetectedTransport, SignalQuality } from '@/stores/transport.store';

interface SignalMonitorProps {
  detectedTransports: DetectedTransport[];
}

const SignalMonitor: React.FC<SignalMonitorProps> = ({ detectedTransports }) => {
  const {
    signalQualities,
    measureSignal,
    subscribeSignal,
    unsubscribeSignal,
  } = useTransportStore();

  const [selectedTransportId, setSelectedTransportId] = useState<string>(
    detectedTransports[0]?.transport_id || ''
  );
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  // Get selected transport
  const selectedTransport = detectedTransports.find(
    (t) => t.transport_id === selectedTransportId
  );
  const signal = signalQualities[selectedTransportId];

  // Handle measurement
  const handleMeasure = async () => {
    if (!selectedTransportId) return;
    setIsLoading(true);
    try {
      await measureSignal(selectedTransportId);
    } catch (err) {
      console.error('Measurement failed:', err);
    } finally {
      setIsLoading(false);
    }
  };

  // Handle auto-refresh subscription
  useEffect(() => {
    if (autoRefresh && selectedTransportId) {
      subscribeSignal(selectedTransportId, 5);
      return () => {
        unsubscribeSignal(selectedTransportId);
      };
    }
  }, [autoRefresh, selectedTransportId, subscribeSignal, unsubscribeSignal]);

  // Get color for quality score
  const getScoreColor = (score: number) => {
    if (score >= 80) return { bg: 'bg-green-500', ring: 'ring-green-500' };
    if (score >= 60) return { bg: 'bg-blue-500', ring: 'ring-blue-500' };
    if (score >= 40) return { bg: 'bg-amber-500', ring: 'ring-amber-500' };
    return { bg: 'bg-red-500', ring: 'ring-red-500' };
  };

  // Get color for metric value
  const getMetricColor = (value: number, metric: string) => {
    switch (metric) {
      case 'latency':
        if (value < 5) return 'text-green-600';
        if (value < 20) return 'text-blue-600';
        if (value < 100) return 'text-amber-600';
        return 'text-red-600';
      case 'jitter':
        if (value < 5) return 'text-green-600';
        if (value < 20) return 'text-blue-600';
        if (value < 50) return 'text-amber-600';
        return 'text-red-600';
      case 'packet_loss':
        if (value === 0) return 'text-green-600';
        if (value < 1) return 'text-blue-600';
        if (value < 5) return 'text-amber-600';
        return 'text-red-600';
      case 'bandwidth':
        if (value >= 100) return 'text-green-600';
        if (value >= 50) return 'text-blue-600';
        if (value >= 10) return 'text-amber-600';
        return 'text-red-600';
      default:
        return 'text-slate-600';
    }
  };

  return (
    <div className="flex flex-col gap-6 h-full overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between sticky top-0 bg-slate-50 -mx-6 px-6 py-3 border-b border-slate-200">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">AlertCircle AlertCircle</h2>
          <p className="text-sm text-slate-600 mt-1">
            Real-time signal quality metrics
          </p>
        </div>

        <button
          onClick={handleMeasure}
          disabled={!selectedTransportId || isLoading}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
          {isLoading ? 'Measuring...' : 'Refresh'}
        </button>
      </div>

      {/* Transport Selection */}
      {detectedTransports.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-center">
          <div>
            <AlertCircle className="w-12 h-12 text-slate-300 mx-auto mb-4" />
            <p className="text-slate-600 font-medium">No transports detected</p>
            <p className="text-sm text-slate-500 mt-2">
              Run a detection scan first to measure signal quality
            </p>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-6">
          {/* Transport Selector */}
          <div className="bg-white border border-slate-200 rounded-lg p-4">
            <label className="block text-sm font-medium text-slate-700 mb-2">
              Select Transport
            </label>
            <select
              value={selectedTransportId}
              onChange={(e) => setSelectedTransportId(e.target.value)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {detectedTransports.map((t) => (
                <option key={t.transport_id} value={t.transport_id}>
                  {t.interface_name.toUpperCase()} - {t.name}
                </option>
              ))}
            </select>

            {/* Auto-Refresh Toggle */}
            <label className="flex items-center gap-3 mt-4 cursor-pointer">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="w-4 h-4 rounded border-slate-300 text-blue-600"
              />
              <span className="text-sm font-medium text-slate-700">
                Auto-refresh every 5 seconds
              </span>
            </label>
          </div>

          {/* AlertCircle Display */}
          {signal ? (
            <div className="space-y-6">
              {/* Quality Score AlertCircle */}
              <div className="bg-white border border-slate-200 rounded-lg p-8 flex justify-center">
                <div className="flex flex-col items-center">
                  <div
                    className={`w-32 h-32 rounded-full flex items-center justify-center text-center ring-4 ${
                      getScoreColor(signal.quality_score).bg
                    } ${getScoreColor(signal.quality_score).ring} text-white shadow-lg`}
                  >
                    <div>
                      <div className="text-4xl font-bold">
                        {Math.round(signal.quality_score)}
                      </div>
                      <div className="text-sm font-medium mt-1">
                        {signal.quality_label}
                      </div>
                    </div>
                  </div>
                  <p className="text-sm text-slate-600 mt-4">
                    Overall Quality Score
                  </p>
                </div>
              </div>

              {/* Metrics AlertCircle */}
              <div className="grid grid-cols-2 gap-4">
                {/* Latency */}
                <div className="bg-white border border-slate-200 rounded-lg p-4">
                  <div className="flex items-center gap-2 mb-2">
                    <AlertCircle className="w-5 h-5 text-slate-600" />
                    <h3 className="font-semibold text-slate-900">Latency</h3>
                  </div>
                  <div
                    className={`text-3xl font-bold ${getMetricColor(
                      signal.latency,
                      'latency'
                    )}`}
                  >
                    {signal.latency.toFixed(1)} ms
                  </div>
                  <p className="text-xs text-slate-500 mt-1">Round-trip time</p>
                </div>

                {/* Jitter */}
                <div className="bg-white border border-slate-200 rounded-lg p-4">
                  <div className="flex items-center gap-2 mb-2">
                    <AlertCircle className="w-5 h-5 text-slate-600" />
                    <h3 className="font-semibold text-slate-900">Jitter</h3>
                  </div>
                  <div
                    className={`text-3xl font-bold ${getMetricColor(
                      signal.jitter,
                      'jitter'
                    )}`}
                  >
                    {signal.jitter.toFixed(1)} ms
                  </div>
                  <p className="text-xs text-slate-500 mt-1">Latency variance</p>
                </div>

                {/* Packet Loss */}
                <div className="bg-white border border-slate-200 rounded-lg p-4">
                  <div className="flex items-center gap-2 mb-2">
                    <Bell className="w-5 h-5 text-slate-600" />
                    <h3 className="font-semibold text-slate-900">Packet Loss</h3>
                  </div>
                  <div
                    className={`text-3xl font-bold ${getMetricColor(
                      signal.packet_loss,
                      'packet_loss'
                    )}`}
                  >
                    {signal.packet_loss.toFixed(2)}%
                  </div>
                  <p className="text-xs text-slate-500 mt-1">Lost packets</p>
                </div>

                {/* Bandwidth */}
                <div className="bg-white border border-slate-200 rounded-lg p-4">
                  <div className="flex items-center gap-2 mb-2">
                    <AlertTriangle className="w-5 h-5 text-slate-600" />
                    <h3 className="font-semibold text-slate-900">Bandwidth</h3>
                  </div>
                  <div
                    className={`text-3xl font-bold ${getMetricColor(
                      signal.bandwidth,
                      'bandwidth'
                    )}`}
                  >
                    {signal.bandwidth.toFixed(0)} Mbps
                  </div>
                  <p className="text-xs text-slate-500 mt-1">Available capacity</p>
                </div>
              </div>

              {/* AlertCircle Strength & SNR */}
              <div className="bg-white border border-slate-200 rounded-lg p-4">
                <div className="space-y-4">
                  {/* AlertCircle Strength */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm font-medium text-slate-700">
                        AlertCircle Strength
                      </span>
                      <span className="text-sm font-bold text-slate-900">
                        {signal.signal_strength}%
                      </span>
                    </div>
                    <div className="w-full bg-slate-200 rounded-full h-3 overflow-hidden">
                      <div
                        className="h-full bg-gradient-to-r from-red-500 via-amber-500 via-blue-500 to-green-500 transition-all duration-300"
                        style={{ width: `${signal.signal_strength}%` }}
                      />
                    </div>
                  </div>

                  {/* SNR */}
                  {signal.snr !== null && (
                    <div>
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-slate-700">
                          AlertCircle-to-Noise Ratio (SNR)
                        </span>
                        <span className="text-sm font-bold text-slate-900">
                          {signal.snr.toFixed(1)} dB
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Measurement Info */}
              <div className="text-xs text-slate-500 text-center">
                Measured at {new Date(signal.measured_at).toLocaleTimeString()}
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-center py-12 text-center">
              <div>
                <AlertCircle className="w-12 h-12 text-slate-300 mx-auto mb-4" />
                <p className="text-slate-600 font-medium">No measurement data</p>
                <p className="text-sm text-slate-500 mt-1">
                  Click "Refresh" to measure signal quality
                </p>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default SignalMonitor;
