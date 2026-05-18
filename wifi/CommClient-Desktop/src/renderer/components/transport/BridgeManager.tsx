/**
 * BridgeManager.tsx — Manage active communication bridges.
 *
 * Features:
 *   - Table of active bridges with status
 *   - Create bridge dialog with configuration
 *   - Auto-bridge selection
 *   - Bridge destruction with confirmation
 *   - Real-time peer tracking
 */

import React, { useState } from 'react';
import {
  Plus,
  AlertCircle,
  Trash2,
  Info,
  Copy,
  ExternalLink,
  CheckCircle,
  AlertTriangle,
} from 'lucide-react';
import { useTransportStore } from '@/stores/transport.store';
import type { BridgeStatus, DetectedTransport, BridgeCreateRequest } from '@/stores/transport.store';

interface BridgeManagerProps {
  bridges: BridgeStatus[];
  detectedTransports: DetectedTransport[];
}

const BridgeManager: React.FC<BridgeManagerProps> = ({ bridges, detectedTransports }) => {
  const { createBridge, destroyBridge, autoBridge } = useTransportStore();
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [showAutoConfirm, setShowAutoConfirm] = useState(false);
  const [formData, setFormData] = useState<BridgeCreateRequest>({
    transport_id: detectedTransports[0]?.transport_id || '',
    name: 'New Bridge',
    bind_port: undefined,
    protocol: 'tcp',
    encryption: true,
    max_connections: 64,
  });
  const [isCreating, setIsCreating] = useState(false);

  const handleCreateBridge = async () => {
    if (!formData.transport_id || !formData.name) {
      alert('Please fill in all required fields');
      return;
    }

    setIsCreating(true);
    try {
      await createBridge(formData);
      setShowCreateDialog(false);
      setFormData({
        transport_id: detectedTransports[0]?.transport_id || '',
        name: 'New Bridge',
        bind_port: undefined,
        protocol: 'tcp',
        encryption: true,
        max_connections: 64,
      });
    } catch (err) {
      console.error('Failed to create bridge:', err);
    } finally {
      setIsCreating(false);
    }
  };

  const handleAutoBridge = async () => {
    try {
      await autoBridge('Auto Bridge');
      setShowAutoConfirm(false);
    } catch (err) {
      console.error('Failed to auto-bridge:', err);
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'active':
        return <CheckCircle className="w-4 h-4 text-green-600" />;
      case 'idle':
        return <AlertTriangle className="w-4 h-4 text-amber-600" />;
      case 'error':
        return <AlertCircle className="w-4 h-4 text-red-600" />;
      default:
        return null;
    }
  };

  const formatUptime = (seconds: number) => {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
  };

  return (
    <div className="flex flex-col gap-6 h-full overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between sticky top-0 bg-slate-50 -mx-6 px-6 py-3 border-b border-slate-200">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">
            Active Bridges ({bridges.length})
          </h2>
          <p className="text-sm text-slate-600 mt-1">
            Manage communication bridges across detected transports
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowAutoConfirm(true)}
            disabled={detectedTransports.length === 0}
            className="flex items-center gap-2 px-3 py-2 bg-amber-600 text-white rounded-lg font-medium hover:bg-amber-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition-colors text-sm"
          >
            <AlertCircle className="w-4 h-4" />
            Auto Bridge
          </button>
          <button
            onClick={() => setShowCreateDialog(true)}
            disabled={detectedTransports.length === 0}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition-colors"
          >
            <Plus className="w-4 h-4" />
            Create Bridge
          </button>
        </div>
      </div>

      {/* Bridges Table */}
      {bridges.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-center">
          <div>
            <Info className="w-12 h-12 text-slate-300 mx-auto mb-4" />
            <p className="text-slate-600 font-medium">No active bridges</p>
            <p className="text-sm text-slate-500 mt-2">
              Create a bridge to enable communication between peers
            </p>
          </div>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="px-4 py-3 text-left font-semibold text-slate-700">
                  Name
                </th>
                <th className="px-4 py-3 text-left font-semibold text-slate-700">
                  Transport
                </th>
                <th className="px-4 py-3 text-left font-semibold text-slate-700">
                  Status
                </th>
                <th className="px-4 py-3 text-center font-semibold text-slate-700">
                  Peers
                </th>
                <th className="px-4 py-3 text-left font-semibold text-slate-700">
                  Uptime
                </th>
                <th className="px-4 py-3 text-right font-semibold text-slate-700">
                  Data
                </th>
                <th className="px-4 py-3 text-right font-semibold text-slate-700">
                  Latency
                </th>
                <th className="px-4 py-3 text-center font-semibold text-slate-700">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {bridges.map((bridge) => (
                <tr key={bridge.bridge_id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3">
                    <div>
                      <p className="font-medium text-slate-900">{bridge.name}</p>
                      <p className="text-xs text-slate-500 font-mono">
                        {bridge.bind_address}:{bridge.bind_port}
                      </p>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <p className="text-slate-700">{bridge.transport_name}</p>
                    <p className="text-xs text-slate-500">
                      {bridge.is_encrypted ? '🔐 Encrypted' : 'Unencrypted'}
                    </p>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      {getStatusIcon(bridge.status)}
                      <span className="capitalize text-slate-700 font-medium">
                        {bridge.status}
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-center">
                    <span className="inline-flex items-center justify-center px-2 py-1 bg-blue-100 text-blue-700 rounded-full text-xs font-medium">
                      {bridge.peer_count}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-slate-700 font-medium">
                      {formatUptime(bridge.uptime_seconds)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="text-xs text-slate-600">
                      <p>
                        ↑ {formatBytes(bridge.bytes_sent)}
                      </p>
                      <p>
                        ↓ {formatBytes(bridge.bytes_received)}
                      </p>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right">
                    {bridge.avg_latency_ms !== null ? (
                      <span className="text-slate-700 font-medium">
                        {bridge.avg_latency_ms.toFixed(2)} ms
                      </span>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <button
                      onClick={() => {
                        if (confirm(`Destroy bridge "${bridge.name}"?`)) {
                          destroyBridge(bridge.bridge_id);
                        }
                      }}
                      className="p-2 text-red-600 hover:bg-red-50 rounded transition-colors"
                      title="Destroy bridge"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create Bridge Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl max-w-md w-full mx-4">
            <div className="px-6 py-4 border-b border-slate-200">
              <h3 className="text-lg font-semibold text-slate-900">Create Bridge</h3>
            </div>

            <div className="px-6 py-4 space-y-4">
              {/* Bridge Name */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Bridge Name
                </label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) =>
                    setFormData({ ...formData, name: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="e.g., Main Bridge"
                />
              </div>

              {/* Transport Selection */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Transport
                </label>
                <select
                  value={formData.transport_id}
                  onChange={(e) =>
                    setFormData({ ...formData, transport_id: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {detectedTransports.map((t) => (
                    <option key={t.transport_id} value={t.transport_id}>
                      {t.name} ({t.interface_name})
                    </option>
                  ))}
                </select>
              </div>

              {/* Port */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Bind Port (optional)
                </label>
                <input
                  type="number"
                  value={formData.bind_port || ''}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      bind_port: e.target.value ? parseInt(e.target.value) : undefined,
                    })
                  }
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="Auto-select if empty"
                  min="1024"
                  max="65535"
                />
              </div>

              {/* Protocol */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Protocol
                </label>
                <select
                  value={formData.protocol}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      protocol: e.target.value as 'tcp' | 'udp' | 'both',
                    })
                  }
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="tcp">TCP</option>
                  <option value="udp">UDP</option>
                  <option value="both">Both</option>
                </select>
              </div>

              {/* Encryption */}
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={formData.encryption}
                  onChange={(e) =>
                    setFormData({ ...formData, encryption: e.target.checked })
                  }
                  className="w-4 h-4 rounded border-slate-300 text-blue-600"
                />
                <span className="text-sm font-medium text-slate-700">
                  Enable Encryption
                </span>
              </label>

              {/* Max Connections */}
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Max Connections: {formData.max_connections}
                </label>
                <input
                  type="range"
                  min="1"
                  max="1000"
                  value={formData.max_connections || 64}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      max_connections: parseInt(e.target.value),
                    })
                  }
                  className="w-full"
                />
              </div>
            </div>

            <div className="px-6 py-4 border-t border-slate-200 flex justify-end gap-3">
              <button
                onClick={() => setShowCreateDialog(false)}
                className="px-4 py-2 border border-slate-200 text-slate-700 rounded-lg hover:bg-slate-50 font-medium transition-colors"
                disabled={isCreating}
              >
                Cancel
              </button>
              <button
                onClick={handleCreateBridge}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 font-medium transition-colors disabled:bg-blue-400 disabled:cursor-not-allowed"
                disabled={isCreating}
              >
                {isCreating ? 'Creating...' : 'Create Bridge'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Auto Bridge Confirmation */}
      {showAutoConfirm && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl max-w-md w-full mx-4 p-6">
            <h3 className="text-lg font-semibold text-slate-900 mb-2">
              Create Auto Bridge
            </h3>
            <p className="text-slate-600 mb-6">
              This will automatically create a bridge on the transport with the best signal quality.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setShowAutoConfirm(false)}
                className="px-4 py-2 border border-slate-200 text-slate-700 rounded-lg hover:bg-slate-50 font-medium transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleAutoBridge}
                className="px-4 py-2 bg-amber-600 text-white rounded-lg hover:bg-amber-700 font-medium transition-colors"
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default BridgeManager;
