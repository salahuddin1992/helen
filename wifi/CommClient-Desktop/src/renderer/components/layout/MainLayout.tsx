import React from 'react';
import { Outlet } from 'react-router-dom';
import { TitleBar } from './TitleBar';
import { Sidebar } from './Sidebar';
import { useCallStore } from '@/stores/call.store.v2';
import { Phone } from 'lucide-react';

/**
 * ActiveCallBanner — shows a thin banner when user is in an active call,
 * allowing quick access to call controls even when browsing other pages.
 */
const ActiveCallBanner: React.FC = () => {
  const callStatus = useCallStore((s) => s.status);
  const callId = useCallStore((s) => s.callId);
  const isMuted = useCallStore((s) => s.isMuted);
  const callType = useCallStore((s) => s.type);

  if (callStatus !== 'active' && callStatus !== 'connecting' && callStatus !== 'reconnecting') {
    return null;
  }

  return (
    <div className="bg-green-600/90 text-white px-4 py-1.5 flex items-center justify-between text-xs font-medium">
      <div className="flex items-center gap-2">
        <Phone size={14} className="animate-pulse" />
        <span>
          {callStatus === 'connecting'
            ? 'Connecting...'
            : callStatus === 'reconnecting'
              ? 'Reconnecting...'
              : `In ${callType} call`}
        </span>
        {isMuted && (
          <span className="bg-red-500/80 px-1.5 py-0.5 rounded text-[10px]">MUTED</span>
        )}
      </div>
      <button
        onClick={() => useCallStore.getState().hangup()}
        className="bg-red-600 hover:bg-red-700 px-3 py-0.5 rounded text-xs transition-colors"
      >
        End Call
      </button>
    </div>
  );
};

export const MainLayout: React.FC = () => {
  return (
    <div className="h-screen w-screen flex flex-col bg-surface-900 text-white overflow-hidden">
      {/* Custom title bar for frameless window */}
      <TitleBar />

      {/* Active call banner (visible when navigating away from call view) */}
      <ActiveCallBanner />

      {/* Main content area */}
      <div className="flex-1 flex overflow-hidden">
        {/* Sidebar navigation */}
        <Sidebar />

        {/* Page content */}
        <div className="flex-1 overflow-auto">
          <Outlet />
        </div>
      </div>
    </div>
  );
};
