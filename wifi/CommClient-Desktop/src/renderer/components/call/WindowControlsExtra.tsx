/**
 * WindowControlsExtra — small chip cluster with two power-user
 * controls:
 *   - Pin (always-on-top): keeps the call window above other apps.
 *   - Compact mode: shrinks to a 360x240 floating tile pinned to
 *     the corner. Toggling off restores the previous bounds.
 *
 * Both invoke Electron-side IPC handlers exposed via the preload.
 * When ``window.electronAPI`` isn't available (e.g. browser dev
 * server) the chip group renders nothing.
 */

import React, { useState } from 'react';
import { useCallStore } from '@/stores/call.store.v2';
import { Pin, Minimize2 } from 'lucide-react';

const WindowControlsExtra: React.FC = () => {
  const status = useCallStore((s) => s.status);
  const [pinned, setPinned] = useState(false);
  const [compact, setCompact] = useState(false);

  const winApi = (typeof window !== 'undefined'
    && (window as any).electronAPI?.window) as
      | { toggleAlwaysOnTop?: () => Promise<boolean>;
          setAlwaysOnTop?: (on: boolean) => Promise<boolean>;
          setCompact?: (on: boolean) => Promise<boolean>; }
      | undefined;

  if (!winApi || (status !== 'active' && status !== 'reconnecting')) {
    return null;
  }

  const togglePin = async () => {
    try {
      const next = await winApi.toggleAlwaysOnTop?.();
      if (typeof next === 'boolean') setPinned(next);
    } catch { /* ignore */ }
  };

  const toggleCompact = async () => {
    try {
      const next = !compact;
      await winApi.setCompact?.(next);
      setCompact(next);
      // Compact implies pinned at the OS level too — keep our
      // local pinned state in sync.
      if (next) setPinned(true);
    } catch { /* ignore */ }
  };

  return (
    <div className="fixed top-4 right-72 z-30 flex items-center gap-1
                    bg-black/60 hover:bg-black/80 rounded-full p-0.5
                    backdrop-blur shadow-lg">
      <button
        onClick={togglePin}
        className={`p-1.5 rounded-full transition-colors ${
          pinned
            ? 'bg-blue-500 text-white'
            : 'text-white/80 hover:text-white'
        }`}
        title={pinned ? 'إلغاء التثبيت فوق التطبيقات' : 'تثبيت فوق التطبيقات'}
      >
        <Pin size={12} />
      </button>
      <button
        onClick={toggleCompact}
        className={`p-1.5 rounded-full transition-colors ${
          compact
            ? 'bg-blue-500 text-white'
            : 'text-white/80 hover:text-white'
        }`}
        title={compact ? 'إنهاء الوضع المضغوط' : 'وضع مضغوط (نافذة صغيرة)'}
      >
        <Minimize2 size={12} />
      </button>
    </div>
  );
};

export default WindowControlsExtra;
