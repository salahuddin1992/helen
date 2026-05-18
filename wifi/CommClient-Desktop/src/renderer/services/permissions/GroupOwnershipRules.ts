/**
 * GroupOwnershipRules.ts — Phase 15: Ownership Transfer, Succession & Group Lifecycle
 *
 * Defines the rules and state machine for group ownership — the most
 * sensitive permission in the system. Ownership cannot be assigned via
 * the normal role change flow; it requires explicit transfer.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                     Ownership Transfer Flow                          │
 * │                                                                      │
 * │  Owner ──[transfer_ownership]──► Confirmation Dialog                 │
 * │                                       │                              │
 * │                               ┌───────┼───────┐                     │
 * │                               │ Confirm│Cancel │                     │
 * │                               ▼       ▼       │                     │
 * │                          Server RPC  (abort)   │                     │
 * │                               │                │                     │
 * │                        ┌──────┴──────┐         │                     │
 * │                        │ Atomic swap │         │                     │
 * │                        │ old→admin   │         │                     │
 * │                        │ new→owner   │         │                     │
 * │                        └──────┬──────┘         │                     │
 * │                               │                │                     │
 * │                        Socket broadcast         │                     │
 * │                        'channel:owner_changed'  │                     │
 * │                               │                │                     │
 * │                  All members update local cache │                     │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                     Automatic Succession                             │
 * │                                                                      │
 * │  Owner leaves / goes offline for 30 days                             │
 * │       │                                                              │
 * │       ▼                                                              │
 * │  Succession chain:                                                   │
 * │    1. Longest-serving admin                                          │
 * │    2. Longest-serving moderator                                      │
 * │    3. Longest-serving member                                         │
 * │    4. (if no members) → group becomes orphaned → auto-delete 7d     │
 * │                                                                      │
 * │  Succession is SERVER-TRIGGERED, not client-initiated.               │
 * │  Client receives 'channel:owner_changed' with reason='succession'.   │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import {
  type ChannelRole,
  CHANNEL_ROLE_POWER,
  CHANNEL_ROLE_META,
} from './RoleModel';

// ── Types ───────────────────────────────────────────────────────

/**
 * Reasons why ownership can change.
 */
export type OwnershipChangeReason =
  | 'manual_transfer'     // Owner explicitly transferred
  | 'succession_leave'    // Owner left the group
  | 'succession_inactive' // Owner inactive for threshold period
  | 'succession_deleted'  // Owner account deleted
  | 'admin_override';     // Server admin force-transferred (emergency)

/**
 * Ownership transfer request (client → server).
 */
export interface OwnershipTransferRequest {
  channelId: string;
  currentOwnerId: string;
  newOwnerId: string;
  /** Role to assign to the old owner after transfer (default: 'admin') */
  oldOwnerNewRole: 'admin' | 'moderator' | 'member';
}

/**
 * Ownership change event (server → all clients via socket).
 */
export interface OwnershipChangeEvent {
  channelId: string;
  previousOwnerId: string;
  newOwnerId: string;
  reason: OwnershipChangeReason;
  previousOwnerNewRole: ChannelRole;
  timestamp: string;
}

/**
 * Succession candidate — computed by server when owner departs.
 */
export interface SuccessionCandidate {
  userId: string;
  username: string;
  displayName: string;
  currentRole: ChannelRole;
  joinedAt: string;
  /** Priority score: higher = more eligible */
  priority: number;
}

// ── Configuration ───────────────────────────────────────────────

export interface OwnershipConfig {
  /** Days of inactivity before succession triggers — default 30 */
  inactivityThresholdDays: number;
  /** Days before orphaned group is auto-deleted — default 7 */
  orphanedGroupDeleteDays: number;
  /** Default role for old owner after transfer — default 'admin' */
  defaultOldOwnerRole: 'admin' | 'moderator' | 'member';
  /** Require confirmation dialog for transfer? — default true */
  requireTransferConfirmation: boolean;
  /** Allow owner to leave without transferring? (triggers succession) — default true */
  allowLeaveWithoutTransfer: boolean;
  /** Show succession warning when owner tries to leave? — default true */
  showLeaveSuccessionWarning: boolean;
}

export const DEFAULT_OWNERSHIP_CONFIG: OwnershipConfig = {
  inactivityThresholdDays: 30,
  orphanedGroupDeleteDays: 7,
  defaultOldOwnerRole: 'admin',
  requireTransferConfirmation: true,
  allowLeaveWithoutTransfer: true,
  showLeaveSuccessionWarning: true,
};

// ── Validation ──────────────────────────────────────────────────

export interface TransferValidation {
  allowed: boolean;
  reason?: string;
  reasonKey?: string;
}

/**
 * Validate an ownership transfer request (client-side pre-check).
 *
 * Rules:
 * 1. Only the current owner can initiate transfer
 * 2. Target must be a current member of the channel
 * 3. Cannot transfer to self
 * 4. Target must not already be owner (no-op check)
 * 5. Channel must have at least 2 members
 */
export function validateTransfer(
  request: OwnershipTransferRequest,
  memberIds: string[],
  currentOwnerRole: ChannelRole,
): TransferValidation {
  // Rule 1: Only owner can transfer
  if (currentOwnerRole !== 'owner') {
    return {
      allowed: false,
      reason: 'Only the owner can transfer ownership',
      reasonKey: 'permissions.error.not_owner',
    };
  }

  // Rule 2: Target must be a member
  if (!memberIds.includes(request.newOwnerId)) {
    return {
      allowed: false,
      reason: 'Target user is not a member of this group',
      reasonKey: 'permissions.error.target_not_member',
    };
  }

  // Rule 3: Cannot transfer to self
  if (request.currentOwnerId === request.newOwnerId) {
    return {
      allowed: false,
      reason: 'Cannot transfer ownership to yourself',
      reasonKey: 'permissions.error.self_transfer',
    };
  }

  // Rule 4: Channel must have ≥2 members
  if (memberIds.length < 2) {
    return {
      allowed: false,
      reason: 'Cannot transfer ownership in a single-member group',
      reasonKey: 'permissions.error.group_too_small',
    };
  }

  return { allowed: true };
}

// ── Succession Logic ────────────────────────────────────────────

/**
 * Compute succession priority for a member.
 * Higher score = more eligible to become owner.
 *
 * Priority formula:
 *   base_role_score + tenure_bonus
 *
 * Role scores:
 *   admin: 300, moderator: 200, member: 100
 *
 * Tenure bonus:
 *   0.001 * days_since_join (earlier join = higher bonus)
 *
 * This ensures: admin > moderator > member, with tie-breaking by tenure.
 */
export function computeSuccessionPriority(
  role: ChannelRole,
  joinedAt: string,
): number {
  const roleScores: Record<ChannelRole, number> = {
    owner: 0,     // Owner should never be in succession list
    admin: 300,
    moderator: 200,
    member: 100,
  };

  const baseScore = roleScores[role];
  const joinDate = new Date(joinedAt).getTime();
  const now = Date.now();
  const daysSinceJoin = (now - joinDate) / (1000 * 60 * 60 * 24);
  const tenureBonus = daysSinceJoin * 0.001;

  return baseScore + tenureBonus;
}

/**
 * Pick the successor from a list of members (excluding the departing owner).
 * Returns null if no eligible successors (group becomes orphaned).
 */
export function pickSuccessor(
  members: Array<{
    userId: string;
    username: string;
    displayName: string;
    role: ChannelRole;
    joinedAt: string;
  }>,
  departingOwnerId: string,
): SuccessionCandidate | null {
  const candidates = members
    .filter(m => m.userId !== departingOwnerId && m.role !== 'owner')
    .map(m => ({
      userId: m.userId,
      username: m.username,
      displayName: m.displayName,
      currentRole: m.role,
      joinedAt: m.joinedAt,
      priority: computeSuccessionPriority(m.role, m.joinedAt),
    }))
    .sort((a, b) => b.priority - a.priority);

  return candidates.length > 0 ? candidates[0] : null;
}

// ── Owner Leave Flow ────────────────────────────────────────────

export type OwnerLeaveDecision =
  | { action: 'transfer_first'; suggestedSuccessor: SuccessionCandidate }
  | { action: 'auto_succession'; successor: SuccessionCandidate }
  | { action: 'delete_group'; reason: 'no_members' }
  | { action: 'block_leave'; reason: string; reasonKey: string };

/**
 * Determine what happens when the owner tries to leave a group.
 *
 * Decision tree:
 * 1. If group has no other members → delete group
 * 2. If config requires transfer → suggest successor, block leave
 * 3. If config allows leave without transfer → auto-succession
 */
export function evaluateOwnerLeave(
  members: Array<{
    userId: string;
    username: string;
    displayName: string;
    role: ChannelRole;
    joinedAt: string;
  }>,
  ownerId: string,
  config: OwnershipConfig = DEFAULT_OWNERSHIP_CONFIG,
): OwnerLeaveDecision {
  const otherMembers = members.filter(m => m.userId !== ownerId);

  // No other members — delete group
  if (otherMembers.length === 0) {
    return { action: 'delete_group', reason: 'no_members' };
  }

  const successor = pickSuccessor(members, ownerId);

  if (!successor) {
    // Should not happen if otherMembers.length > 0, but be safe
    return { action: 'delete_group', reason: 'no_members' };
  }

  if (!config.allowLeaveWithoutTransfer) {
    return {
      action: 'transfer_first',
      suggestedSuccessor: successor,
    };
  }

  return {
    action: 'auto_succession',
    successor,
  };
}

// ── Group Lifecycle States ──────────────────────────────────────

export type GroupLifecycleState =
  | 'active'          // Normal operation
  | 'owner_departed'  // Owner left, succession in progress
  | 'orphaned'        // No eligible successor, awaiting cleanup
  | 'deleting'        // Deletion in progress
  | 'archived';       // Soft-deleted, recoverable within retention

/**
 * Group deletion rules.
 *
 * Who can delete:
 *   - Owner: can always delete (with confirmation)
 *   - Server: auto-deletes orphaned groups after configurable period
 *
 * What happens on delete:
 *   1. All active calls are ended
 *   2. All members receive 'channel:deleted' socket event
 *   3. Messages are soft-deleted (retained for 30 days for export)
 *   4. Files are queued for cleanup
 *   5. Channel is removed from all member channel lists
 */
export interface GroupDeletionPolicy {
  /** Who initiated the deletion */
  initiator: 'owner' | 'system_orphan_cleanup' | 'admin_override';
  /** Grace period before permanent delete (days) */
  softDeleteRetentionDays: number;
  /** Should files be purged immediately? */
  purgeFilesImmediately: boolean;
  /** Notify members? */
  notifyMembers: boolean;
}

export const DEFAULT_DELETION_POLICY: GroupDeletionPolicy = {
  initiator: 'owner',
  softDeleteRetentionDays: 30,
  purgeFilesImmediately: false,
  notifyMembers: true,
};

// ── Socket Event Contracts ──────────────────────────────────────

/**
 * Socket events the client should listen for regarding ownership.
 * These are CONTRACTS — the backend must emit these events.
 */
export const OWNERSHIP_SOCKET_EVENTS = {
  /** Ownership has changed (manual or succession) */
  OWNER_CHANGED: 'channel:owner_changed' as const,
  /** Owner is about to be succeeded (pre-notification) */
  SUCCESSION_PENDING: 'channel:succession_pending' as const,
  /** Group is now orphaned (no eligible successor) */
  GROUP_ORPHANED: 'channel:group_orphaned' as const,
  /** Group is scheduled for deletion */
  GROUP_DELETING: 'channel:group_deleting' as const,
  /** Group has been deleted */
  GROUP_DELETED: 'channel:deleted' as const,
} as const;

/**
 * Payloads for ownership-related socket events.
 */
export interface OwnershipSocketPayloads {
  [OWNERSHIP_SOCKET_EVENTS.OWNER_CHANGED]: OwnershipChangeEvent;
  [OWNERSHIP_SOCKET_EVENTS.SUCCESSION_PENDING]: {
    channelId: string;
    departingOwnerId: string;
    suggestedSuccessorId: string;
    reason: OwnershipChangeReason;
  };
  [OWNERSHIP_SOCKET_EVENTS.GROUP_ORPHANED]: {
    channelId: string;
    deleteScheduledAt: string;
  };
  [OWNERSHIP_SOCKET_EVENTS.GROUP_DELETING]: {
    channelId: string;
    deletionPolicy: GroupDeletionPolicy;
  };
  [OWNERSHIP_SOCKET_EVENTS.GROUP_DELETED]: {
    channelId: string;
    reason: string;
  };
}
