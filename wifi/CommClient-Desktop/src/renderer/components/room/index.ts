/**
 * Room components — simplified group/call/participant UX.
 *
 * Usage in any parent component:
 *   import { SimpleCreateGroup, SimpleJoinGroup, QuickCallSheet, ... } from '@/components/room';
 */

export { default as SimpleCreateGroup } from './SimpleCreateGroup';
export { default as SimpleJoinGroup } from './SimpleJoinGroup';
export { default as QuickCallSheet } from './QuickCallSheet';
export { default as SimpleParticipantList } from './SimpleParticipantList';
export { default as RoomStateBar } from './RoomStateBar';
export { default as GroupActionHub } from './GroupActionHub';

// Re-export types for consumers
export type { Participant, ParticipantRole, ParticipantStatus } from './SimpleParticipantList';
export type { RoomState } from './RoomStateBar';
