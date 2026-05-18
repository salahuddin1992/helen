/**
 * Product Module — Transforms CommClient from a technical project
 * into a polished, child-friendly Windows product.
 *
 * ┌────────────────────────────────────────────────────────────┐
 * │                    ProductShell                             │
 * │  Window chrome, tray, notifications, idle, presence, title │
 * ├────────────────────────────────────────────────────────────┤
 * │                    SmartDefaults                            │
 * │  Auto-detect: language, theme, devices, quality, server    │
 * ├────────────────────────────────────────────────────────────┤
 * │                 ConnectionResilience                        │
 * │  5 friendly states: connected/connecting/slow/offline/none │
 * │  Auto-retry, WiFi sleep detection, RTT probe               │
 * ├────────────────────────────────────────────────────────────┤
 * │                  ChildSafetyGuard                           │
 * │  Confirm calls, safe files, link disable, volume cap,      │
 * │  rate-limited profile changes, camera/screen banners        │
 * ├────────────────────────────────────────────────────────────┤
 * │                   OneClickActions                           │
 * │  quickCall, quickMessage, quickGroup, quickScreenShare,    │
 * │  quickAddContact, quickToggleMute, quickToggleVideo        │
 * └────────────────────────────────────────────────────────────┘
 *
 * Integration:
 *   import { smartDefaults, connectionResilience, childSafetyGuard,
 *            oneClickActions, productShell } from '@/services/product';
 *
 *   // At app startup:
 *   const defaults = await smartDefaults.detect();
 *   smartDefaults.startWatching();
 *   connectionResilience.start(serverUrl);
 *   productShell.start();
 *   oneClickActions.init({ ...deps });
 *
 *   // In Simple Mode:
 *   childSafetyGuard.applySimpleMode();
 *
 *   // In Advanced Mode:
 *   childSafetyGuard.applyAdvancedMode();
 */

export { smartDefaults } from './SmartDefaults';
export type { SmartDefaultsSnapshot } from './SmartDefaults';

export { connectionResilience } from './ConnectionResilience';
export type { FriendlyConnectionState, ConnectionStatus } from './ConnectionResilience';

export { childSafetyGuard } from './ChildSafetyGuard';
export type { SafetyConfig, SafetyCheckResult } from './ChildSafetyGuard';

export { oneClickActions } from './OneClickActions';
export type { ActionResult, OneClickDependencies } from './OneClickActions';

export { productShell } from './ProductShell';
export type { ProductShellConfig, CloseAction, PresenceState } from './ProductShell';
