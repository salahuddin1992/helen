/**
 * RoleModel.ts — Phase 15: Role Definitions, Hierarchy & Assignment Engine
 *
 * Defines the four-tier role system for CommClient group contexts.
 * Roles are CONTEXT-SCOPED:
 *   - Channel roles: owner / admin / moderator / member
 *   - Call roles: host / co-host / participant / viewer
 *   - DM: no roles — both parties are equal peers
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │                         Role Hierarchy                                │
 * │                                                                      │
 * │  ┌─────────┐                                                         │
 * │  │  OWNER  │  ← exactly ONE per channel, cannot be removed           │
 * │  │ (host)  │  ← auto-assigned to channel creator                     │
 * │  └────┬────┘                                                         │
 * │       │ can promote/demote all below                                  │
 * │  ┌────▼────┐                                                         │
 * │  │  ADMIN  │  ← trusted lieutenants (max 3 per channel)              │
 * │  │(co-host)│  ← can manage moderators and members                    │
 * │  └────┬────┘                                                         │
 * │       │ can manage members, limited moderator control                 │
 * │  ┌────▼──────┐                                                       │
 * │  │ MODERATOR │  ← can mute, pin messages, manage call floor          │
 * │  │           │  ← CANNOT change roles or remove members              │
 * │  └────┬──────┘                                                       │
 * │       │ standard member permissions                                   │
 * │  ┌────▼────┐                                                         │
 * │  │ MEMBER  │  ← default role for all who join                        │
 * │  │(viewer) │  ← can chat, call, share screen (if allowed)            │
 * │  └─────────┘                                                         │
 * │                                                                      │
 * │  DM Context: both users are EQUAL — no roles, no hierarchy           │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * Role assignment is always SERVER-AUTHORITATIVE. The client maintains a
 * local cache of roles for UI gating but the server is the source of truth.
 * Socket events keep the client cache synchronized.
 */

// ── Channel Roles ──────────────────────────────────────────────

/**
 * Channel (group) context roles. Ordered by power level.
 *
 * NOTE: The existing ChannelMember.role field uses 'admin' | 'moderator' | 'member'.
 * We ADD 'owner' as the top tier without modifying the existing type —
 * the backend will return 'owner' for the creator going forward, and
 * old groups will be migrated (creator → owner) via a one-time backend migration.
 */
export type ChannelRole = 'owner' | 'admin' | 'moderator' | 'member';

/**
 * Numeric power level for each role. Higher = more authority.
 * Used for comparison: can user A act on user B? → A.power > B.power.
 */
export const CHANNEL_ROLE_POWER: Record<ChannelRole, number> = {
  owner:     100,
  admin:      75,
  moderator:  50,
  member:     10,
};

/**
 * Maximum count per role per channel (owner is always exactly 1).
 */
export const CHANNEL_ROLE_LIMITS: Record<ChannelRole, number> = {
  owner:      1,
  admin:      3,
  moderator: 10,
  member:    Infinity,
};

/**
 * Channel role metadata for UI display.
 */
export interface RoleMeta {
  key: ChannelRole;
  power: number;
  limit: number;
  /** i18n key for display name */
  labelKey: string;
  /** i18n key for short description */
  descriptionKey: string;
  /** Badge color (hex) */
  color: string;
  /** Badge icon name (lucide-react) */
  icon: string;
}

export const CHANNEL_ROLE_META: Record<ChannelRole, RoleMeta> = {
  owner: {
    key: 'owner',
    power: 100,
    limit: 1,
    labelKey: 'permissions.role.owner',
    descriptionKey: 'permissions.role.owner_desc',
    color: '#F59E0B',
    icon: 'Crown',
  },
  admin: {
    key: 'admin',
    power: 75,
    limit: 3,
    labelKey: 'permissions.role.admin',
    descriptionKey: 'permissions.role.admin_desc',
    color: '#3B82F6',
    icon: 'Shield',
  },
  moderator: {
    key: 'moderator',
    power: 50,
    limit: 10,
    labelKey: 'permissions.role.moderator',
    descriptionKey: 'permissions.role.moderator_desc',
    color: '#10B981',
    icon: 'ShieldCheck',
  },
  member: {
    key: 'member',
    power: 10,
    limit: Infinity,
    labelKey: 'permissions.role.member',
    descriptionKey: 'permissions.role.member_desc',
    color: '#6B7280',
    icon: 'User',
  },
};

// ── Call Roles ──────────────────────────────────────────────────

/**
 * Call-scoped roles. These exist ONLY during an active call and are
 * derived from channel roles with optional overrides.
 *
 * Mapping: owner → host, admin → co-host, moderator/member → participant.
 * 'viewer' is for spectators in large calls (future feature).
 */
export type CallRole = 'host' | 'co-host' | 'participant' | 'viewer';

export const CALL_ROLE_POWER: Record<CallRole, number> = {
  host:        100,
  'co-host':    75,
  participant:  50,
  viewer:       10,
};

/**
 * Default mapping from channel role to call role.
 * The call initiator always gets 'host' regardless of channel role.
 */
export const CHANNEL_TO_CALL_ROLE: Record<ChannelRole, CallRole> = {
  owner:     'host',
  admin:     'co-host',
  moderator: 'participant',
  member:    'participant',
};

export const CALL_ROLE_META: Record<CallRole, {
  labelKey: string;
  descriptionKey: string;
  color: string;
  icon: string;
}> = {
  host: {
    labelKey: 'permissions.call_role.host',
    descriptionKey: 'permissions.call_role.host_desc',
    color: '#F59E0B',
    icon: 'Crown',
  },
  'co-host': {
    labelKey: 'permissions.call_role.co_host',
    descriptionKey: 'permissions.call_role.co_host_desc',
    color: '#3B82F6',
    icon: 'Shield',
  },
  participant: {
    labelKey: 'permissions.call_role.participant',
    descriptionKey: 'permissions.call_role.participant_desc',
    color: '#10B981',
    icon: 'User',
  },
  viewer: {
    labelKey: 'permissions.call_role.viewer',
    descriptionKey: 'permissions.call_role.viewer_desc',
    color: '#6B7280',
    icon: 'Eye',
  },
};

// ── Role Comparison Helpers ────────────────────────────────────

/**
 * Check if roleA has higher or equal authority than roleB.
 * Used to determine if a user can act on another user.
 *
 * CRITICAL RULE: a user can only act on users with STRICTLY LOWER power.
 * Equal power users cannot affect each other. Owner is untouchable.
 */
export function hasHigherPower(actorRole: ChannelRole, targetRole: ChannelRole): boolean {
  return CHANNEL_ROLE_POWER[actorRole] > CHANNEL_ROLE_POWER[targetRole];
}

/**
 * Check if roleA meets the minimum required role level.
 */
export function meetsMinimumRole(role: ChannelRole, minimum: ChannelRole): boolean {
  return CHANNEL_ROLE_POWER[role] >= CHANNEL_ROLE_POWER[minimum];
}

/**
 * Get all roles that a given role can promote/demote to.
 * You can only set roles STRICTLY BELOW your own level.
 * Owner is the exception: can set any role including admin.
 */
export function getAssignableRoles(actorRole: ChannelRole): ChannelRole[] {
  const actorPower = CHANNEL_ROLE_POWER[actorRole];
  return (['admin', 'moderator', 'member'] as ChannelRole[]).filter(
    r => CHANNEL_ROLE_POWER[r] < actorPower,
  );
}

/**
 * Derive call role from channel role + whether the user initiated the call.
 */
export function deriveCallRole(channelRole: ChannelRole, isCallInitiator: boolean): CallRole {
  if (isCallInitiator) return 'host';
  return CHANNEL_TO_CALL_ROLE[channelRole];
}

/**
 * Get the sorted list of all channel roles, highest first.
 */
export function getRoleHierarchy(): ChannelRole[] {
  return ['owner', 'admin', 'moderator', 'member'];
}

// ── Role Assignment Validation ─────────────────────────────────

export interface RoleChangeRequest {
  actorUserId: string;
  actorRole: ChannelRole;
  targetUserId: string;
  targetCurrentRole: ChannelRole;
  newRole: ChannelRole;
  channelId: string;
}

export interface RoleChangeResult {
  allowed: boolean;
  reason?: string;
  reasonKey?: string; // i18n key
}

/**
 * Validate whether a role change is permitted.
 *
 * Rules:
 * 1. Cannot change own role (except owner transferring ownership)
 * 2. Cannot act on someone with equal or higher power
 * 3. Cannot assign a role equal to or higher than your own (except owner)
 * 4. Cannot exceed role limits (e.g., max 3 admins)
 * 5. Owner role can only be transferred, not assigned
 * 6. The 'member' role can always be assigned (it's a demotion for everyone)
 */
export function validateRoleChange(
  request: RoleChangeRequest,
  currentRoleCounts: Record<ChannelRole, number>,
): RoleChangeResult {
  const { actorRole, targetCurrentRole, newRole, actorUserId, targetUserId } = request;

  // Rule 0: No-op check
  if (targetCurrentRole === newRole) {
    return { allowed: false, reason: 'Target already has this role', reasonKey: 'permissions.error.same_role' };
  }

  // Rule 1: Cannot change own role (unless ownership transfer handled separately)
  if (actorUserId === targetUserId) {
    return { allowed: false, reason: 'Cannot change own role', reasonKey: 'permissions.error.self_role' };
  }

  // Rule 2: Cannot act on higher or equal power
  if (!hasHigherPower(actorRole, targetCurrentRole)) {
    return { allowed: false, reason: 'Insufficient authority over target', reasonKey: 'permissions.error.insufficient_authority' };
  }

  // Rule 3: Cannot assign role ≥ own (except owner can assign admin)
  if (actorRole !== 'owner' && CHANNEL_ROLE_POWER[newRole] >= CHANNEL_ROLE_POWER[actorRole]) {
    return { allowed: false, reason: 'Cannot assign role equal to or above own', reasonKey: 'permissions.error.role_too_high' };
  }

  // Rule 4: Owner role cannot be assigned (only transferred via GroupOwnershipRules)
  if (newRole === 'owner') {
    return { allowed: false, reason: 'Owner role is transferred, not assigned', reasonKey: 'permissions.error.owner_transfer_only' };
  }

  // Rule 5: Check role limits
  const limit = CHANNEL_ROLE_LIMITS[newRole];
  const currentCount = currentRoleCounts[newRole] || 0;
  if (currentCount >= limit) {
    return {
      allowed: false,
      reason: `Maximum ${limit} ${newRole}(s) allowed`,
      reasonKey: 'permissions.error.role_limit_reached',
    };
  }

  return { allowed: true };
}

// ── Role Display Helpers ───────────────────────────────────────

/**
 * Get a user-friendly label for a channel role badge.
 */
export function getRoleBadgeInfo(role: ChannelRole): { color: string; icon: string; labelKey: string } {
  const meta = CHANNEL_ROLE_META[role];
  return { color: meta.color, icon: meta.icon, labelKey: meta.labelKey };
}

/**
 * Should a role badge be displayed for this role?
 * Members don't get a badge — only owner/admin/moderator.
 */
export function shouldShowRoleBadge(role: ChannelRole): boolean {
  return role !== 'member';
}

/**
 * Get a call role badge for in-call overlay.
 */
export function getCallRoleBadge(callRole: CallRole): { color: string; icon: string; labelKey: string } | null {
  if (callRole === 'participant') return null; // No badge for regular participants
  const meta = CALL_ROLE_META[callRole];
  return { color: meta.color, icon: meta.icon, labelKey: meta.labelKey };
}

// ── Context Type Helper ────────────────────────────────────────

/**
 * Context types where permissions apply.
 * DM = direct message (1-to-1), no roles.
 * GROUP = group channel, full role system.
 * CALL = active call overlay, call-scoped roles.
 */
export type PermissionContext = 'dm' | 'group' | 'call';

/**
 * Determine permission context from channel type.
 */
export function getPermissionContext(channelType: 'dm' | 'group', inCall: boolean): PermissionContext {
  if (inCall) return 'call';
  return channelType;
}
