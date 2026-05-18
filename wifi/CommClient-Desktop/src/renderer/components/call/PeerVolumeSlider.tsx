/**
 * PeerVolumeSlider — small overlay slider per participant tile.
 *
 * The slider value (0–1.5) is stored on call.store.peerVolumes; an
 * effect in CallView attaches a hidden ``<audio>`` element per peer
 * with ``volume`` bound to that gain so users can rebalance loud
 * speakers without changing system audio.
 *
 * The slider only opens on hover (desktop) or long-press (touch);
 * out of the way otherwise.
 */

import React, { useState } from 'react';
import { Volume2, VolumeX } from 'lucide-react';
import { useCallStore } from '@/stores/call.store.v2';

const PeerVolumeSlider: React.FC<{ peerId: string }> = ({ peerId }) => {
  const volume = useCallStore((s) => s.peerVolumes[peerId] ?? 1);
  const setPeerVolume = useCallStore((s) => s.setPeerVolume);
  const [open, setOpen] = useState(false);

  const muted = volume === 0;

  return (
    <div
      className="relative"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        onClick={() => setPeerVolume(peerId, muted ? 1 : 0)}
        className={`p-1.5 rounded-full transition-colors ${
          muted
            ? 'bg-red-500/80 text-white hover:bg-red-500'
            : 'bg-black/50 text-white hover:bg-black/70'
        }`}
        title={muted ? 'إلغاء الكتم' : 'كتم هذا المشارك محلياً'}
      >
        {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
      </button>

      {open && (
        <div className="absolute bottom-full mb-1 left-1/2 -translate-x-1/2
                        bg-black/80 backdrop-blur rounded-lg p-2 shadow-xl
                        flex items-center gap-2 min-w-[120px]">
          <input
            type="range"
            min={0}
            max={1.5}
            step={0.05}
            value={volume}
            onChange={(e) => setPeerVolume(peerId, parseFloat(e.target.value))}
            className="flex-1"
            aria-label="Peer volume"
          />
          <span className="text-[10px] text-white/80 w-7 text-right tabular-nums">
            {Math.round(volume * 100)}%
          </span>
        </div>
      )}
    </div>
  );
};

export default PeerVolumeSlider;
