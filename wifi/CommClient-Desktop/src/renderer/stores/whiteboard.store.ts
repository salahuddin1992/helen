/**
 * Whiteboard Store — Zustand store for whiteboard state
 */
import { create } from 'zustand';

export interface Stroke {
  id: string;
  tool: string;
  color: string;
  width: number;
  points?: Array<{ x: number; y: number }>;
  startX?: number;
  startY?: number;
  endX?: number;
  endY?: number;
  text?: string;
  fontSize?: number;
  x?: number;
  y?: number;
}

interface Participant {
  id: string;
  name: string;
}

interface WhiteboardState {
  activeSessionId: string | null;
  strokes: Stroke[];
  participants: Participant[];
  cursorPositions: Map<string, { x: number; y: number }>;
  selectedTool: string;
  selectedColor: string;
  selectedWidth: number;
  undoStack: Stroke[][];
  redoStack: Stroke[][];

  // Actions
  setActiveSession: (sessionId: string | null) => void;
  addStroke: (stroke: Stroke) => void;
  setStrokes: (strokes: Stroke[]) => void;
  undo: () => void;
  redo: () => void;
  clear: () => void;
  setParticipants: (participants: Participant[]) => void;
  updateCursorPosition: (userId: string, x: number, y: number) => void;
  setSelectedTool: (tool: string) => void;
  setSelectedColor: (color: string) => void;
  setSelectedWidth: (width: number) => void;
}

export const useWhiteboardStore = create<WhiteboardState>((set, get) => ({
  activeSessionId: null,
  strokes: [],
  participants: [],
  cursorPositions: new Map(),
  selectedTool: 'pen',
  selectedColor: '#000000',
  selectedWidth: 2,
  undoStack: [],
  redoStack: [],

  setActiveSession: (sessionId) => {
    set({
      activeSessionId: sessionId,
      strokes: [],
      participants: [],
      undoStack: [],
      redoStack: [],
    });
  },

  addStroke: (stroke) => {
    // A new stroke breaks the redo lineage — same convention as
    // every editor: typing after Undo discards the redo branch.
    set((state) => ({
      strokes: [...state.strokes, stroke],
      undoStack: [...state.undoStack, state.strokes],
      redoStack: [],
    }));
  },

  setStrokes: (strokes) => {
    set({ strokes });
  },

  undo: () => {
    set((state) => {
      if (state.undoStack.length === 0) return state;

      const newStack = [...state.undoStack];
      const previousStrokes = newStack.pop() || [];

      return {
        strokes: previousStrokes,
        undoStack: newStack,
        // Push the *current* (pre-undo) strokes onto the redo stack
        // so a subsequent redo() can put them back.
        redoStack: [...state.redoStack, state.strokes],
      };
    });
  },

  redo: () => {
    set((state) => {
      if (state.redoStack.length === 0) return state;

      const newRedo = [...state.redoStack];
      const nextStrokes = newRedo.pop() || [];

      return {
        strokes: nextStrokes,
        redoStack: newRedo,
        undoStack: [...state.undoStack, state.strokes],
      };
    });
  },

  clear: () => {
    set({
      strokes: [],
      undoStack: [],
      redoStack: [],
    });
  },

  setParticipants: (participants) => {
    set({ participants });
  },

  updateCursorPosition: (userId, x, y) => {
    set((state) => {
      const newPositions = new Map(state.cursorPositions);
      newPositions.set(userId, { x, y });
      return { cursorPositions: newPositions };
    });
  },

  setSelectedTool: (tool) => {
    set({ selectedTool: tool });
  },

  setSelectedColor: (color) => {
    set({ selectedColor: color });
  },

  setSelectedWidth: (width) => {
    set({ selectedWidth: width });
  },
}));
