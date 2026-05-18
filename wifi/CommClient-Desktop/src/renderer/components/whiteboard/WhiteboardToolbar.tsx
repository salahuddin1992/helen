/**
 * WhiteboardToolbar — Tool selection, color/width, undo/redo,
 * clear, export.
 *
 * Why most icons are inline SVG
 * -----------------------------
 * lucide-react 0.383's d.ts inconsistently exports its
 * pen / circle / square aliases (we tripped over Link/Disc/Hourglass
 * earlier). Rather than fight the type generator, the drawing-tool
 * icons here are tiny inline SVGs — readable, version-stable, and
 * cheap.
 */

import React from 'react';
import { Minus, Download, Trash2 } from 'lucide-react';

// Lucide d.ts in this version doesn't export Undo/Redo/RotateCw,
// so we inline both arrows.
const Undo2: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <path d="M3 7v6h6" />
    <path d="M21 17a9 9 0 0 0-15-6.7L3 13" />
  </svg>
);

const Redo2: React.FC<{ size?: number }> = ({ size = 14 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <path d="M21 7v6h-6" />
    <path d="M3 17a9 9 0 0 1 15-6.7L21 13" />
  </svg>
);

type DrawingTool = 'pen' | 'eraser' | 'line' | 'rectangle' | 'circle' | 'text';

interface WhiteboardToolbarProps {
  selectedTool: DrawingTool;
  selectedColor: string;
  selectedWidth: number;
  onToolChange: (tool: DrawingTool) => void;
  onColorChange: (color: string) => void;
  onWidthChange: (width: number) => void;
  onExport?: () => void;
  onUndo?: () => void;
  onRedo?: () => void;
  onClear?: () => void;
  /** Disable the undo button when the stack is empty. */
  canUndo?: boolean;
  canRedo?: boolean;
}

const COLORS = [
  '#000000', '#FFFFFF', '#FF0000', '#00FF00',
  '#0000FF', '#FFFF00', '#FF00FF', '#00FFFF',
];

// ── Inline-SVG icons for each drawing tool ─────────────────────

const PenIcon: React.FC<{ size?: number }> = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <path d="M12 19l7-7 3 3-7 7-3-3z" />
    <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z" />
    <path d="M2 2l7.586 7.586" />
    <circle cx="11" cy="11" r="2" />
  </svg>
);

const EraserIcon: React.FC<{ size?: number }> = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <path d="M20 20H7L3 16c-1-1-1-3 0-4l9-9c1-1 3-1 4 0l5 5c1 1 1 3 0 4z" />
    <path d="M5 14l7 7" />
  </svg>
);

const SquareIcon: React.FC<{ size?: number }> = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
    <rect x="4" y="4" width="16" height="16" rx="1" />
  </svg>
);

const CircleIcon: React.FC<{ size?: number }> = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2">
    <circle cx="12" cy="12" r="9" />
  </svg>
);

const TextIcon: React.FC<{ size?: number }> = ({ size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round"
       strokeLinejoin="round">
    <polyline points="4 7 4 4 20 4 20 7" />
    <line x1="9" y1="20" x2="15" y2="20" />
    <line x1="12" y1="4" x2="12" y2="20" />
  </svg>
);

interface ToolDef {
  id: DrawingTool;
  label: string;
  Icon: React.ComponentType<{ size?: number }>;
}

const TOOLS: ToolDef[] = [
  { id: 'pen', label: 'قلم', Icon: PenIcon },
  { id: 'eraser', label: 'ممحاة', Icon: EraserIcon },
  { id: 'line', label: 'خط', Icon: () => <Minus size={18} /> },
  { id: 'rectangle', label: 'مستطيل', Icon: SquareIcon },
  { id: 'circle', label: 'دائرة', Icon: CircleIcon },
  { id: 'text', label: 'نص', Icon: TextIcon },
];

export const WhiteboardToolbar: React.FC<WhiteboardToolbarProps> = ({
  selectedTool,
  selectedColor,
  selectedWidth,
  onToolChange,
  onColorChange,
  onWidthChange,
  onExport,
  onUndo,
  onRedo,
  onClear,
  canUndo = true,
  canRedo = false,
}) => {
  return (
    <div className="flex flex-col gap-4 rounded-lg bg-gray-100 p-4">
      {/* Tools */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-2">
          الأدوات
        </label>
        <div className="grid grid-cols-3 gap-2">
          {TOOLS.map((tool) => (
            <button
              key={tool.id}
              onClick={() => onToolChange(tool.id)}
              className={
                'flex items-center justify-center rounded p-2 ' +
                'transition-all ' +
                (selectedTool === tool.id
                  ? 'bg-blue-500 text-white'
                  : 'bg-white text-gray-700 hover:bg-gray-200')
              }
              title={tool.label}
              aria-label={tool.label}
            >
              <tool.Icon size={18} />
            </button>
          ))}
        </div>
      </div>

      {/* Colors */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-2">
          اللون
        </label>
        <div className="flex flex-wrap gap-2">
          {COLORS.map((color) => (
            <button
              key={color}
              onClick={() => onColorChange(color)}
              className={
                'h-8 w-8 rounded border-2 transition-all ' +
                (selectedColor === color
                  ? 'border-gray-900'
                  : 'border-transparent hover:border-gray-400')
              }
              style={{ backgroundColor: color }}
              title={color}
            />
          ))}
        </div>
      </div>

      {/* Stroke width */}
      <div>
        <label className="block text-xs font-semibold text-gray-700 mb-2">
          سُمك الخط: {selectedWidth}px
        </label>
        <input
          type="range"
          min="1"
          max="20"
          value={selectedWidth}
          onChange={(e) => onWidthChange(parseInt(e.target.value, 10))}
          className="w-full"
        />
      </div>

      {/* History controls — undo / redo / clear */}
      <div className="grid grid-cols-3 gap-2">
        <button
          onClick={onUndo}
          disabled={!canUndo}
          className="flex items-center justify-center gap-1 rounded
                     bg-gray-300 px-2 py-2 text-xs font-medium
                     text-gray-800 hover:bg-gray-400 disabled:opacity-40
                     disabled:cursor-not-allowed transition-all"
          title="تراجع (Ctrl+Z)"
        >
          <Undo2 size={14} />
          <span>تراجع</span>
        </button>
        <button
          onClick={onRedo}
          disabled={!canRedo}
          className="flex items-center justify-center gap-1 rounded
                     bg-gray-300 px-2 py-2 text-xs font-medium
                     text-gray-800 hover:bg-gray-400 disabled:opacity-40
                     disabled:cursor-not-allowed transition-all"
          title="إعادة (Ctrl+Y)"
        >
          <Redo2 size={14} />
          <span>إعادة</span>
        </button>
        <button
          onClick={onClear}
          className="flex items-center justify-center gap-1 rounded
                     bg-red-500/80 px-2 py-2 text-xs font-medium
                     text-white hover:bg-red-600 transition-all"
          title="مسح الكل"
        >
          <Trash2 size={14} />
          <span>مسح</span>
        </button>
      </div>

      {/* Export — saves as PNG via Electron downloads IPC */}
      <button
        onClick={onExport}
        className="flex items-center justify-center gap-2 rounded
                   bg-green-500 px-4 py-2 text-sm font-medium text-white
                   hover:bg-green-600 transition-all"
      >
        <Download size={18} />
        <span>تصدير PNG</span>
      </button>
    </div>
  );
};
