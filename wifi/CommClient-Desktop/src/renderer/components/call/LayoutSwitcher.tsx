/**
 * LayoutSwitcher — three-way toggle for the call view layout.
 *
 * Renders as a small pill in the top-right (left of the participant
 * search chip) with three icons. Clicking an icon switches; the
 * selection persists to localStorage so it survives reloads.
 *
 * Keyboard shortcut: Ctrl+Shift+L cycles through modes.
 */

import React, { useEffect } from 'react';
import { useLayoutStore, type LayoutMode } from '@/stores/layout.store';
import { useCallStore } from '@/stores/call.store.v2';

const GalleryIcon: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);
const SpeakerIcon: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="18" height="18" rx="2" />
  </svg>
);
const SidebarIcon: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="13" height="18" rx="1" />
    <rect x="18" y="3"  width="3" height="5" rx="0.5" />
    <rect x="18" y="9"  width="3" height="5" rx="0.5" />
    <rect x="18" y="15" width="3" height="5" rx="0.5" />
  </svg>
);

const items: Array<{ mode: LayoutMode; label: string; Icon: React.FC<{ size?: number }> }> = [
  { mode: 'gallery', label: 'شبكة',  Icon: GalleryIcon },
  { mode: 'speaker', label: 'متحدث', Icon: SpeakerIcon },
  { mode: 'sidebar', label: 'سايد',  Icon: SidebarIcon },
];

const LayoutSwitcher: React.FC = () => {
  const layout = useLayoutStore((s) => s.layout);
  const setLayout = useLayoutStore((s) => s.setLayout);
  const cycle = useLayoutStore((s) => s.cycleLayout);
  const status = useCallStore((s) => s.status);

  // Ctrl+Shift+L cycles through modes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'L' || e.key === 'l')) {
        e.preventDefault();
        cycle();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cycle]);

  if (status !== 'active' && status !== 'reconnecting') return null;

  return (
    <div className="fixed top-4 right-32 z-30 bg-black/60 hover:bg-black/80
                    rounded-full p-0.5 flex shadow-lg backdrop-blur"
         title="تخطيط (Ctrl+Shift+L)">
      {items.map(({ mode, label, Icon }) => {
        const active = layout === mode;
        return (
          <button
            key={mode}
            onClick={() => setLayout(mode)}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px]
                        font-medium transition-colors ${
              active
                ? 'bg-white/20 text-white'
                : 'text-white/60 hover:text-white/90'
            }`}
            title={label}
          >
            <Icon size={12} />
            <span className="hidden md:inline">{label}</span>
          </button>
        );
      })}
    </div>
  );
};

export default LayoutSwitcher;
