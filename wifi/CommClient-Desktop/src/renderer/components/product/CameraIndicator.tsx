/**
 * CameraIndicator.tsx — Recording-style indicator when camera is active.
 *
 * Shows a persistent red dot + "Camera On" label in the corner of the
 * screen whenever the user's camera is active during a call. This helps
 * children and non-technical users maintain awareness that they are visible.
 *
 * Features:
 *   - Small, non-intrusive red dot with label
 *   - Pulsing animation (like a recording indicator)
 *   - Fixed position (bottom-right corner, above call controls)
 *   - Only visible during active calls with camera on
 *   - Clickable to toggle camera off
 */

import React from 'react';
import { Camera } from 'lucide-react';
import { t } from '@/i18n';
import { useCallStore } from '@/stores/call.store.v2';
import { childSafetyGuard } from '@/services/product';

const CameraIndicator: React.FC = () => {
  const callStatus = useCallStore((s) => s.status);
  const isVideoOff = useCallStore((s) => s.isVideoOff);
  const toggleVideo = useCallStore((s) => s.toggleVideo);

  const isActive = callStatus === 'active' && !isVideoOff;

  if (!isActive) return null;
  if (!childSafetyGuard.shouldShowCameraIndicator()) return null;

  return (
    <button
      onClick={() => toggleVideo?.()}
      className="fixed bottom-24 end-4 z-[55] flex items-center gap-2 px-3 py-1.5 bg-red-600/90 hover:bg-red-700 text-white rounded-full shadow-lg transition-colors"
      title={t('product.camera_on_hint')}
    >
      <div className="w-2 h-2 rounded-full bg-white animate-pulse" />
      <Camera size={14} />
      <span className="text-xs font-medium">{t('product.camera_on')}</span>
    </button>
  );
};

export default CameraIndicator;
