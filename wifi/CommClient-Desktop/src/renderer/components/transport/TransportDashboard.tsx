/**
 * TransportDashboard.tsx — Main transport management interface.
 *
 * 4-tab interface:
 *   1. Catalog — Browse all 500+ transport types
 *   2. Detected — Auto-detected transports on local network
 *   3. Bridges — Active communication bridges
 *   4. AlertCircle AlertCircle — Real-time signal quality metrics
 */

import React, { useState, useEffect } from 'react';
// @ts-ignore
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  AlertCircle,
  Bell,
  AlertTriangle,
} from 'lucide-react';
import { useTransportStore } from '@/stores/transport.store';
import TransportCatalog from './TransportCatalog';
import DetectedTransports from './DetectedTransports';
import BridgeManager from './BridgeManager';
import SignalMonitor from './SignalMonitor';

const TransportDashboard: React.FC = () => {
  const [activeTab, setActiveTab] = useState('catalog');
  const {
    categories,
    transports,
    detectedTransports,
    activeBridges,
    isScanning,
    error,
    loadCategories,
    loadAllTransports,
    getDetectedTransports,
    listBridges,
    setError,
  } = useTransportStore();

  useEffect(() => {
    // Load initial data
    loadCategories();
    loadAllTransports();
    getDetectedTransports();
    listBridges();
  }, [loadCategories, loadAllTransports, getDetectedTransports, listBridges]);

  // Auto-refresh detected transports every 30 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      if (activeTab === 'detected') {
        getDetectedTransports();
      }
    }, 30000);
    return () => clearInterval(interval);
  }, [activeTab, getDetectedTransports]);

  return (
    <div className="w-full h-full flex flex-col bg-slate-50">
      {/* Header */}
      <div className="px-6 py-4 bg-white border-b border-slate-200 shadow-sm">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
              <AlertCircle className="w-7 h-7 text-blue-600" />
              Transport Layer Manager
            </h1>
            <p className="text-sm text-slate-600 mt-1">
              Manage network transports, detect interfaces, and configure bridges
            </p>
          </div>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="mx-6 mt-4 p-4 bg-red-50 border border-red-200 rounded-lg flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm font-medium text-red-900">{error}</p>
          </div>
          <button
            onClick={() => setError(null)}
            className="text-red-600 hover:text-red-700 font-medium text-sm"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex-1 overflow-auto">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full h-full flex flex-col">
          <TabsList className="w-full px-6 py-0 bg-white border-b border-slate-200 rounded-none h-auto gap-0">
            <TabsTrigger
              value="catalog"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600 flex items-center gap-2 px-4 py-3"
            >
              <AlertCircle className="w-4 h-4" />
              Catalog
            </TabsTrigger>
            <TabsTrigger
              value="detected"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600 flex items-center gap-2 px-4 py-3"
            >
              <Bell className="w-4 h-4" />
              Detected
              {detectedTransports.length > 0 && (
                <span className="ml-2 px-2 py-0.5 bg-green-100 text-green-700 rounded-full text-xs font-medium">
                  {detectedTransports.length}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger
              value="bridges"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600 flex items-center gap-2 px-4 py-3"
            >
              <AlertCircle className="w-4 h-4" />
              Bridges
              {activeBridges.length > 0 && (
                <span className="ml-2 px-2 py-0.5 bg-blue-100 text-blue-700 rounded-full text-xs font-medium">
                  {activeBridges.length}
                </span>
              )}
            </TabsTrigger>
            <TabsTrigger
              value="signal"
              className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600 flex items-center gap-2 px-4 py-3"
            >
              <AlertCircle className="w-4 h-4" />
              AlertCircle AlertCircle
            </TabsTrigger>
          </TabsList>

          {/* Tab Content */}
          <div className="flex-1 overflow-auto px-6 py-4">
            <TabsContent value="catalog" className="mt-0 h-full">
              <TransportCatalog transports={transports} categories={categories} />
            </TabsContent>

            <TabsContent value="detected" className="mt-0 h-full">
              <DetectedTransports transports={detectedTransports} isScanning={isScanning} />
            </TabsContent>

            <TabsContent value="bridges" className="mt-0 h-full">
              <BridgeManager bridges={activeBridges} detectedTransports={detectedTransports} />
            </TabsContent>

            <TabsContent value="signal" className="mt-0 h-full">
              <SignalMonitor detectedTransports={detectedTransports} />
            </TabsContent>
          </div>
        </Tabs>
      </div>
    </div>
  );
};

export default TransportDashboard;
