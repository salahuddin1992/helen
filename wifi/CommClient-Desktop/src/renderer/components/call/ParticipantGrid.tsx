/**
 * ParticipantGrid.tsx — Dynamic video grid for group calls.
 *
 * Auto-layout: 1 = full screen, 2 = side-by-side, 3-4 = 2x2, 5-6 = 3x2, 7-9 = 3x3
 * Features: active speaker border, pin/unpin, video tile mute indicators
 */

import React, { useMemo } from 'react';
import { MicOff, VideoOff, AlertCircle } from 'lucide-react';
import { Avatar } from '../common/Avatar';

export interface Participant {
  userId: string;
  displayName: string;
  stream: MediaStream | null;
  isMuted: boolean;
  isVideoOff: boolean;
  isScreenSharing: boolean;
}

interface ParticipantGridProps {
  participants: Participant[];
  localStream: MediaStream | null;
  activeSpeaker?: string;
  pinnedUser?: string | null;
  onPin: (userId: string | null) => void;
}

const ParticipantTile: React.FC<{
  participant: Participant | { isLocal: true; stream: MediaStream | null };
  isActive: boolean;
  isPinned: boolean;
  onPin: () => void;
  className: string;
}> = ({ participant, isActive, isPinned, onPin, className }) => {
  const videoRef = React.useRef<HTMLVideoElement>(null);
  const isLocal = 'isLocal' in participant;
  const displayName = isLocal ? 'You' : participant.displayName;
  const stream = isLocal ? participant.stream : participant.stream;
  const isMuted = !isLocal && participant.isMuted;
  const isVideoOff = !isLocal && participant.isVideoOff;
  const isScreenSharing = !isLocal && participant.isScreenSharing;
  // Map connection quality → border color. Engine sets `quality` on
  // each Participant from RTCStats / call_quality_report aggregations.
  const quality = !isLocal ? (participant as any).quality : undefined;
  const qualityRing =
    quality === 'poor' ? 'ring-2 ring-red-500/80' :
    quality === 'fair' ? 'ring-2 ring-yellow-500/80' :
    quality === 'good' ? 'ring-2 ring-green-500/60' :
    '';

  React.useEffect(() => {
    if (videoRef.current && stream) {
      videoRef.current.srcObject = stream;
      videoRef.current.play().catch(() => {
        // Autoplay might be blocked, user interaction required
      });
    }
  }, [stream]);

  return (
    <div
      className={`${className} relative bg-surface-900 rounded-lg overflow-hidden cursor-pointer group transition-all duration-200 ${
        isPinned ? 'ring-2 ring-primary-500 shadow-lg shadow-primary-500/20' : ''
      } ${isActive ? 'ring-2 ring-primary-500 shadow-lg shadow-primary-500/30' : ''} ${
        !isPinned && !isActive ? qualityRing : ''
      }`}
      onClick={onPin}
    >
      {/* Video stream */}
      {stream && !isVideoOff ? (
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="w-full h-full object-cover"
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-surface-800 to-surface-900">
          <div className="flex flex-col items-center gap-2">
            <Avatar name={displayName} size="lg" />
            <span className="text-xs text-text-400">{displayName}</span>
          </div>
        </div>
      )}

      {/* Overlay on hover */}
      <div className="absolute inset-0 bg-black/0 group-hover:bg-black/20 transition-colors duration-200 flex items-center justify-center opacity-0 group-hover:opacity-100">
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPin();
          }}
          className="px-3 py-1.5 bg-primary-500 hover:bg-primary-600 text-white text-xs font-medium rounded transition-colors"
        >
          {isPinned ? 'Unpin' : 'Pin'}
        </button>
      </div>

      {/* Status indicators */}
      <div className="absolute bottom-2 left-2 flex flex-col gap-1">
        {isMuted && (
          <div
            className="flex items-center gap-1 bg-black/60 backdrop-blur px-2 py-1 rounded-full"
            title="Muted"
          >
            <MicOff size={14} className="text-red-400" />
          </div>
        )}
        {isScreenSharing && (
          <div
            className="flex items-center gap-1 bg-black/60 backdrop-blur px-2 py-1 rounded-full"
            title="Screen Sharing"
          >
            <AlertCircle size={14} className="text-green-400" />
          </div>
        )}
      </div>

      {/* Display name (always visible) */}
      <div className="absolute top-2 left-2 right-2 flex justify-between items-start">
        <span className="text-xs font-medium text-white bg-black/40 backdrop-blur px-2 py-1 rounded">
          {displayName}
          {isLocal && ' (You)'}
        </span>
      </div>

      {/* Active speaker pulse animation */}
      {isActive && (
        <div className="absolute inset-0 rounded-lg border-2 border-primary-500 pointer-events-none animate-pulse" />
      )}
    </div>
  );
};

const ParticipantGrid: React.FC<ParticipantGridProps> = ({
  participants,
  localStream,
  activeSpeaker,
  pinnedUser,
  onPin,
}) => {
  const allParticipants = useMemo(() => {
    const list: (Participant | { isLocal: true; stream: MediaStream | null })[] = [
      { isLocal: true, stream: localStream },
      ...participants,
    ];
    return list;
  }, [participants, localStream]);

  const totalCount = allParticipants.length;

  // Calculate grid layout based on participant count
  const gridConfig = useMemo(() => {
    if (totalCount === 1) {
      return { cols: 1, rows: 1, tileClass: 'col-span-1 row-span-1' };
    }
    if (totalCount === 2) {
      return { cols: 2, rows: 1, tileClass: 'col-span-1 row-span-1' };
    }
    if (totalCount <= 4) {
      return { cols: 2, rows: 2, tileClass: 'col-span-1 row-span-1' };
    }
    if (totalCount <= 6) {
      return { cols: 3, rows: 2, tileClass: 'col-span-1 row-span-1' };
    }
    return { cols: 3, rows: 3, tileClass: 'col-span-1 row-span-1' };
  }, [totalCount]);

  // Handle pinned layout: pinned on left (larger), rest in grid on right
  const pinnedParticipant = allParticipants.find(
    (p) => ('userId' in p ? p.userId : false) === pinnedUser
  );

  return (
    <div className="w-full h-full bg-surface-950 rounded-lg overflow-hidden flex">
      {/* Pinned participant (if any) */}
      {pinnedParticipant && pinnedUser && (
        <div className="w-2/3 h-full">
          <ParticipantTile
            participant={pinnedParticipant as Participant}
            isActive={
              'userId' in pinnedParticipant
                ? pinnedParticipant.userId === activeSpeaker
                : false
            }
            isPinned={true}
            onPin={() => onPin(null)}
            className="w-full h-full"
          />
        </div>
      )}

      {/* AlertCircle for remaining participants */}
      <div
        className={`${pinnedParticipant && pinnedUser ? 'w-1/3' : 'w-full'} h-full p-2 bg-surface-900 overflow-auto`}
      >
        <div
          className={`grid gap-2 h-fit`}
          style={{
            gridTemplateColumns: `repeat(${
              pinnedParticipant && pinnedUser ? 2 : gridConfig.cols
            }, minmax(0, 1fr))`,
            gridAutoRows: `minmax(120px, 1fr)`,
          }}
        >
          {allParticipants.map((p, idx) => {
            // ChevronRight pinned participant in grid
            if (pinnedUser && 'userId' in p && p.userId === pinnedUser) {
              return null;
            }

            const isLocal = 'isLocal' in p;
            const userId = isLocal ? 'local' : p.userId;

            return (
              <ParticipantTile
                key={userId}
                participant={p}
                isActive={
                  isLocal
                    ? false
                    : (p as Participant).userId === activeSpeaker
                }
                isPinned={userId === pinnedUser}
                onPin={() =>
                  onPin(isLocal ? null : (p as Participant).userId)
                }
                className={`${gridConfig.tileClass} min-h-[120px]`}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
};

export default ParticipantGrid;
