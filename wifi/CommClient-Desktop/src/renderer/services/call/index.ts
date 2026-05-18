/**
 * Call Engine — barrel export.
 *
 * Usage:
 *   import { CallEngine, CallStateMachine, ... } from '@/services/call';
 */

export { CallStateMachine } from './CallStateMachine';
export type { CallStatus, CallEvent, StateChangeCallback } from './CallStateMachine';

export { MediaDeviceManager } from './MediaDeviceManager';
export type { DeviceInfo, DeviceSelection, MediaConstraintOptions } from './MediaDeviceManager';

export { PeerConnection } from './PeerConnection';
export type { PeerConnectionConfig, SignalMessage } from './PeerConnection';

export { GroupCallManager } from './GroupCallManager';
export type { GroupParticipant, GroupCallConfig } from './GroupCallManager';

export { QualityController, QUALITY_PRESETS } from './QualityController';
export type {
  QualityPreset,
  QualityLevel,
  PeerQualitySnapshot,
  QualityChangeEvent,
} from './QualityController';

export { CallEngine } from './CallEngine';
export type {
  CallType,
  CallRouting,
  CallEngineState,
  CallEngineCallbacks,
} from './CallEngine';

export { ScreenShareManager, SCREEN_QUALITY_PRESETS } from './ScreenShareManager';
export type {
  ScreenShareMode,
  ContentType,
  ContentHint,
  ScreenShareSource,
  ScreenShareQualityPreset,
  ScreenShareStatus,
  ScreenShareState,
  ScreenShareCallbacks,
} from './ScreenShareManager';

export { PresenterManager } from './PresenterManager';
export type {
  PresenterInfo,
  PresenterRequestStatus,
  PresenterState,
  PresenterCallbacks,
} from './PresenterManager';

export { ScreenShareEngine } from './ScreenShareEngine';
export type {
  ScreenShareEngineState,
  ScreenShareEngineCallbacks,
  ShareOptions,
} from './ScreenShareEngine';

export { TopologyCoordinator, NoopSFUAdapter } from './TopologyCoordinator';
export type {
  CallRoutingMode,
  TopologySwitchEvent,
  QualitySample,
  TopologyCoordinatorConfig,
  ISFUAdapter,
  SFUInfo,
} from './TopologyCoordinator';

export { MediasoupSFUAdapter } from './MediasoupSFUAdapter';
