/**
 * PermissionMatrix.ts — Phase 15: Complete Permission Matrix
 *
 * Defines every action in the system and which roles can perform it.
 * The matrix covers 9 permission domains × 4 channel roles.
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                     Permission Matrix Overview                        │
 * │                                                                      │
 * │  Domain        │ Owner  │ Admin  │ Moderator │ Member               │
 * │  ──────────────┼────────┼────────┼───────────┼──────────            │
 * │  Messaging     │  ████  │  ████  │   ████    │  ███░                │
 * │  Calling       │  ████  │  ████  │   ███░    │  ██░░                │
 * │  Screen Share  │  ████  │  ████  │   ███░    │  ██░░                │
 * │  Members       │  ████  │  ███░  │   █░░░    │  ░░░░                │
 * │  Roles         │  ████  │  ██░░  │   ░░░░    │  ░░░░                │
 * │  Room Control  │  ████  │  ███░  │   ██░░    │  ░░░░                │
 * │  Moderation    │  ████  │  ████  │   ████    │  ░░░░                │
 * │  Group Config  │  ████  │  ██░░  │   ░░░░    │  ░░░░                │
 * │  Call Floor    │  ████  │  ████  │   ███░    │  █░░░                │
 * │                                                                      │
 * │  ████ = full access   ███░ = most actions                            │
 * │  ██░░ = limited       █░░░ = minimal                                 │
 * │  ░░░░ = no access                                                    │
 * │                                                                      │
 * │  DM context: all messaging/calling/sharing actions are ALLOWED       │
 * │  for both parties — no permission checks needed.                     │
 * └──────────────────────────────────────────────────────────────────────┘
 */

import {
  type ChannelRole,
  type CallRole,
  type PermissionContext,
  CHANNEL_ROLE_POWER,
  CALL_ROLE_POWER,
} from './RoleModel';

// ── Permission Domains ─────────────────────────────────────────

export type PermissionDomain =
  | 'messaging'
  | 'calling'
  | 'screenshare'
  | 'members'
  | 'roles'
  | 'room_control'
  | 'moderation'
  | 'group_config'
  | 'call_floor';

// ── Permission Actions ─────────────────────────────────────────

/**
 * Every discrete action in the system. Named as domain.verb for clarity.
 */
export type PermissionAction =
  // ── Messaging ──────────────────────
  | 'messaging.send'              // Send text messages
  | 'messaging.send_files'        // Send file attachments
  | 'messaging.send_voice'        // Send voice messages
  | 'messaging.edit_own'          // Edit own messages
  | 'messaging.delete_own'        // Delete own messages
  | 'messaging.delete_any'        // Delete any message (moderation)
  | 'messaging.pin'               // Pin/unpin messages
  | 'messaging.react'             // Add reactions
  | 'messaging.reply'             // Reply to messages
  | 'messaging.mention_all'       // @all / @everyone mention

  // ── Calling ────────────────────────
  | 'calling.start_audio'         // Initiate audio call
  | 'calling.start_video'         // Initiate video call
  | 'calling.join'                // Join ongoing group call
  | 'calling.unmute_self'         // Unmute own microphone
  | 'calling.enable_video_self'   // Enable own camera
  | 'calling.end_call'            // End call for everyone

  // ── Screen Share ───────────────────
  | 'screenshare.request'         // Request to share screen
  | 'screenshare.share'           // Actually share (after grant or if no queue)
  | 'screenshare.force_stop'      // Force-stop someone else's share

  // ── Members ────────────────────────
  | 'members.invite'              // Invite new users to channel
  | 'members.remove'              // Remove a member from channel
  | 'members.view_list'           // View full member list
  | 'members.leave'               // Leave the channel (always allowed)

  // ── Roles ──────────────────────────
  | 'roles.view'                  // See role badges
  | 'roles.change'                // Change another user's role
  | 'roles.view_permissions'      // View permission details (Advanced Mode)

  // ── Room Control ───────────────────
  | 'room_control.lock'           // Lock room (prevent new joins)
  | 'room_control.unlock'         // Unlock room
  | 'room_control.set_slow_mode'  // Enable slow mode (rate limit messages)
  | 'room_control.clear_chat'     // Clear all chat history

  // ── Moderation ─────────────────────
  | 'moderation.mute_user'        // Mute a user in call
  | 'moderation.deafen_user'      // Deafen a user (they can't hear)
  | 'moderation.warn_user'        // Send a warning to a user
  | 'moderation.timeout_user'     // Temporarily restrict a user
  | 'moderation.view_audit_log'   // View moderation audit log (Advanced Mode)

  // ── Group Config ───────────────────
  | 'group_config.edit_name'      // Change group name
  | 'group_config.edit_desc'      // Change group description
  | 'group_config.edit_avatar'    // Change group avatar
  | 'group_config.delete_group'   // Delete the entire group
  | 'group_config.transfer_owner' // Transfer ownership

  // ── Call Floor Control ─────────────
  | 'call_floor.mute_all'         // Mute all participants
  | 'call_floor.request_speak'    // Raise hand / request to speak
  | 'call_floor.grant_speak'      // Grant speak permission to someone
  | 'call_floor.deny_speak'       // Deny speak request
  | 'call_floor.lower_all_hands'  // Lower all raised hands
  ;

// ── Permission Rule ────────────────────────────────────────────

export interface PermissionRule {
  /** The action being permitted */
  action: PermissionAction;
  /** Domain for grouping in UI */
  domain: PermissionDomain;
  /** Minimum channel role required in group context */
  minGroupRole: ChannelRole;
  /** Minimum call role required in call context (null = use group role) */
  minCallRole: CallRole | null;
  /** Is this action allowed in DM context? */
  allowedInDM: boolean;
  /** Requires target to have lower power? (for moderation actions) */
  requiresHigherPower: boolean;
  /** Is this visible in Simple Mode UI? */
  visibleInSimpleMode: boolean;
  /** i18n key for action label */
  labelKey: string;
  /** i18n key for denied message */
  deniedKey: string;
}

// ── The Matrix ─────────────────────────────────────────────────

export const PERMISSION_MATRIX: Record<PermissionAction, PermissionRule> = {

  // ══════════════════════════════════════════════════════════════
  //  MESSAGING
  // ══════════════════════════════════════════════════════════════

  'messaging.send': {
    action: 'messaging.send', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.send_message',
    deniedKey: 'permissions.denied.send_message',
  },
  'messaging.send_files': {
    action: 'messaging.send_files', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.send_files',
    deniedKey: 'permissions.denied.send_files',
  },
  'messaging.send_voice': {
    action: 'messaging.send_voice', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.send_voice',
    deniedKey: 'permissions.denied.send_voice',
  },
  'messaging.edit_own': {
    action: 'messaging.edit_own', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.edit_own',
    deniedKey: 'permissions.denied.edit_own',
  },
  'messaging.delete_own': {
    action: 'messaging.delete_own', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.delete_own',
    deniedKey: 'permissions.denied.delete_own',
  },
  'messaging.delete_any': {
    action: 'messaging.delete_any', domain: 'messaging',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.delete_any',
    deniedKey: 'permissions.denied.delete_any',
  },
  'messaging.pin': {
    action: 'messaging.pin', domain: 'messaging',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.pin_message',
    deniedKey: 'permissions.denied.pin_message',
  },
  'messaging.react': {
    action: 'messaging.react', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.react',
    deniedKey: 'permissions.denied.react',
  },
  'messaging.reply': {
    action: 'messaging.reply', domain: 'messaging',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.reply',
    deniedKey: 'permissions.denied.reply',
  },
  'messaging.mention_all': {
    action: 'messaging.mention_all', domain: 'messaging',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.mention_all',
    deniedKey: 'permissions.denied.mention_all',
  },

  // ══════════════════════════════════════════════════════════════
  //  CALLING
  // ══════════════════════════════════════════════════════════════

  'calling.start_audio': {
    action: 'calling.start_audio', domain: 'calling',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.start_audio',
    deniedKey: 'permissions.denied.start_audio',
  },
  'calling.start_video': {
    action: 'calling.start_video', domain: 'calling',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.start_video',
    deniedKey: 'permissions.denied.start_video',
  },
  'calling.join': {
    action: 'calling.join', domain: 'calling',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.join_call',
    deniedKey: 'permissions.denied.join_call',
  },
  'calling.unmute_self': {
    action: 'calling.unmute_self', domain: 'calling',
    minGroupRole: 'member', minCallRole: 'participant', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.unmute_self',
    deniedKey: 'permissions.denied.unmute_self',
  },
  'calling.enable_video_self': {
    action: 'calling.enable_video_self', domain: 'calling',
    minGroupRole: 'member', minCallRole: 'participant', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.enable_video',
    deniedKey: 'permissions.denied.enable_video',
  },
  'calling.end_call': {
    action: 'calling.end_call', domain: 'calling',
    minGroupRole: 'admin', minCallRole: 'host', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.end_call',
    deniedKey: 'permissions.denied.end_call',
  },

  // ══════════════════════════════════════════════════════════════
  //  SCREEN SHARE
  // ══════════════════════════════════════════════════════════════

  'screenshare.request': {
    action: 'screenshare.request', domain: 'screenshare',
    minGroupRole: 'member', minCallRole: 'participant', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.request_share',
    deniedKey: 'permissions.denied.request_share',
  },
  'screenshare.share': {
    action: 'screenshare.share', domain: 'screenshare',
    minGroupRole: 'member', minCallRole: 'participant', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.share_screen',
    deniedKey: 'permissions.denied.share_screen',
  },
  'screenshare.force_stop': {
    action: 'screenshare.force_stop', domain: 'screenshare',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.force_stop_share',
    deniedKey: 'permissions.denied.force_stop_share',
  },

  // ══════════════════════════════════════════════════════════════
  //  MEMBERS
  // ══════════════════════════════════════════════════════════════

  'members.invite': {
    action: 'members.invite', domain: 'members',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.invite',
    deniedKey: 'permissions.denied.invite',
  },
  'members.remove': {
    action: 'members.remove', domain: 'members',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.remove_member',
    deniedKey: 'permissions.denied.remove_member',
  },
  'members.view_list': {
    action: 'members.view_list', domain: 'members',
    minGroupRole: 'member', minCallRole: null, allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.view_members',
    deniedKey: 'permissions.denied.view_members',
  },
  'members.leave': {
    action: 'members.leave', domain: 'members',
    minGroupRole: 'member', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.leave_group',
    deniedKey: 'permissions.denied.leave_group',
  },

  // ══════════════════════════════════════════════════════════════
  //  ROLES
  // ══════════════════════════════════════════════════════════════

  'roles.view': {
    action: 'roles.view', domain: 'roles',
    minGroupRole: 'member', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.view_roles',
    deniedKey: 'permissions.denied.view_roles',
  },
  'roles.change': {
    action: 'roles.change', domain: 'roles',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.change_role',
    deniedKey: 'permissions.denied.change_role',
  },
  'roles.view_permissions': {
    action: 'roles.view_permissions', domain: 'roles',
    minGroupRole: 'member', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.view_permissions',
    deniedKey: 'permissions.denied.view_permissions',
  },

  // ══════════════════════════════════════════════════════════════
  //  ROOM CONTROL
  // ══════════════════════════════════════════════════════════════

  'room_control.lock': {
    action: 'room_control.lock', domain: 'room_control',
    minGroupRole: 'admin', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.lock_room',
    deniedKey: 'permissions.denied.lock_room',
  },
  'room_control.unlock': {
    action: 'room_control.unlock', domain: 'room_control',
    minGroupRole: 'admin', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.unlock_room',
    deniedKey: 'permissions.denied.unlock_room',
  },
  'room_control.set_slow_mode': {
    action: 'room_control.set_slow_mode', domain: 'room_control',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.slow_mode',
    deniedKey: 'permissions.denied.slow_mode',
  },
  'room_control.clear_chat': {
    action: 'room_control.clear_chat', domain: 'room_control',
    minGroupRole: 'owner', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.clear_chat',
    deniedKey: 'permissions.denied.clear_chat',
  },

  // ══════════════════════════════════════════════════════════════
  //  MODERATION
  // ══════════════════════════════════════════════════════════════

  'moderation.mute_user': {
    action: 'moderation.mute_user', domain: 'moderation',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.mute_user',
    deniedKey: 'permissions.denied.mute_user',
  },
  'moderation.deafen_user': {
    action: 'moderation.deafen_user', domain: 'moderation',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.deafen_user',
    deniedKey: 'permissions.denied.deafen_user',
  },
  'moderation.warn_user': {
    action: 'moderation.warn_user', domain: 'moderation',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.warn_user',
    deniedKey: 'permissions.denied.warn_user',
  },
  'moderation.timeout_user': {
    action: 'moderation.timeout_user', domain: 'moderation',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: true, visibleInSimpleMode: false,
    labelKey: 'permissions.action.timeout_user',
    deniedKey: 'permissions.denied.timeout_user',
  },
  'moderation.view_audit_log': {
    action: 'moderation.view_audit_log', domain: 'moderation',
    minGroupRole: 'moderator', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.view_audit',
    deniedKey: 'permissions.denied.view_audit',
  },

  // ══════════════════════════════════════════════════════════════
  //  GROUP CONFIG
  // ══════════════════════════════════════════════════════════════

  'group_config.edit_name': {
    action: 'group_config.edit_name', domain: 'group_config',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.edit_name',
    deniedKey: 'permissions.denied.edit_name',
  },
  'group_config.edit_desc': {
    action: 'group_config.edit_desc', domain: 'group_config',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.edit_desc',
    deniedKey: 'permissions.denied.edit_desc',
  },
  'group_config.edit_avatar': {
    action: 'group_config.edit_avatar', domain: 'group_config',
    minGroupRole: 'admin', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.edit_avatar',
    deniedKey: 'permissions.denied.edit_avatar',
  },
  'group_config.delete_group': {
    action: 'group_config.delete_group', domain: 'group_config',
    minGroupRole: 'owner', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.delete_group',
    deniedKey: 'permissions.denied.delete_group',
  },
  'group_config.transfer_owner': {
    action: 'group_config.transfer_owner', domain: 'group_config',
    minGroupRole: 'owner', minCallRole: null, allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.transfer_owner',
    deniedKey: 'permissions.denied.transfer_owner',
  },

  // ══════════════════════════════════════════════════════════════
  //  CALL FLOOR CONTROL
  // ══════════════════════════════════════════════════════════════

  'call_floor.mute_all': {
    action: 'call_floor.mute_all', domain: 'call_floor',
    minGroupRole: 'admin', minCallRole: 'host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.mute_all',
    deniedKey: 'permissions.denied.mute_all',
  },
  'call_floor.request_speak': {
    action: 'call_floor.request_speak', domain: 'call_floor',
    minGroupRole: 'member', minCallRole: 'viewer', allowedInDM: true,
    requiresHigherPower: false, visibleInSimpleMode: true,
    labelKey: 'permissions.action.request_speak',
    deniedKey: 'permissions.denied.request_speak',
  },
  'call_floor.grant_speak': {
    action: 'call_floor.grant_speak', domain: 'call_floor',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.grant_speak',
    deniedKey: 'permissions.denied.grant_speak',
  },
  'call_floor.deny_speak': {
    action: 'call_floor.deny_speak', domain: 'call_floor',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.deny_speak',
    deniedKey: 'permissions.denied.deny_speak',
  },
  'call_floor.lower_all_hands': {
    action: 'call_floor.lower_all_hands', domain: 'call_floor',
    minGroupRole: 'moderator', minCallRole: 'co-host', allowedInDM: false,
    requiresHigherPower: false, visibleInSimpleMode: false,
    labelKey: 'permissions.action.lower_hands',
    deniedKey: 'permissions.denied.lower_hands',
  },
};

// ── Query Helpers ──────────────────────────────────────────────

/**
 * Check if a channel role is permitted to perform an action.
 */
export function isActionAllowed(
  action: PermissionAction,
  role: ChannelRole,
  context: PermissionContext,
): boolean {
  const rule = PERMISSION_MATRIX[action];
  if (!rule) return false;

  // DM context: use allowedInDM flag
  if (context === 'dm') return rule.allowedInDM;

  // Group/call context: check minimum role
  return CHANNEL_ROLE_POWER[role] >= CHANNEL_ROLE_POWER[rule.minGroupRole];
}

/**
 * Check if a call role is permitted for a call-specific action.
 */
export function isCallActionAllowed(
  action: PermissionAction,
  callRole: CallRole,
): boolean {
  const rule = PERMISSION_MATRIX[action];
  if (!rule || !rule.minCallRole) return true; // No call role restriction

  return CALL_ROLE_POWER[callRole] >= CALL_ROLE_POWER[rule.minCallRole];
}

/**
 * Full permission check: channel role + call role + context + power hierarchy.
 */
export function checkPermission(params: {
  action: PermissionAction;
  actorRole: ChannelRole;
  actorCallRole?: CallRole;
  context: PermissionContext;
  /** Required for actions with requiresHigherPower */
  targetRole?: ChannelRole;
}): { allowed: boolean; deniedKey?: string } {
  const rule = PERMISSION_MATRIX[params.action];
  if (!rule) return { allowed: false, deniedKey: 'permissions.denied.unknown_action' };

  // DM: use allowedInDM
  if (params.context === 'dm') {
    return rule.allowedInDM
      ? { allowed: true }
      : { allowed: false, deniedKey: rule.deniedKey };
  }

  // Check channel role
  if (CHANNEL_ROLE_POWER[params.actorRole] < CHANNEL_ROLE_POWER[rule.minGroupRole]) {
    return { allowed: false, deniedKey: rule.deniedKey };
  }

  // Check call role if in call context
  if (params.context === 'call' && params.actorCallRole && rule.minCallRole) {
    if (CALL_ROLE_POWER[params.actorCallRole] < CALL_ROLE_POWER[rule.minCallRole]) {
      return { allowed: false, deniedKey: rule.deniedKey };
    }
  }

  // Check power hierarchy (for moderation actions)
  if (rule.requiresHigherPower && params.targetRole) {
    if (CHANNEL_ROLE_POWER[params.actorRole] <= CHANNEL_ROLE_POWER[params.targetRole]) {
      return { allowed: false, deniedKey: 'permissions.denied.insufficient_authority' };
    }
  }

  return { allowed: true };
}

/**
 * Get all actions a role can perform in a given context.
 */
export function getAllowedActions(role: ChannelRole, context: PermissionContext): PermissionAction[] {
  return (Object.keys(PERMISSION_MATRIX) as PermissionAction[]).filter(
    action => isActionAllowed(action, role, context),
  );
}

/**
 * Get all actions within a specific domain.
 */
export function getActionsByDomain(domain: PermissionDomain): PermissionAction[] {
  return (Object.keys(PERMISSION_MATRIX) as PermissionAction[]).filter(
    action => PERMISSION_MATRIX[action].domain === domain,
  );
}

/**
 * Get actions visible in Simple Mode.
 */
export function getSimpleModeActions(): PermissionAction[] {
  return (Object.keys(PERMISSION_MATRIX) as PermissionAction[]).filter(
    action => PERMISSION_MATRIX[action].visibleInSimpleMode,
  );
}

/**
 * Get a human-readable summary of what a role can do.
 * Returns arrays of action labelKeys grouped by domain.
 */
export function getRoleCapabilitySummary(
  role: ChannelRole,
): Record<PermissionDomain, string[]> {
  const summary: Record<PermissionDomain, string[]> = {
    messaging: [], calling: [], screenshare: [], members: [], roles: [],
    room_control: [], moderation: [], group_config: [], call_floor: [],
  };

  const allowed = getAllowedActions(role, 'group');
  for (const action of allowed) {
    const rule = PERMISSION_MATRIX[action];
    summary[rule.domain].push(rule.labelKey);
  }

  return summary;
}
