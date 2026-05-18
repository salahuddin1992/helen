/**
 * WhiteboardCanvas — HTML5 Canvas-based drawing surface
 * Tools: pen, eraser, line, rectangle, circle, text
 * Color picker, stroke width slider, touch support
 */
import React, { useRef, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';

type DrawingTool = 'pen' | 'eraser' | 'line' | 'rectangle' | 'circle' | 'text';

interface Stroke {
  id: string;
  tool: DrawingTool;
  color: string;
  width: number;
  points: Array<{ x: number; y: number }>;
  // For shapes
  startX?: number;
  startY?: number;
  endX?: number;
  endY?: number;
  // For text
  text?: string;
  fontSize?: number;
  x?: number;
  y?: number;
}

interface WhiteboardCanvasProps {
  strokes?: Stroke[];
  selectedTool?: DrawingTool;
  selectedColor?: string;
  selectedWidth?: number;
  onStrokeAdded?: (stroke: Stroke) => void;
  onClear?: () => void;
  onUndo?: () => void;
}

export const WhiteboardCanvas: React.FC<WhiteboardCanvasProps> = ({
  strokes = [],
  selectedTool = 'pen',
  selectedColor = '#000000',
  selectedWidth = 2,
  onStrokeAdded,
  onClear,
  onUndo,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [isDrawing, setIsDrawing] = useState(false);
  const [currentStroke, setCurrentStroke] = useState<Stroke | null>(null);
  const startPosRef = useRef({ x: 0, y: 0 });

  // Redraw canvas when strokes change
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Clear canvas
    ctx.fillStyle = 'white';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Draw all strokes
    for (const stroke of strokes) {
      if (stroke.tool === 'pen' || stroke.tool === 'eraser') {
        drawPenStroke(ctx, stroke);
      } else if (stroke.tool === 'line') {
        drawLine(ctx, stroke);
      } else if (stroke.tool === 'rectangle') {
        drawRectangle(ctx, stroke);
      } else if (stroke.tool === 'circle') {
        drawCircle(ctx, stroke);
      } else if (stroke.tool === 'text') {
        drawText(ctx, stroke);
      }
    }

    // Draw current stroke being drawn
    if (currentStroke && currentStroke.tool === 'pen') {
      drawPenStroke(ctx, currentStroke);
    } else if (currentStroke && currentStroke.tool === 'eraser') {
      drawEraser(ctx, currentStroke);
    } else if (currentStroke && currentStroke.tool === 'line') {
      drawLinePreview(ctx, currentStroke);
    } else if (currentStroke && currentStroke.tool === 'rectangle') {
      drawRectanglePreview(ctx, currentStroke);
    } else if (currentStroke && currentStroke.tool === 'circle') {
      drawCirclePreview(ctx, currentStroke);
    }
  }, [strokes, currentStroke]);

  const drawPenStroke = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    if (stroke.points.length === 0) return;

    ctx.beginPath();
    ctx.moveTo(stroke.points[0].x, stroke.points[0].y);

    for (let i = 1; i < stroke.points.length; i++) {
      ctx.lineTo(stroke.points[i].x, stroke.points[i].y);
    }
    ctx.stroke();
  };

  const drawEraser = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = stroke.width * 3;
    ctx.lineCap = 'round';

    if (stroke.points.length === 0) return;

    ctx.beginPath();
    ctx.moveTo(stroke.points[0].x, stroke.points[0].y);

    for (let i = 1; i < stroke.points.length; i++) {
      ctx.lineTo(stroke.points[i].x, stroke.points[i].y);
    }
    ctx.stroke();
  };

  const drawLine = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY || !stroke.endX || !stroke.endY) return;

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.beginPath();
    ctx.moveTo(stroke.startX, stroke.startY);
    ctx.lineTo(stroke.endX, stroke.endY);
    ctx.stroke();
  };

  const drawLinePreview = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY) return;

    const endX = stroke.endX || startPosRef.current.x;
    const endY = stroke.endY || startPosRef.current.y;

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(stroke.startX, stroke.startY);
    ctx.lineTo(endX, endY);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  const drawRectangle = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY || !stroke.endX || !stroke.endY) return;

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    const w = stroke.endX - stroke.startX;
    const h = stroke.endY - stroke.startY;
    ctx.strokeRect(stroke.startX, stroke.startY, w, h);
  };

  const drawRectanglePreview = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY) return;

    const endX = stroke.endX || startPosRef.current.x;
    const endY = stroke.endY || startPosRef.current.y;

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.setLineDash([5, 5]);
    const w = endX - stroke.startX;
    const h = endY - stroke.startY;
    ctx.strokeRect(stroke.startX, stroke.startY, w, h);
    ctx.setLineDash([]);
  };

  const drawCircle = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY || !stroke.endX || !stroke.endY) return;

    const radius = Math.sqrt(
      Math.pow(stroke.endX - stroke.startX, 2) +
      Math.pow(stroke.endY - stroke.startY, 2)
    );

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.beginPath();
    ctx.arc(stroke.startX, stroke.startY, radius, 0, 2 * Math.PI);
    ctx.stroke();
  };

  const drawCirclePreview = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.startX || !stroke.startY) return;

    const radius = Math.sqrt(
      Math.pow((stroke.endX || startPosRef.current.x) - stroke.startX, 2) +
      Math.pow((stroke.endY || startPosRef.current.y) - stroke.startY, 2)
    );

    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.arc(stroke.startX, stroke.startY, radius, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.setLineDash([]);
  };

  const drawText = (ctx: CanvasRenderingContext2D, stroke: Stroke) => {
    if (!stroke.text || !stroke.x || !stroke.y) return;

    ctx.fillStyle = stroke.color;
    ctx.font = `${stroke.fontSize || 16}px Arial`;
    ctx.fillText(stroke.text, stroke.x, stroke.y);
  };

  const getCanvasPos = (e: React.MouseEvent | React.TouchEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };

    const rect = canvas.getBoundingClientRect();
    const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX;
    const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY;

    return {
      x: clientX - rect.left,
      y: clientY - rect.top,
    };
  };

  const handleMouseDown = (e: React.MouseEvent | React.TouchEvent) => {
    const pos = getCanvasPos(e);
    startPosRef.current = pos;
    setIsDrawing(true);

    const id = `stroke-${Date.now()}`;

    if (selectedTool === 'pen' || selectedTool === 'eraser') {
      setCurrentStroke({
        id,
        tool: selectedTool,
        color: selectedColor,
        width: selectedWidth,
        points: [pos],
      });
    } else if (selectedTool === 'line' || selectedTool === 'rectangle' || selectedTool === 'circle') {
      setCurrentStroke({
        id,
        tool: selectedTool,
        color: selectedColor,
        width: selectedWidth,
        points: [],
        startX: pos.x,
        startY: pos.y,
      });
    }
  };

  const handleMouseMove = (e: React.MouseEvent | React.TouchEvent) => {
    if (!isDrawing || !currentStroke) return;

    const pos = getCanvasPos(e);

    if (currentStroke.tool === 'pen' || currentStroke.tool === 'eraser') {
      setCurrentStroke((prev) =>
        prev ? { ...prev, points: [...prev.points, pos] } : null
      );
    } else {
      setCurrentStroke((prev) =>
        prev
          ? { ...prev, endX: pos.x, endY: pos.y }
          : null
      );
    }
  };

  const handleMouseUp = () => {
    setIsDrawing(false);

    if (currentStroke && currentStroke.points.length > 0) {
      onStrokeAdded?.(currentStroke);
    }

    setCurrentStroke(null);
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <button
          onClick={onUndo}
          className="rounded bg-gray-200 px-3 py-2 text-sm hover:bg-gray-300"
        >
          Undo
        </button>
        <button
          onClick={onClear}
          className="flex items-center gap-1 rounded bg-red-500 px-3 py-2 text-sm text-white hover:bg-red-600"
        >
          <RefreshCw size={16} />
          Clear
        </button>
      </div>

      <canvas
        ref={canvasRef}
        width={800}
        height={600}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onTouchStart={handleMouseDown}
        onTouchMove={handleMouseMove}
        onTouchEnd={handleMouseUp}
        className="cursor-crosshair border border-gray-300 rounded bg-white"
      />
    </div>
  );
};
