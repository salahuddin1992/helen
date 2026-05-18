/**
 * Messaging Engine — barrel export.
 */

export { MessageQueue } from './MessageQueue';
export type { OutboundMessage, QueueCallbacks, MessageType } from './MessageQueue';

export { DeliveryTracker } from './DeliveryTracker';
export type { DeliveryStatus, MessageDeliveryState, DeliveryCallbacks } from './DeliveryTracker';

export { SyncManager } from './SyncManager';
export type {
  SyncResult,
  SyncedMessage,
  ChannelUnreadInfo,
  ChannelSummary,
  SyncCallbacks,
} from './SyncManager';

export { MessagingEngine } from './MessagingEngine';
export type {
  IncomingMessage,
  TypingEvent,
  MessageEditedEvent,
  MessageDeletedEvent,
  ReactionUpdateEvent,
  MessagingEngineCallbacks,
} from './MessagingEngine';
