/**
 * PermissionGuard.ts — Phase 15: UI Permission Gating & Backend Enforcement Contracts
 *
 * This module provides:
 *
 *   1. **UI Permission Guard** — Client-side check that determines whether
 *      a UI element (button, menu item, action) should be visible, enabled,
 *      or hidden based on the current user's role and context.
 *
 *   2. **Action Pre-Check** — Before sending an API/socket request, validates
 *      the action locally to provide instant feedback (avoids round-trip
 *      just to get a 403).
 *
 *   3. **Backend Enforcement Contract** — Type definitions and REST/socket
 *      endpoint specifications that the backend MUST implement. These are
 *      the server-side gatekeepers — the client guard is UX optimization,
 *      the server guard is SECURITY.
 *
 *   4. **Simple/Advanced Mode Integration** — Actions marked as
 *      visibleInSimpleMode=false are hidden from Simple Mode users
 *      entirely. Advanced Mode reveals the full permission surface.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                    Dual Guard Architecture                            │
 * │                                                                      │
 * │  ┌─────────────────────────┐    ┌─────────────────────────────────┐ │
 * │  │   Client (PermGuard)    │    │    Server (Enforcement)          │ │
 * │  │                         │    │                                  │ │
 * │  │  UI Component           │    │  API Endpoint / Socket Handler   │ │
 * │  │     │                   │    │     │                            │ │
 * │  │     ▼                   │    │     ▼                            │ │
 * │  │  canPerform(action)?    │    │  authorize(userId, action)?      │ │
 * │  │     │                   │    │     │                            │ │
 * │  │  ┌──┴──┐               │    │  ┌──┴──┐                        │ │
 * │  │  │ Yes │ → Show/Enable │    │  │ Yes │ → Execute              │ │
 * │  │  │ No  │ → Hide/Gray   │    │  │ No  │ → 403 + error code     │ │
 * │  │  └─────┘               │    │  └─────┘                        │ │
 * │  │                         │    │                                  │ │
 * │  │  Purpose: UX            │    │  Purpose: SECURITY              │ │
 * │  │  (optimistic gating)    │    │  (authoritative enforcement)    │ │
 * │  └─────────────────────────┘    └─────────────────────────────────┘ │
 * │                                                                      │
 * │  CRITICAL: Client guard is NEVER trusted for security.               │
 * │  It exists only to prevent showing actions the user cannot perform.  │
 * │  The server MUST independently verify every permission.              │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import {
  type ChannelRole,
  type CallRole,
  type PermissionContext,
  CHANNEL_ROLE_POWER,
  hasHigherPower,
  getPermissionContext,
} from './RoleModel';

import {
  type PermissionAction,
  type PermissionDomain,
  PERMISSION_MATRIX,
  checkPermission,
} from './PermissionMatrix';

// ── UI Guard Result ────────────────────────────────────────────

export type UIVisibility = 'visible' | 'disabled' | 'hidden';

export interface UIGuardResult {
  /** Whether the UI element should be shown, grayed out, or hidden */
  visibility: UIVisibility;
  /** If disabled or hidden, the reason (for tooltip) */
  reason?: string;
  /** i18n key for the reason */
  reasonKey?: string;
  /** Is this a Simple Mode restriction? (not a permission issue) */
  isSimpleModeRestriction: boolean;
}

// ── User Context ───────────────────────────────────────────────

/**
 * The context needed to evaluate permissions for the current user.
 * This should be assembled from stores (auth, chat, call, app-mode).
 */
export interface UserPermissionContext {
  /** Current user's ID */
  userId: string;
  /** Current channel type */
  channelType: 'dm' | 'group';
  /** User's role in the current channel (null for DM) */
  channelRole: ChannelRole | null;
  /** User's role in the current call (null if not in call) */
  callRole: CallRole | null;
  /** Whether the user is in a call right now */
  inCall: boolean;
  /** App mode — simple or advanced */
  appMode: 'simple' | 'advanced';
  /** Channel ID (for caching) */
  channelId: string;
}

// ── UI Permission Guard ────────────────────────────────────────

/**
 * Check whether a UI element for an action should be visible/enabled/hidden.
 *
 * Decision cascade:
 * 1. If action is not visibleInSimpleMode and mode is 'simple' → HIDDEN
 * 2. If context is DM → check allowedInDM flag
 * 3. If user lacks minimum role → HIDDEN (not disabled, to keep UI clean)
 * 4. If action requires higher power but no target specified → VISIBLE (show button, check on click)
 * 5. Otherwise → VISIBLE
 */
export function guardUI(
  action: PermissionAction,
  context: UserPermissionContext,
  targetRole?: ChannelRole,
): UIGuardResult {
  const rule = PERMISSION_MATRIX[action];
  if (!rule) {
    return { visibility: 'hidden', reason: 'Unknown action', isSimpleModeRestriction: false };
  }

  // Step 1: Simple Mode filter
  if (!rule.visibleInSimpleMode && context.appMode === 'simple') {
    return {
      visibility: 'hidden',
      reason: 'Available in Advanced Mode',
      reasonKey: 'permissions.guard.advanced_only',
      isSimpleModeRestriction: true,
    };
  }

  // Step 2: DM context
  if (context.channelType === 'dm') {
    if (rule.allowedInDM) {
      return { visibility: 'visible', isSimpleModeRestriction: false };
    }
    return {
      visibility: 'hidden',
      reason: 'Not available in direct messages',
      reasonKey: 'permissions.guard.not_in_dm',
      isSimpleModeRestriction: false,
    };
  }

  // Step 3: Group context — check channel role
  const effectiveRole = context.channelRole || 'member';
  const permContext = getPermissionContext(context.channelType, context.inCall);

  const result = checkPermission({
    action,
    actorRole: effectiveRole,
    actorCallRole: context.callRole || undefined,
    context: permContext,
    targetRole,
  });

  if (!result.allowed) {
    // HIDDEN instead of disabled — keeps the UI uncluttered for lower roles
    return {
      visibility: 'hidden',
      reason: 'Insufficient permissions',
      reasonKey: result.deniedKey || 'permissions.guard.insufficient',
      isSimpleModeRestriction: false,
    };
  }

  return { visibility: 'visible', isSimpleModeRestriction: false };
}

/**
 * Batch-check multiple actions at once (efficient for rendering menus).
 */
export function guardUIBatch(
  actions: PermissionAction[],
  context: UserPermissionContext,
  targetRole?: ChannelRole,
): Map<PermissionAction, UIGuardResult> {
  const results = new Map<PermissionAction, UIGuardResult>();
  for (const action of actions) {
    results.set(action, guardUI(action, context, targetRole));
  }
  return results;
}

/**
 * Check if ANY action in a domain is visible for this user.
 * Useful for showing/hiding entire menu sections.
 */
export function isDomainVisible(
  domain: PermissionDomain,
  context: UserPermissionContext,
): boolean {
  const actions = Object.entries(PERMISSION_MATRIX)
    .filter(([_, rule]) => rule.domain === domain)
    .map(([action]) => action as PermissionAction);

  return actions.some(action => guardUI(action, context).visibility === 'visible');
}

// ── Action Pre-Check (before API call) ─────────────────────────

export interface PreCheckResult {
  allowed: boolean;
  /** If denied, show this message to the user */
  userMessage?: string;
  /** i18n key for user message */
  userMessageKey?: string;
  /** Error code for logging */
  errorCode?: string;
}

/**
 * Pre-check an action before making the API/socket call.
 * This is more thorough than guardUI — it validates all conditions.
 */
export function preCheckAction(
  action: PermissionAction,
  context: UserPermissionContext,
  targetUserId?: string,
  targetRole?: ChannelRole,
): PreCheckResult {
  const rule = PERMISSION_MATRIX[action];
  if (!rule) {
    return {
      allowed: false,
      userMessage: 'Unknown action',
      userMessageKey: 'permissions.error.unknown_action',
      errorCode: 'PERM_UNKNOWN_ACTION',
    };
  }

  // DM context
  if (context.channelType === 'dm') {
    if (!rule.allowedInDM) {
      return {
        allowed: false,
        userMessage: 'Not available in direct messages',
        userMessageKey: 'permissions.error.not_in_dm',
        errorCode: 'PERM_NOT_IN_DM',
      };
    }
    return { allowed: true };
  }

  // Group context
  const effectiveRole = context.channelRole || 'member';
  const permContext = getPermissionContext(context.channelType, context.inCall);

  // Minimum role check
  if (CHANNEL_ROLE_POWER[effectiveRole] < CHANNEL_ROLE_POWER[rule.minGroupRole]) {
    return {
      allowed: false,
      userMessage: `Requires ${rule.minGroupRole} role or higher`,
      userMessageKey: rule.deniedKey,
      errorCode: 'PERM_ROLE_TOO_LOW',
    };
  }

  // Higher power check for moderation actions
  if (rule.requiresHigherPower && targetRole) {
    if (!hasHigherPower(effectiveRole, targetRole)) {
      return {
        allowed: false,
        userMessage: 'Cannot perform this action on a user with equal or higher role',
        userMessageKey: 'permissions.error.insufficient_authority',
        errorCode: 'PERM_POWER_TOO_LOW',
      };
    }
  }

  // Self-action check for moderation (can't moderate yourself)
  if (rule.requiresHigherPower && targetUserId === context.userId) {
    return {
      allowed: false,
      userMessage: 'Cannot perform this action on yourself',
      userMessageKey: 'permissions.error.self_action',
      errorCode: 'PERM_SELF_ACTION',
    };
  }

  return { allowed: true };
}

// ══════════════════════════════════════════════════════════════════
//  BACKEND ENFORCEMENT CONTRACTS
// ══════════════════════════════════════════════════════════════════

/**
 * REST API endpoints that MUST enforce permissions.
 * Format: HTTP_METHOD path → required permission.
 *
 * The server must:
 *   1. Extract the acting user from the JWT token
 *   2. Look up their role in the target channel
 *   3. Evaluate the permission rule
 *   4. Return 403 with a structured error if denied
 */
export const API_PERMISSION_MAP: Record<string, {
  method: string;
  path: string;
  action: PermissionAction;
  description: string;
}> = {
  // Messaging
  send_message: {
    method: 'POST', path: '/api/channels/:channelId/messages',
    action: 'messaging.send', description: 'Send a message',
  },
  delete_message: {
    method: 'DELETE', path: '/api/channels/:channelId/messages/:messageId',
    action: 'messaging.delete_any', description: 'Delete any message (mod+)',
  },
  pin_message: {
    method: 'POST', path: '/api/channels/:channelId/messages/:messageId/pin',
    action: 'messaging.pin', description: 'Pin a message',
  },

  // Members
  invite_member: {
    method: 'POST', path: '/api/channels/:channelId/members',
    action: 'members.invite', description: 'Invite user to channel',
  },
  remove_member: {
    method: 'DELETE', path: '/api/channels/:channelId/members/:userId',
    action: 'members.remove', description: 'Remove member from channel',
  },

  // Roles
  change_role: {
    method: 'PATCH', path: '/api/channels/:channelId/members/:userId/role',
    action: 'roles.change', description: 'Change member role',
  },

  // Group Config
  update_channel: {
    method: 'PATCH', path: '/api/channels/:channelId',
    action: 'group_config.edit_name', description: 'Update channel name/desc/avatar',
  },
  delete_channel: {
    method: 'DELETE', path: '/api/channels/:channelId',
    action: 'group_config.delete_group', description: 'Delete the group',
  },
  transfer_ownership: {
    method: 'POST', path: '/api/channels/:channelId/transfer-ownership',
    action: 'group_config.transfer_owner', description: 'Transfer group ownership',
  },
};

/**
 * Socket events that MUST enforce permissions server-side.
 */
export const SOCKET_PERMISSION_MAP: Record<string, {
  event: string;
  action: PermissionAction;
  description: string;
}> = {
  // Call
  start_call: {
    event: 'call:start',
    action: 'calling.start_audio',
    description: 'Start a call in channel',
  },
  end_call_for_all: {
    event: 'call:end_all',
    action: 'calling.end_call',
    description: 'End call for all participants',
  },

  // Moderation
  mute_participant: {
    event: 'call:mute_user',
    action: 'moderation.mute_user',
    description: 'Force-mute a participant',
  },
  mute_all: {
    event: 'call:mute_all',
    action: 'call_floor.mute_all',
    description: 'Mute all participants',
  },

  // Screen Share
  force_stop_share: {
    event: 'presenter:force_stop',
    action: 'screenshare.force_stop',
    description: 'Force-stop screen share',
  },

  // Room Control
  lock_room: {
    event: 'channel:lock',
    action: 'room_control.lock',
    description: 'Lock channel',
  },
  unlock_room: {
    event: 'channel:unlock',
    action: 'room_control.unlock',
    description: 'Unlock channel',
  },

  // Floor Control
  grant_speak: {
    event: 'call:grant_speak',
    action: 'call_floor.grant_speak',
    description: 'Grant speak permission',
  },
  deny_speak: {
    event: 'call:deny_speak',
    action: 'call_floor.deny_speak',
    description: 'Deny speak request',
  },
};

/**
 * Structured error response the server should return on permission denial.
 */
export interface PermissionDeniedResponse {
  /** HTTP status code (always 403) */
  status: 403;
  /** Error type */
  error: 'permission_denied';
  /** Machine-readable error code */
  code: string;
  /** Human-readable message (English) */
  message: string;
  /** The action that was attempted */
  action: PermissionAction;
  /** The user's actual role */
  actualRole: ChannelRole;
  /** The minimum role required */
  requiredRole: ChannelRole;
  /** Additional context */
  detail?: string;
}

// ── Socket Events for Permission Updates ────────────────────────

/**
 * Socket events the client listens to for permission-related updates.
 */
export const PERMISSION_SOCKET_EVENTS = {
  /** A member's role was changed */
  ROLE_CHANGED: 'channel:role_changed' as const,
  /** A member was removed */
  MEMBER_REMOVED: 'channel:member_removed' as const,
  /** A new member joined */
  MEMBER_JOINED: 'channel:member_joined' as const,
  /** Room lock status changed */
  ROOM_LOCKED: 'channel:room_locked' as const,
  /** Slow mode toggled */
  SLOW_MODE_CHANGED: 'channel:slow_mode_changed' as const,
  /** A user was timed out */
  USER_TIMED_OUT: 'channel:user_timed_out' as const,
  /** A user's timeout expired */
  USER_TIMEOUT_EXPIRED: 'channel:timeout_expired' as const,
} as const;

export interface PermissionSocketPayloads {
  [PERMISSION_SOCKET_EVENTS.ROLE_CHANGED]: {
    channelId: string;
    userId: string;
    oldRole: ChannelRole;
    newRole: ChannelRole;
    changedBy: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.MEMBER_REMOVED]: {
    channelId: string;
    userId: string;
    removedBy: string;
    reason?: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.MEMBER_JOINED]: {
    channelId: string;
    userId: string;
    username: string;
    displayName: string;
    role: ChannelRole;
    invitedBy?: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.ROOM_LOCKED]: {
    channelId: string;
    locked: boolean;
    lockedBy: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.SLOW_MODE_CHANGED]: {
    channelId: string;
    enabled: boolean;
    intervalSeconds: number;
    changedBy: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.USER_TIMED_OUT]: {
    channelId: string;
    userId: string;
    timedOutBy: string;
    durationSeconds: number;
    reason?: string;
    timestamp: string;
  };
  [PERMISSION_SOCKET_EVENTS.USER_TIMEOUT_EXPIRED]: {
    channelId: string;
    userId: string;
    timestamp: string;
  };
}

// ── Audit Log Entry ─────────────────────────────────────────────

/**
 * Structure for permission-related audit log entries.
 * The server should persist these for the moderation audit trail.
 */
export interface PermissionAuditEntry {
  id: string;
  channelId: string;
  /** The user who performed the action */
  actorId: string;
  actorUsername: string;
  actorRole: ChannelRole;
  /** The action performed */
  action: PermissionAction;
  /** The target user (if applicable) */
  targetId?: string;
  targetUsername?: string;
  targetRole?: ChannelRole;
  /** Result of the action */
  result: 'allowed' | 'denied';
  /** Additional details */
  detail?: string;
  /** When it happened */
  timestamp: string;
}
