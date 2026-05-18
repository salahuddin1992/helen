import React from 'react';

type ConnectionQuality = 'excellent' | 'good' | 'fair' | 'poor' | 'unknown';

interface ConnectionQualityIndicatorProps {
  quality: ConnectionQuality;
  latencyMs?: number;
}

const qualityConfig: Record<ConnectionQuality, { bars: number; color: string; label: string }> = {
  excellent: { bars: 4, color: '#22c55e', label: 'Excellent' },
  good:      { bars: 3, color: '#22c55e', label: 'Good' },
  fair:      { bars: 2, color: '#eab308', label: 'Fair' },
  poor:      { bars: 1, color: '#ef4444', label: 'Poor' },
  unknown:   { bars: 0, color: '#6b7280', label: 'Unknown' },
};

const ConnectionQualityIndicator: React.FC<ConnectionQualityIndicatorProps> = ({
  quality,
  latencyMs,
}) => {
  const { bars, color, label } = qualityConfig[quality];
  const totalBars = 4;
  const barHeights = [6, 10, 14, 18];
  const barWidth = 3;
  const gap = 1.5;
  const svgWidth = totalBars * barWidth + (totalBars - 1) * gap;
  const svgHeight = 20;

  const tooltipText = latencyMs != null
    ? `${label} (${latencyMs}ms)`
    : label;

  return (
    <div
      className="inline-flex items-center relative group cursor-default"
      title={tooltipText}
    >
      <svg
        width={svgWidth}
        height={svgHeight}
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        xmlns="http://www.w3.org/2000/svg"
      >
        {barHeights.map((h, i) => {
          const isActive = i < bars;
          const x = i * (barWidth + gap);
          const y = svgHeight - h;
          return (
            <rect
              key={i}
              x={x}
              y={y}
              width={barWidth}
              height={h}
              rx={1}
              fill={isActive ? color : '#4b5563'}
              opacity={isActive ? 1 : 0.3}
            />
          );
        })}
      </svg>
      {/* Tooltip */}
      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 px-2 py-1 bg-black/80 text-white text-xs rounded whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
        {tooltipText}
      </div>
    </div>
  );
};

export default ConnectionQualityIndicator;
