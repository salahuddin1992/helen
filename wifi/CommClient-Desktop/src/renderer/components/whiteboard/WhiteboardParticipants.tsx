/**
 * WhiteboardParticipants — Show participant cursors and participant list
 */
import React from 'react';
import { AlertCircle as CircleIcon, LogOut } from 'lucide-react';

export interface ParticipantCursor {
  userId: string;
  name: string;
  color: string;
  x: number;
  y: number;
  timestamp: number;
}

interface WhiteboardParticipantsProps {
  participants: ParticipantCursor[];
  onlineUsers: Array<{ id: string; name: string }>;
  onParticipantLeave?: (userId: string) => void;
}

const COLORS = [
  '#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A', '#98D8C8',
  '#F7DC6F', '#BB8FCE', '#85C1E2', '#F8B88B', '#52C4A1',
];

export const WhiteboardParticipants: React.FC<WhiteboardParticipantsProps> = ({
  participants,
  onlineUsers,
  onParticipantLeave,
}) => {
  const getColorForUser = (userId: string, index: number) => {
    return COLORS[index % COLORS.length];
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Cursors Overlay */}
      <div className="relative h-96 overflow-hidden rounded-lg border border-gray-300 bg-gray-50">
        <div className="relative w-full h-full">
          {participants.map((cursor, index) => (
            <div
              key={cursor.userId}
              style={{
                left: `${cursor.x}px`,
                top: `${cursor.y}px`,
                position: 'absolute',
              }}
              className="pointer-events-none transition-all duration-75"
            >
              <div
                className="h-4 w-4 rounded-full border-2 border-white shadow-lg"
                style={{
                  backgroundColor: getColorForUser(cursor.userId, index),
                }}
              />
              <div className="mt-1 rounded bg-gray-800 px-2 py-1 text-xs text-white whitespace-nowrap">
                {cursor.name}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Participant List */}
      <div className="rounded-lg bg-gray-100 p-3">
        <h3 className="mb-2 text-sm font-semibold text-gray-700">
          Participants ({onlineUsers.length})
        </h3>
        <div className="space-y-2">
          {onlineUsers.map((user, index) => (
            <div
              key={user.id}
              className="flex items-center justify-between rounded bg-white p-2"
            >
              <div className="flex items-center gap-2">
                <CircleIcon
                  size={12}
                  className="flex-shrink-0"
                  style={{
                    color: getColorForUser(user.id, index),
                    fill: getColorForUser(user.id, index),
                  }}
                />
                <span className="text-sm text-gray-700">{user.name}</span>
              </div>
              <button
                onClick={() => onParticipantLeave?.(user.id)}
                className="p-1 text-gray-400 hover:text-red-500 transition-colors"
                title="Remove participant"
              >
                <LogOut size={14} />
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
