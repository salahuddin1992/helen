/**
 * WhiteboardSession — Full whiteboard page combining Canvas + Toolbar + Participants
 * Real-time sync via socket, join/leave lifecycle
 */
import React, { useEffect, useState } from 'react';
import { useWhiteboardStore } from '../../stores/whiteboard.store';
import { socketManager } from '../../services/socket.manager';
import { WhiteboardCanvas } from './WhiteboardCanvas';
import { WhiteboardToolbar } from './WhiteboardToolbar';
import { WhiteboardParticipants, type ParticipantCursor } from './WhiteboardParticipants';
import type { Stroke } from '../../stores/whiteboard.store';

interface WhiteboardSessionProps {
  sessionId: string;
  userId: string;
  userName: string;
  onLeave?: () => void;
}

type DrawingTool = 'pen' | 'eraser' | 'line' | 'rectangle' | 'circle' | 'text';

export const WhiteboardSession: React.FC<WhiteboardSessionProps> = ({
  sessionId,
  userId,
  userName,
  onLeave,
}) => {
  const store = useWhiteboardStore();
  const [selectedTool, setSelectedTool] = useState<DrawingTool>('pen');
  const [selectedColor, setSelectedColor] = useState('#000000');
  const [selectedWidth, setSelectedWidth] = useState(2);
  const [participantCursors, setParticipantCursors] = useState<ParticipantCursor[]>([]);

  // Join session
  useEffect(() => {
    socketManager.emit('whiteboard:join', {
      session_id: sessionId,
      user_id: userId,
      user_name: userName,
    });

    // Listen for strokes from other participants
    const unsubscribeStroke = socketManager.on(
      'whiteboard:stroke',
      (stroke: any) => {
        store.addStroke(stroke);
      }
    );

    // Listen for participant cursor updates
    const unsubscribeCursor = socketManager.on(
      'whiteboard:cursor',
      (data: ParticipantCursor) => {
        setParticipantCursors((prev) => {
          const filtered = prev.filter((c) => c.userId !== data.userId);
          return [...filtered, data];
        });
      }
    );

    // Listen for session state
    const unsubscribeState = socketManager.on(
      'whiteboard:state',
      (data: any) => {
        store.setStrokes(data.strokes || []);
        store.setParticipants(data.participants || []);
      }
    );

    return () => {
      unsubscribeStroke();
      unsubscribeCursor();
      unsubscribeState();

      // Leave session on cleanup
      socketManager.emit('whiteboard:leave', { session_id: sessionId });
    };
  }, [sessionId, userId, userName]);

  // Emit cursor position on mouse move
  const handleMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    socketManager.emit('whiteboard:cursor', {
      session_id: sessionId,
      user_id: userId,
      name: userName,
      x,
      y,
    });
  };

  const handleStrokeAdded = (stroke: any) => {
    store.addStroke(stroke);
    socketManager.emit('whiteboard:stroke', {
      session_id: sessionId,
      stroke,
    });
  };

  const handleClear = () => {
    store.clear();
    socketManager.emit('whiteboard:clear', { session_id: sessionId });
  };

  const handleUndo = () => {
    store.undo();
    socketManager.emit('whiteboard:undo', { session_id: sessionId });
  };

  const handleRedo = () => {
    store.redo();
    socketManager.emit('whiteboard:redo', { session_id: sessionId });
  };

  const handleExport = () => {
    // Prefer the dedicated whiteboard canvas — querying for *any*
    // <canvas> would return whichever one mounted first (e.g. an
    // active-call participant tile). Fall back to the first canvas
    // in the document if our scoped lookup fails.
    const canvas = (document.querySelector(
      '.whiteboard-canvas-root canvas',
    ) as HTMLCanvasElement | null)
      ?? (document.querySelector('canvas') as HTMLCanvasElement | null);
    if (!canvas) return;

    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `whiteboard-${stamp}.png`;
    const dl = (window as any).electronAPI?.downloads;

    // Electron path — write the PNG bytes to ~/Downloads via IPC so
    // the file lands in a predictable place + survives a route change.
    if (dl?.saveBuffer) {
      canvas.toBlob(async (blob) => {
        if (!blob) return;
        try {
          const buf = await blob.arrayBuffer();
          await dl.saveBuffer(filename, buf);
        } catch { /* best-effort; browser fallback below covers it */ }
      }, 'image/png');
      return;
    }

    // Browser-mode fallback — anchor download.
    const link = document.createElement('a');
    link.href = canvas.toDataURL('image/png');
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  return (
    <div className="flex h-screen gap-4 bg-gray-50 p-4">
      {/* Main Canvas Area */}
      <div className="flex-1 flex flex-col gap-4" onMouseMove={handleMouseMove}>
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold text-gray-900">Whiteboard Session</h2>
          <button
            onClick={() => {
              onLeave?.();
            }}
            className="rounded bg-red-500 px-4 py-2 text-sm font-medium text-white hover:bg-red-600"
          >
            Leave Session
          </button>
        </div>

        <div className="whiteboard-canvas-root flex-1 overflow-auto
                        rounded-lg border border-gray-300 bg-white">
          <WhiteboardCanvas
            strokes={store.strokes as any}
            selectedTool={selectedTool}
            selectedColor={selectedColor}
            selectedWidth={selectedWidth}
            onStrokeAdded={handleStrokeAdded}
            onClear={handleClear}
            onUndo={handleUndo}
          />
        </div>
      </div>

      {/* Right Sidebar */}
      <div className="flex w-72 flex-col gap-4">
        <WhiteboardToolbar
          selectedTool={selectedTool}
          selectedColor={selectedColor}
          selectedWidth={selectedWidth}
          onToolChange={setSelectedTool}
          onColorChange={setSelectedColor}
          onWidthChange={setSelectedWidth}
          onExport={handleExport}
          onUndo={handleUndo}
          onRedo={handleRedo}
          onClear={handleClear}
          canUndo={store.undoStack.length > 0}
          canRedo={store.redoStack.length > 0}
        />

        <div className="flex-1 overflow-auto">
          <WhiteboardParticipants
            participants={participantCursors}
            onlineUsers={store.participants}
          />
        </div>
      </div>
    </div>
  );
};
