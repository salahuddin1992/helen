/**
 * Product UI Components — Polished, child-friendly UI layer.
 *
 * These components sit on top of existing UI to add:
 *   - Friendly connection status (replaces technical ConnectionTracker)
 *   - Simplified navigation (replaces complex Sidebar/ModeAwareSidebar)
 *   - Screen share safety banner (persistent awareness)
 *   - Camera active indicator (recording-style dot)
 */

export { default as FriendlyConnectionBanner } from './FriendlyConnectionBanner';
export { default as ProductNav } from './ProductNav';
export { default as ScreenShareSafetyBanner } from './ScreenShareSafetyBanner';
export { default as CameraIndicator } from './CameraIndicator';
