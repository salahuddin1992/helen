/**
 * permissions/ — Phase 15: Collaboration Permissions & Role Architecture
 *
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │              CommClient Permissions & Roles Architecture                  │
 * │                                                                          │
 * │  ┌──────────────────────────────────────────────────────────────────┐   │
 * │  │                        RoleModel.ts                              │   │
 * │  │                                                                  │   │
 * │  │  4-tier channel roles: owner > admin > moderator > member        │   │
 * │  │  4-tier call roles:    host  > co-host > participant > viewer    │   │
 * │  │  DM context:           no roles — equal peers                    │   │
 * │  │                                                                  │   │
 * │  │  • Role power levels & hierarchy comparison                      │   │
 * │  │  • Role limits (1 owner, 3 admins, 10 moderators)               │   │
 * │  │  • Channel→Call role mapping                                     │   │
 * │  │  • Role assignment validation engine                             │   │
 * │  │  • Badge display helpers                                         │   │
 * │  └──────────────────────────────────────────────────────────────────┘   │
 * │                                │                                        │
 * │                                ▼                                        │
 * │  ┌──────────────────────────────────────────────────────────────────┐   │
 * │  │                     PermissionMatrix.ts                          │   │
 * │  │                                                                  │   │
 * │  │  9 domains × 4 roles = 40 discrete permission actions            │   │
 * │  │                                                                  │   │
 * │  │  Domains:                                                        │   │
 * │  │    messaging · calling · screenshare · members · roles           │   │
 * │  │    room_control · moderation · group_config · call_floor         │   │
 * │  │                                                                  │   │
 * │  │  Each action defines:                                            │   │
 * │  │    minGroupRole · minCallRole · allowedInDM                      │   │
 * │  │    requiresHigherPower · visibleInSimpleMode                     │   │
 * │  │    labelKey · deniedKey (i18n)                                   │   │
 * │  │                                                                  │   │
 * │  │  Query: isActionAllowed, checkPermission, getAllowedActions       │   │
 * │  └──────────────────────────────────────────────────────────────────┘   │
 * │                                │                                        │
 * │                    ┌───────────┴───────────┐                            │
 * │                    ▼                       ▼                            │
 * │  ┌─────────────────────────┐  ┌──────────────────────────────────┐    │
 * │  │  GroupOwnershipRules.ts │  │       PermissionGuard.ts         │    │
 * │  │                         │  │                                  │    │
 * │  │  • Transfer validation  │  │  CLIENT SIDE:                    │    │
 * │  │  • Succession chain     │  │    guardUI() → visible/hidden    │    │
 * │  │  • Owner leave flow     │  │    preCheckAction() → allow/deny │    │
 * │  │  • Group lifecycle FSM  │  │    isDomainVisible()             │    │
 * │  │  • Orphan → auto-delete │  │    guardUIBatch()                │    │
 * │  │  • Deletion policy      │  │                                  │    │
 * │  │  • Socket event specs   │  │  SERVER CONTRACT:                │    │
 * │  │                         │  │    API_PERMISSION_MAP (REST)     │    │
 * │  │                         │  │    SOCKET_PERMISSION_MAP (WS)    │    │
 * │  │                         │  │    PermissionDeniedResponse      │    │
 * │  │                         │  │    PermissionAuditEntry          │    │
 * │  │                         │  │    Socket event payloads         │    │
 * │  └─────────────────────────┘  └──────────────────────────────────┘    │
 * │                                                                        │
 * │  ┌──────────────────────────────────────────────────────────────────┐   │
 * │  │                    Integration Points                            │   │
 * │  │                                                                  │   │
 * │  │  Channel (types/index.ts)                                        │   │
 * │  │    ChannelMember.role: 'owner'|'admin'|'moderator'|'member'     │   │
 * │  │    → Existing 'admin'|'moderator'|'member' + new 'owner'        │   │
 * │  │                                                                  │   │
 * │  │  Call (GroupCallManager.ts)                                      │   │
 * │  │    ParticipantMetadata.role: 'host'|'co-host'|'participant'     │   │
 * │  │    → Derived from channel role via deriveCallRole()              │   │
 * │  │                                                                  │   │
 * │  │  PresenterManager.ts                                             │   │
 * │  │    → screenshare.request / screenshare.force_stop permissions    │   │
 * │  │                                                                  │   │
 * │  │  app-mode.store.ts                                               │   │
 * │  │    → visibleInSimpleMode flag gates Advanced-only actions        │   │
 * │  │                                                                  │   │
 * │  │  ErrorClassifier (Phase 13)                                      │   │
 * │  │    → 'auth' domain error codes for permission denials            │   │
 * │  │                                                                  │   │
 * │  │  DiagnosticsLogger (Phase 14)                                    │   │
 * │  │    → 'auth' category for permission audit logging                │   │
 * │  └──────────────────────────────────────────────────────────────────┘   │
 * └──────────────────────────────────────────────────────────────────────────┘
 */

// ── RoleModel ───────────────────────────────────────────────────
export {
  type ChannelRole,
  type CallRole,
  type PermissionContext,
  type RoleMeta,
  type RoleChangeRequest,
  type RoleChangeResult,
  CHANNEL_ROLE_POWER,
  CHANNEL_ROLE_LIMITS,
  CHANNEL_ROLE_META,
  CALL_ROLE_POWER,
  CHANNEL_TO_CALL_ROLE,
  CALL_ROLE_META,
  hasHigherPower,
  meetsMinimumRole,
  getAssignableRoles,
  deriveCallRole,
  getRoleHierarchy,
  validateRoleChange,
  getRoleBadgeInfo,
  shouldShowRoleBadge,
  getCallRoleBadge,
  getPermissionContext,
} from './RoleModel';

// ── PermissionMatrix ────────────────────────────────────────────
export {
  type PermissionDomain,
  type PermissionAction,
  type PermissionRule,
  PERMISSION_MATRIX,
  isActionAllowed,
  isCallActionAllowed,
  checkPermission,
  getAllowedActions,
  getActionsByDomain,
  getSimpleModeActions,
  getRoleCapabilitySummary,
} from './PermissionMatrix';

// ── GroupOwnershipRules ─────────────────────────────────────────
export {
  type OwnershipChangeReason,
  type OwnershipTransferRequest,
  type OwnershipChangeEvent,
  type SuccessionCandidate,
  type OwnershipConfig,
  type TransferValidation,
  type OwnerLeaveDecision,
  type GroupLifecycleState,
  type GroupDeletionPolicy,
  type OwnershipSocketPayloads,
  DEFAULT_OWNERSHIP_CONFIG,
  DEFAULT_DELETION_POLICY,
  OWNERSHIP_SOCKET_EVENTS,
  validateTransfer,
  computeSuccessionPriority,
  pickSuccessor,
  evaluateOwnerLeave,
} from './GroupOwnershipRules';

// ── PermissionGuard ─────────────────────────────────────────────
export {
  type UIVisibility,
  type UIGuardResult,
  type UserPermissionContext,
  type PreCheckResult,
  type PermissionDeniedResponse,
  type PermissionAuditEntry,
  type PermissionSocketPayloads,
  PERMISSION_SOCKET_EVENTS,
  API_PERMISSION_MAP,
  SOCKET_PERMISSION_MAP,
  guardUI,
  guardUIBatch,
  isDomainVisible,
  preCheckAction,
} from './PermissionGuard';
