/**
 * TransportCatalog.tsx — Browse all transports from catalog.
 *
 * Features:
 *   - Category sidebar with counts
 *   - Search bar with filtering
 *   - Transport grid/list with details
 *   - Expandable cards for full specs
 */

import React, { useState, useMemo } from 'react';
import { Search, ChevronDown, ChevronRight, Wifi, AlertTriangle, AlertCircle } from 'lucide-react';
import type { TransportDefinition } from '@/stores/transport.store';

interface TransportCatalogProps {
  transports: TransportDefinition[];
  categories: Array<{ name: string; count: number }>;
}

const TransportCatalog: React.FC<TransportCatalogProps> = ({ transports, categories }) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [expandedTransport, setExpandedTransport] = useState<string | null>(null);

  // Filter transports
  const filtered = useMemo(() => {
    return transports.filter((t) => {
      const matchesSearch =
        !searchQuery ||
        t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        t.description.toLowerCase().includes(searchQuery.toLowerCase());

      const matchesCategory = !selectedCategory || t.category === selectedCategory;

      return matchesSearch && matchesCategory;
    });
  }, [transports, searchQuery, selectedCategory]);

  const getMediumIcon = (medium: string) => {
    switch (medium) {
      case 'wireless':
        return <Wifi className="w-4 h-4" />;
      case 'wired':
        return <AlertCircle className="w-4 h-4" />;
      case 'optical':
        return <AlertCircle className="w-4 h-4" />;
      case 'usb':
        return <AlertTriangle className="w-4 h-4" />;
      default:
        return null;
    }
  };

  const getMediumLabel = (medium: string) => {
    return medium.charAt(0).toUpperCase() + medium.slice(1);
  };

  return (
    <div className="grid grid-cols-4 gap-6 h-full">
      {/* Sidebar */}
      <div className="col-span-1 flex flex-col gap-4 border-r border-slate-200 pr-6 overflow-y-auto">
        <div>
          <h3 className="font-semibold text-slate-900 mb-3">Categories</h3>
          <button
            onClick={() => setSelectedCategory(null)}
            className={`block w-full text-left px-3 py-2 rounded-lg text-sm mb-1 transition-colors ${
              selectedCategory === null
                ? 'bg-blue-100 text-blue-700 font-medium'
                : 'text-slate-700 hover:bg-slate-100'
            }`}
          >
            All Transports ({transports.length})
          </button>

          <div className="space-y-1">
            {categories.map((cat) => (
              <button
                key={cat.name}
                onClick={() => setSelectedCategory(cat.name)}
                className={`block w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                  selectedCategory === cat.name
                    ? 'bg-blue-100 text-blue-700 font-medium'
                    : 'text-slate-700 hover:bg-slate-100'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span>{cat.name}</span>
                  <span className="text-xs font-semibold text-slate-500 ml-2">
                    {cat.count}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="col-span-3 flex flex-col gap-4 overflow-y-auto">
        {/* Search */}
        <div className="relative sticky top-0 bg-slate-50 pb-2">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-slate-400" />
          <input
            type="text"
            placeholder="Search transports..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 bg-white border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {/* Transport List */}
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-center">
            <div>
              <p className="text-slate-600 font-medium">No transports found</p>
              <p className="text-sm text-slate-500 mt-1">
                Try adjusting your search or category filter
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {filtered.map((transport) => (
              <div
                key={transport.transport_id}
                className="bg-white border border-slate-200 rounded-lg overflow-hidden hover:border-blue-300 transition-colors"
              >
                {/* Header */}
                <button
                  onClick={() =>
                    setExpandedTransport(
                      expandedTransport === transport.transport_id
                        ? null
                        : transport.transport_id
                    )
                  }
                  className="w-full p-4 flex items-start gap-3 hover:bg-slate-50 transition-colors"
                >
                  {/* Icon */}
                  <div className="mt-0.5">
                    {getMediumIcon(transport.medium)}
                  </div>

                  {/* Content */}
                  <div className="flex-1 text-left">
                    <div className="flex items-center gap-2 mb-1">
                      <h4 className="font-semibold text-slate-900">{transport.name}</h4>
                      {transport.is_common && (
                        <span className="px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium">
                          Common
                        </span>
                      )}
                      {transport.requires_hardware && (
                        <span className="px-2 py-0.5 bg-amber-100 text-amber-700 rounded text-xs font-medium">
                          Hardware
                        </span>
                      )}
                    </div>
                    <p className="text-sm text-slate-600">{transport.description}</p>

                    {/* Quick specs */}
                    <div className="flex items-center gap-4 mt-2 text-xs text-slate-500">
                      <span>
                        <strong>Bandwidth:</strong> {transport.typical_bandwidth}
                      </span>
                      <span>
                        <strong>Latency:</strong> {transport.typical_latency}
                      </span>
                      {transport.typical_range && (
                        <span>
                          <strong>Range:</strong> {transport.typical_range}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Expand Button */}
                  <div className="text-slate-400">
                    {expandedTransport === transport.transport_id ? (
                      <ChevronDown className="w-5 h-5" />
                    ) : (
                      <ChevronRight className="w-5 h-5" />
                    )}
                  </div>
                </button>

                {/* Expanded Details */}
                {expandedTransport === transport.transport_id && (
                  <div className="border-t border-slate-200 bg-slate-50 p-4 space-y-3">
                    <div className="grid grid-cols-2 gap-4 text-sm">
                      <div>
                        <p className="text-slate-500 font-medium mb-1">Category</p>
                        <p className="text-slate-900">{transport.category}</p>
                      </div>
                      <div>
                        <p className="text-slate-500 font-medium mb-1">Medium</p>
                        <p className="text-slate-900 flex items-center gap-2">
                          {getMediumIcon(transport.medium)}
                          {getMediumLabel(transport.medium)}
                        </p>
                      </div>
                      <div>
                        <p className="text-slate-500 font-medium mb-1">Detection Method</p>
                        <p className="text-slate-900 text-xs">{transport.detection_method}</p>
                      </div>
                      <div>
                        <p className="text-slate-500 font-medium mb-1">Transport ID</p>
                        <p className="text-slate-900 font-mono text-xs">
                          {transport.transport_id}
                        </p>
                      </div>
                    </div>

                    <div>
                      <p className="text-slate-500 font-medium mb-2">Specifications</p>
                      <ul className="text-sm text-slate-600 space-y-1 ml-4 list-disc">
                        <li>Bandwidth: {transport.typical_bandwidth}</li>
                        <li>Latency: {transport.typical_latency}</li>
                        {transport.typical_range && (
                          <li>Range: {transport.typical_range}</li>
                        )}
                        <li>
                          {transport.requires_hardware
                            ? 'Requires special hardware'
                            : 'Standard hardware compatibility'}
                        </li>
                      </ul>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default TransportCatalog;
