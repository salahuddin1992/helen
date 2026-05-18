/**
 * MediaStats — Donut chart of media types, total size, recent uploads timeline
 */
import React from 'react';

interface MediaStats {
  totalSize: number;
  imageCount: number;
  videoCount: number;
  audioCount: number;
  documentCount: number;
  recentUploads: Array<{
    date: string;
    count: number;
  }>;
}

interface MediaStatsProps {
  stats: MediaStats;
}

export const MediaStats: React.FC<MediaStatsProps> = ({ stats }) => {
  const totalCount =
    stats.imageCount +
    stats.videoCount +
    stats.audioCount +
    stats.documentCount;

  const imagePercent = (stats.imageCount / totalCount) * 100 || 0;
  const videoPercent = (stats.videoCount / totalCount) * 100 || 0;
  const audioPercent = (stats.audioCount / totalCount) * 100 || 0;
  const documentPercent = (stats.documentCount / totalCount) * 100 || 0;

  const formatSize = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
  };

  const donutSegments = [
    {
      type: 'Images',
      count: stats.imageCount,
      percent: imagePercent,
      color: '#3b82f6',
      emoji: '🖼️',
    },
    {
      type: 'Videos',
      count: stats.videoCount,
      percent: videoPercent,
      color: '#ef4444',
      emoji: '🎥',
    },
    {
      type: 'Audio',
      count: stats.audioCount,
      percent: audioPercent,
      color: '#10b981',
      emoji: '🔊',
    },
    {
      type: 'Documents',
      count: stats.documentCount,
      percent: documentPercent,
      color: '#f59e0b',
      emoji: '📄',
    },
  ];

  const maxUploads = Math.max(...stats.recentUploads.map((u) => u.count), 1);

  return (
    <div className="space-y-6">
      {/* Total Size and Count */}
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-lg bg-gradient-to-br from-blue-50 to-blue-100 p-4">
          <p className="text-sm text-gray-600">Total Size</p>
          <p className="text-2xl font-bold text-blue-600">
            {formatSize(stats.totalSize)}
          </p>
        </div>
        <div className="rounded-lg bg-gradient-to-br from-purple-50 to-purple-100 p-4">
          <p className="text-sm text-gray-600">Total Files</p>
          <p className="text-2xl font-bold text-purple-600">{totalCount}</p>
        </div>
      </div>

      {/* Donut Chart */}
      <div className="flex flex-col items-center gap-4">
        <svg className="h-48 w-48" viewBox="0 0 100 100">
          {donutSegments
            .filter((s) => s.count > 0)
            .reduce((acc, segment, index) => {
              let cumulativePercent = 0;
              for (let i = 0; i < index; i++) {
                cumulativePercent += donutSegments[i].percent;
              }

              const startAngle = (cumulativePercent * 360) / 100;
              const endAngle = startAngle + (segment.percent * 360) / 100;

              const startRad = (startAngle * Math.PI) / 180;
              const endRad = (endAngle * Math.PI) / 180;

              const x1 = 50 + 30 * Math.cos(startRad);
              const y1 = 50 + 30 * Math.sin(startRad);
              const x2 = 50 + 30 * Math.cos(endRad);
              const y2 = 50 + 30 * Math.sin(endRad);

              const largeArc = segment.percent > 50 ? 1 : 0;

              const path = `M 50 50 L ${x1} ${y1} A 30 30 0 ${largeArc} 1 ${x2} ${y2} Z`;

              return [
                ...acc,
                <path
                  key={index}
                  d={path}
                  fill={segment.color}
                  stroke="white"
                  strokeWidth="1"
                />,
              ];
            }, [] as React.ReactNode[])}

          {/* Center circle for donut effect */}
          <circle cx="50" cy="50" r="15" fill="white" />
        </svg>

        {/* Legend */}
        <div className="grid w-full grid-cols-2 gap-2">
          {donutSegments
            .filter((s) => s.count > 0)
            .map((segment) => (
              <div key={segment.type} className="flex items-center gap-2">
                <div
                  className="h-3 w-3 rounded-full"
                  style={{ backgroundColor: segment.color }}
                />
                <div className="text-sm">
                  <p className="font-medium text-gray-900">{segment.type}</p>
                  <p className="text-gray-500">
                    {segment.count} ({segment.percent.toFixed(1)}%)
                  </p>
                </div>
              </div>
            ))}
        </div>
      </div>

      {/* Recent Uploads Timeline */}
      <div className="rounded-lg bg-gray-50 p-4">
        <h4 className="mb-3 font-semibold text-gray-900">Recent Uploads</h4>
        <div className="space-y-2">
          {stats.recentUploads.slice(0, 7).map((item, index) => (
            <div key={index} className="flex items-center gap-3">
              <div className="text-sm text-gray-600 w-20">{item.date}</div>
              <div className="flex-1 h-6 bg-gray-200 rounded overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-blue-400 to-blue-600"
                  style={{
                    width: `${(item.count / maxUploads) * 100}%`,
                  }}
                />
              </div>
              <div className="text-sm font-medium text-gray-900 w-8 text-right">
                {item.count}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
