/**
 * WhiteboardPage — route wrapper around WhiteboardSession.
 *
 * Reads sessionId from the URL (`/whiteboard/:id`) so the same session can
 * be opened from any context: a launch button in CallControls, a deep link
 * pasted in chat, or a direct navigation. The session is per-channel by
 * convention — `/whiteboard/<channelId>` keeps everyone on the same board
 * for the same conversation.
 */
import React from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { WhiteboardSession } from '@/components/whiteboard/WhiteboardSession';
import { useAuthStore } from '@/stores/auth.store';

const WhiteboardPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  if (!id) {
    return (
      <div className="p-8 text-center text-gray-400">
        Whiteboard session id missing in URL.
      </div>
    );
  }

  if (!user) {
    return (
      <div className="p-8 text-center text-gray-400">
        Sign-in required to open the whiteboard.
      </div>
    );
  }

  return (
    <div className="h-full w-full flex flex-col bg-surface-900">
      <div className="px-4 py-2 border-b border-surface-800 flex items-center gap-3 shrink-0">
        <button
          onClick={() => navigate(-1)}
          className="p-1.5 hover:bg-surface-800 rounded text-gray-300"
          title="Back"
        >
          <ArrowLeft size={18} />
        </button>
        <h1 className="text-sm font-semibold text-white">Whiteboard</h1>
        <span className="text-xs text-gray-500 font-mono">{id.slice(0, 12)}…</span>
      </div>
      <div className="flex-1 overflow-hidden">
        <WhiteboardSession
          sessionId={id}
          userId={user.id}
          userName={user.display_name || user.username}
          onLeave={() => navigate(-1)}
        />
      </div>
    </div>
  );
};

export default WhiteboardPage;
