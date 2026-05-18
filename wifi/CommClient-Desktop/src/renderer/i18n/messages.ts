/**
 * messages.ts — Structured UX message catalog for CommClient.
 *
 * This module provides a typed, structured message system that sits ON TOP
 * of the flat i18n key-value store. Each message object carries:
 *
 *   - title:     i18n key for the headline / label
 *   - body:      i18n key for the longer description
 *   - hint:      i18n key for a recovery action hint (optional)
 *   - icon:      Lucide icon name (string) for the UI to resolve
 *   - severity:  info | success | warning | error | neutral
 *   - action:    primary actionable button label key (optional)
 *   - autoHide:  ms after which the message should auto-dismiss (0 = sticky)
 *   - uiBehavior: how the UI should render this message
 *
 * Components consume these via `getMessage(id)` and render accordingly.
 * The i18n keys referenced here MUST exist in i18n/index.ts.
 *
 * Design philosophy:
 *   - A child should understand every message
 *   - Every error tells you what happened AND what to do
 *   - No technical jargon (no "timeout", "socket", "500", "ECONNREFUSED")
 *   - Positive framing: "We're working on it" not "Failed"
 */

import { t } from './index';

// ── Types ────────────────────────────────────────────────

export type MessageSeverity = 'info' | 'success' | 'warning' | 'error' | 'neutral';

export type UIBehavior =
  | 'toast'           // Brief notification, auto-dismisses
  | 'banner'          // Persistent strip at top/bottom of view
  | 'overlay'         // Full-screen blocking overlay
  | 'inline'          // Inline within form or component
  | 'modal'           // Modal dialog requiring user action
  | 'status_bar'      // Thin status line (like RoomStateBar)
  | 'badge';          // Small badge/dot indicator

export interface AppMessage {
  id: string;
  category: MessageCategory;
  severity: MessageSeverity;
  icon: string;
  title: string;        // i18n key
  body: string;         // i18n key
  hint?: string;        // i18n key — recovery guidance
  action?: string;      // i18n key — primary button label
  secondaryAction?: string; // i18n key — secondary button label
  autoHide: number;     // milliseconds; 0 = sticky
  uiBehavior: UIBehavior;
}

export type MessageCategory =
  | 'connection'
  | 'network'
  | 'call'
  | 'media'
  | 'chat'
  | 'group'
  | 'auth'
  | 'startup'
  | 'file'
  | 'permission'
  | 'status'
  | 'confirmation';

// ── Resolved message (with translated strings) ──────────

export interface ResolvedMessage {
  id: string;
  category: MessageCategory;
  severity: MessageSeverity;
  icon: string;
  title: string;        // Translated string
  body: string;         // Translated string
  hint?: string;        // Translated string
  action?: string;      // Translated string
  secondaryAction?: string;
  autoHide: number;
  uiBehavior: UIBehavior;
}

// ── Message Catalog ─────────────────────────────────────

const catalog: Record<string, AppMessage> = {

  // ════════════════════════════════════════════════════════
  //  CONNECTION
  // ════════════════════════════════════════════════════════

  'connection.lost': {
    id: 'connection.lost',
    category: 'connection',
    severity: 'warning',
    icon: 'WifiOff',
    title: 'msg.connection.lost.title',
    body: 'msg.connection.lost.body',
    hint: 'msg.connection.lost.hint',
    action: 'msg.connection.lost.action',
    autoHide: 0,
    uiBehavior: 'banner',
  },

  'connection.reconnecting': {
    id: 'connection.reconnecting',
    category: 'connection',
    severity: 'warning',
    icon: 'RefreshCw',
    title: 'msg.connection.reconnecting.title',
    body: 'msg.connection.reconnecting.body',
    autoHide: 0,
    uiBehavior: 'banner',
  },

  'connection.restored': {
    id: 'connection.restored',
    category: 'connection',
    severity: 'success',
    icon: 'Wifi',
    title: 'msg.connection.restored.title',
    body: 'msg.connection.restored.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'connection.server_unreachable': {
    id: 'connection.server_unreachable',
    category: 'connection',
    severity: 'error',
    icon: 'ServerOff',
    title: 'msg.connection.server_unreachable.title',
    body: 'msg.connection.server_unreachable.body',
    hint: 'msg.connection.server_unreachable.hint',
    action: 'msg.connection.server_unreachable.action',
    autoHide: 0,
    uiBehavior: 'overlay',
  },

  'connection.server_restarting': {
    id: 'connection.server_restarting',
    category: 'connection',
    severity: 'info',
    icon: 'RotateCw',
    title: 'msg.connection.server_restarting.title',
    body: 'msg.connection.server_restarting.body',
    autoHide: 0,
    uiBehavior: 'banner',
  },

  // ════════════════════════════════════════════════════════
  //  NETWORK
  // ════════════════════════════════════════════════════════

  'network.offline': {
    id: 'network.offline',
    category: 'network',
    severity: 'error',
    icon: 'WifiOff',
    title: 'msg.network.offline.title',
    body: 'msg.network.offline.body',
    hint: 'msg.network.offline.hint',
    action: 'msg.network.offline.action',
    autoHide: 0,
    uiBehavior: 'overlay',
  },

  'network.wifi_lost': {
    id: 'network.wifi_lost',
    category: 'network',
    severity: 'warning',
    icon: 'WifiOff',
    title: 'msg.network.wifi_lost.title',
    body: 'msg.network.wifi_lost.body',
    hint: 'msg.network.wifi_lost.hint',
    autoHide: 0,
    uiBehavior: 'banner',
  },

  'network.wifi_restored': {
    id: 'network.wifi_restored',
    category: 'network',
    severity: 'success',
    icon: 'Wifi',
    title: 'msg.network.wifi_restored.title',
    body: 'msg.network.wifi_restored.body',
    autoHide: 4000,
    uiBehavior: 'toast',
  },

  'network.slow': {
    id: 'network.slow',
    category: 'network',
    severity: 'warning',
    icon: 'Gauge',
    title: 'msg.network.slow.title',
    body: 'msg.network.slow.body',
    hint: 'msg.network.slow.hint',
    autoHide: 8000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  CALL
  // ════════════════════════════════════════════════════════

  'call.failed': {
    id: 'call.failed',
    category: 'call',
    severity: 'error',
    icon: 'PhoneOff',
    title: 'msg.call.failed.title',
    body: 'msg.call.failed.body',
    hint: 'msg.call.failed.hint',
    action: 'msg.call.failed.action',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'call.ended': {
    id: 'call.ended',
    category: 'call',
    severity: 'neutral',
    icon: 'PhoneOff',
    title: 'msg.call.ended.title',
    body: 'msg.call.ended.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'call.user_busy': {
    id: 'call.user_busy',
    category: 'call',
    severity: 'info',
    icon: 'PhoneMissed',
    title: 'msg.call.user_busy.title',
    body: 'msg.call.user_busy.body',
    hint: 'msg.call.user_busy.hint',
    autoHide: 5000,
    uiBehavior: 'toast',
  },

  'call.no_answer': {
    id: 'call.no_answer',
    category: 'call',
    severity: 'info',
    icon: 'PhoneMissed',
    title: 'msg.call.no_answer.title',
    body: 'msg.call.no_answer.body',
    hint: 'msg.call.no_answer.hint',
    action: 'msg.call.no_answer.action',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'call.reconnecting': {
    id: 'call.reconnecting',
    category: 'call',
    severity: 'warning',
    icon: 'RefreshCw',
    title: 'msg.call.reconnecting.title',
    body: 'msg.call.reconnecting.body',
    autoHide: 0,
    uiBehavior: 'status_bar',
  },

  'call.quality_poor': {
    id: 'call.quality_poor',
    category: 'call',
    severity: 'warning',
    icon: 'SignalLow',
    title: 'msg.call.quality_poor.title',
    body: 'msg.call.quality_poor.body',
    hint: 'msg.call.quality_poor.hint',
    autoHide: 6000,
    uiBehavior: 'toast',
  },

  'call.full': {
    id: 'call.full',
    category: 'call',
    severity: 'warning',
    icon: 'Users',
    title: 'msg.call.full.title',
    body: 'msg.call.full.body',
    autoHide: 5000,
    uiBehavior: 'toast',
  },

  'call.user_joined': {
    id: 'call.user_joined',
    category: 'call',
    severity: 'info',
    icon: 'UserPlus',
    title: 'msg.call.user_joined.title',
    body: 'msg.call.user_joined.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'call.user_left': {
    id: 'call.user_left',
    category: 'call',
    severity: 'neutral',
    icon: 'UserMinus',
    title: 'msg.call.user_left.title',
    body: 'msg.call.user_left.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  MEDIA (Camera, Mic, Screen Share)
  // ════════════════════════════════════════════════════════

  'media.camera_not_found': {
    id: 'media.camera_not_found',
    category: 'media',
    severity: 'warning',
    icon: 'VideoOff',
    title: 'msg.media.camera_not_found.title',
    body: 'msg.media.camera_not_found.body',
    hint: 'msg.media.camera_not_found.hint',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'media.camera_in_use': {
    id: 'media.camera_in_use',
    category: 'media',
    severity: 'warning',
    icon: 'VideoOff',
    title: 'msg.media.camera_in_use.title',
    body: 'msg.media.camera_in_use.body',
    hint: 'msg.media.camera_in_use.hint',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'media.mic_blocked': {
    id: 'media.mic_blocked',
    category: 'media',
    severity: 'error',
    icon: 'MicOff',
    title: 'msg.media.mic_blocked.title',
    body: 'msg.media.mic_blocked.body',
    hint: 'msg.media.mic_blocked.hint',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'media.mic_not_found': {
    id: 'media.mic_not_found',
    category: 'media',
    severity: 'warning',
    icon: 'MicOff',
    title: 'msg.media.mic_not_found.title',
    body: 'msg.media.mic_not_found.body',
    hint: 'msg.media.mic_not_found.hint',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'media.screen_share_stopped': {
    id: 'media.screen_share_stopped',
    category: 'media',
    severity: 'info',
    icon: 'MonitorOff',
    title: 'msg.media.screen_share_stopped.title',
    body: 'msg.media.screen_share_stopped.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'media.screen_share_denied': {
    id: 'media.screen_share_denied',
    category: 'media',
    severity: 'info',
    icon: 'MonitorOff',
    title: 'msg.media.screen_share_denied.title',
    body: 'msg.media.screen_share_denied.body',
    hint: 'msg.media.screen_share_denied.hint',
    autoHide: 5000,
    uiBehavior: 'toast',
  },

  'media.speaker_not_found': {
    id: 'media.speaker_not_found',
    category: 'media',
    severity: 'warning',
    icon: 'VolumeX',
    title: 'msg.media.speaker_not_found.title',
    body: 'msg.media.speaker_not_found.body',
    hint: 'msg.media.speaker_not_found.hint',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  // ════════════════════════════════════════════════════════
  //  CHAT / MESSAGING
  // ════════════════════════════════════════════════════════

  'chat.send_failed': {
    id: 'chat.send_failed',
    category: 'chat',
    severity: 'error',
    icon: 'MessageCircleX',
    title: 'msg.chat.send_failed.title',
    body: 'msg.chat.send_failed.body',
    hint: 'msg.chat.send_failed.hint',
    action: 'msg.chat.send_failed.action',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'chat.send_retrying': {
    id: 'chat.send_retrying',
    category: 'chat',
    severity: 'warning',
    icon: 'RefreshCw',
    title: 'msg.chat.send_retrying.title',
    body: 'msg.chat.send_retrying.body',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'chat.message_deleted': {
    id: 'chat.message_deleted',
    category: 'chat',
    severity: 'neutral',
    icon: 'Trash2',
    title: 'msg.chat.message_deleted.title',
    body: 'msg.chat.message_deleted.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'chat.message_edited': {
    id: 'chat.message_edited',
    category: 'chat',
    severity: 'info',
    icon: 'Pencil',
    title: 'msg.chat.message_edited.title',
    body: 'msg.chat.message_edited.body',
    autoHide: 2000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  GROUP
  // ════════════════════════════════════════════════════════

  'group.created': {
    id: 'group.created',
    category: 'group',
    severity: 'success',
    icon: 'Users',
    title: 'msg.group.created.title',
    body: 'msg.group.created.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'group.joined': {
    id: 'group.joined',
    category: 'group',
    severity: 'success',
    icon: 'UserPlus',
    title: 'msg.group.joined.title',
    body: 'msg.group.joined.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'group.left': {
    id: 'group.left',
    category: 'group',
    severity: 'neutral',
    icon: 'LogOut',
    title: 'msg.group.left.title',
    body: 'msg.group.left.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'group.member_joined': {
    id: 'group.member_joined',
    category: 'group',
    severity: 'info',
    icon: 'UserPlus',
    title: 'msg.group.member_joined.title',
    body: 'msg.group.member_joined.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'group.member_left': {
    id: 'group.member_left',
    category: 'group',
    severity: 'neutral',
    icon: 'UserMinus',
    title: 'msg.group.member_left.title',
    body: 'msg.group.member_left.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  'group.member_removed': {
    id: 'group.member_removed',
    category: 'group',
    severity: 'warning',
    icon: 'UserX',
    title: 'msg.group.member_removed.title',
    body: 'msg.group.member_removed.body',
    autoHide: 4000,
    uiBehavior: 'toast',
  },

  'group.you_were_removed': {
    id: 'group.you_were_removed',
    category: 'group',
    severity: 'warning',
    icon: 'UserX',
    title: 'msg.group.you_were_removed.title',
    body: 'msg.group.you_were_removed.body',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'group.role_changed': {
    id: 'group.role_changed',
    category: 'group',
    severity: 'info',
    icon: 'Shield',
    title: 'msg.group.role_changed.title',
    body: 'msg.group.role_changed.body',
    autoHide: 4000,
    uiBehavior: 'toast',
  },

  'group.invite_copied': {
    id: 'group.invite_copied',
    category: 'group',
    severity: 'success',
    icon: 'Copy',
    title: 'msg.group.invite_copied.title',
    body: 'msg.group.invite_copied.body',
    autoHide: 2000,
    uiBehavior: 'toast',
  },

  'group.invite_invalid': {
    id: 'group.invite_invalid',
    category: 'group',
    severity: 'error',
    icon: 'XCircle',
    title: 'msg.group.invite_invalid.title',
    body: 'msg.group.invite_invalid.body',
    hint: 'msg.group.invite_invalid.hint',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  // ════════════════════════════════════════════════════════
  //  AUTH
  // ════════════════════════════════════════════════════════

  'auth.login_failed': {
    id: 'auth.login_failed',
    category: 'auth',
    severity: 'error',
    icon: 'ShieldX',
    title: 'msg.auth.login_failed.title',
    body: 'msg.auth.login_failed.body',
    hint: 'msg.auth.login_failed.hint',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'auth.register_failed': {
    id: 'auth.register_failed',
    category: 'auth',
    severity: 'error',
    icon: 'UserX',
    title: 'msg.auth.register_failed.title',
    body: 'msg.auth.register_failed.body',
    hint: 'msg.auth.register_failed.hint',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'auth.username_taken': {
    id: 'auth.username_taken',
    category: 'auth',
    severity: 'warning',
    icon: 'UserX',
    title: 'msg.auth.username_taken.title',
    body: 'msg.auth.username_taken.body',
    hint: 'msg.auth.username_taken.hint',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'auth.session_expired': {
    id: 'auth.session_expired',
    category: 'auth',
    severity: 'warning',
    icon: 'Clock',
    title: 'msg.auth.session_expired.title',
    body: 'msg.auth.session_expired.body',
    action: 'msg.auth.session_expired.action',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'auth.logged_out': {
    id: 'auth.logged_out',
    category: 'auth',
    severity: 'info',
    icon: 'LogOut',
    title: 'msg.auth.logged_out.title',
    body: 'msg.auth.logged_out.body',
    autoHide: 3000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  STARTUP
  // ════════════════════════════════════════════════════════

  'startup.backend_starting': {
    id: 'startup.backend_starting',
    category: 'startup',
    severity: 'info',
    icon: 'Loader2',
    title: 'msg.startup.backend_starting.title',
    body: 'msg.startup.backend_starting.body',
    autoHide: 0,
    uiBehavior: 'overlay',
  },

  'startup.discovering': {
    id: 'startup.discovering',
    category: 'startup',
    severity: 'info',
    icon: 'Search',
    title: 'msg.startup.discovering.title',
    body: 'msg.startup.discovering.body',
    autoHide: 0,
    uiBehavior: 'overlay',
  },

  'startup.restoring_session': {
    id: 'startup.restoring_session',
    category: 'startup',
    severity: 'info',
    icon: 'RotateCw',
    title: 'msg.startup.restoring_session.title',
    body: 'msg.startup.restoring_session.body',
    autoHide: 0,
    uiBehavior: 'overlay',
  },

  'startup.ready': {
    id: 'startup.ready',
    category: 'startup',
    severity: 'success',
    icon: 'CheckCircle2',
    title: 'msg.startup.ready.title',
    body: 'msg.startup.ready.body',
    autoHide: 2000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  FILE TRANSFER
  // ════════════════════════════════════════════════════════

  'file.upload_failed': {
    id: 'file.upload_failed',
    category: 'file',
    severity: 'error',
    icon: 'FileX',
    title: 'msg.file.upload_failed.title',
    body: 'msg.file.upload_failed.body',
    hint: 'msg.file.upload_failed.hint',
    action: 'msg.file.upload_failed.action',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  'file.too_large': {
    id: 'file.too_large',
    category: 'file',
    severity: 'warning',
    icon: 'FileWarning',
    title: 'msg.file.too_large.title',
    body: 'msg.file.too_large.body',
    hint: 'msg.file.too_large.hint',
    autoHide: 5000,
    uiBehavior: 'toast',
  },

  'file.upload_complete': {
    id: 'file.upload_complete',
    category: 'file',
    severity: 'success',
    icon: 'FileCheck',
    title: 'msg.file.upload_complete.title',
    body: 'msg.file.upload_complete.body',
    autoHide: 2000,
    uiBehavior: 'toast',
  },

  'file.download_failed': {
    id: 'file.download_failed',
    category: 'file',
    severity: 'error',
    icon: 'FileX',
    title: 'msg.file.download_failed.title',
    body: 'msg.file.download_failed.body',
    hint: 'msg.file.download_failed.hint',
    action: 'msg.file.download_failed.action',
    autoHide: 0,
    uiBehavior: 'inline',
  },

  // ════════════════════════════════════════════════════════
  //  PERMISSIONS
  // ════════════════════════════════════════════════════════

  'permission.not_allowed': {
    id: 'permission.not_allowed',
    category: 'permission',
    severity: 'warning',
    icon: 'ShieldAlert',
    title: 'msg.permission.not_allowed.title',
    body: 'msg.permission.not_allowed.body',
    autoHide: 4000,
    uiBehavior: 'toast',
  },

  'permission.owner_only': {
    id: 'permission.owner_only',
    category: 'permission',
    severity: 'info',
    icon: 'Crown',
    title: 'msg.permission.owner_only.title',
    body: 'msg.permission.owner_only.body',
    autoHide: 4000,
    uiBehavior: 'toast',
  },

  // ════════════════════════════════════════════════════════
  //  STATUS LABELS (for inline UI, not toast)
  // ════════════════════════════════════════════════════════

  'status.user_online': {
    id: 'status.user_online',
    category: 'status',
    severity: 'success',
    icon: 'Circle',
    title: 'msg.status.user_online.title',
    body: 'msg.status.user_online.body',
    autoHide: 0,
    uiBehavior: 'badge',
  },

  'status.user_offline': {
    id: 'status.user_offline',
    category: 'status',
    severity: 'neutral',
    icon: 'CircleDashed',
    title: 'msg.status.user_offline.title',
    body: 'msg.status.user_offline.body',
    autoHide: 0,
    uiBehavior: 'badge',
  },

  'status.user_away': {
    id: 'status.user_away',
    category: 'status',
    severity: 'info',
    icon: 'Moon',
    title: 'msg.status.user_away.title',
    body: 'msg.status.user_away.body',
    autoHide: 0,
    uiBehavior: 'badge',
  },

  'status.user_busy': {
    id: 'status.user_busy',
    category: 'status',
    severity: 'warning',
    icon: 'MinusCircle',
    title: 'msg.status.user_busy.title',
    body: 'msg.status.user_busy.body',
    autoHide: 0,
    uiBehavior: 'badge',
  },

  'status.user_in_call': {
    id: 'status.user_in_call',
    category: 'status',
    severity: 'info',
    icon: 'Phone',
    title: 'msg.status.user_in_call.title',
    body: 'msg.status.user_in_call.body',
    autoHide: 0,
    uiBehavior: 'badge',
  },

  // ════════════════════════════════════════════════════════
  //  CONFIRMATIONS (user-triggered actions)
  // ════════════════════════════════════════════════════════

  'confirm.leave_group': {
    id: 'confirm.leave_group',
    category: 'confirmation',
    severity: 'warning',
    icon: 'LogOut',
    title: 'msg.confirm.leave_group.title',
    body: 'msg.confirm.leave_group.body',
    action: 'msg.confirm.leave_group.action',
    secondaryAction: 'msg.confirm.leave_group.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'confirm.remove_member': {
    id: 'confirm.remove_member',
    category: 'confirmation',
    severity: 'warning',
    icon: 'UserX',
    title: 'msg.confirm.remove_member.title',
    body: 'msg.confirm.remove_member.body',
    action: 'msg.confirm.remove_member.action',
    secondaryAction: 'msg.confirm.remove_member.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'confirm.delete_message': {
    id: 'confirm.delete_message',
    category: 'confirmation',
    severity: 'warning',
    icon: 'Trash2',
    title: 'msg.confirm.delete_message.title',
    body: 'msg.confirm.delete_message.body',
    action: 'msg.confirm.delete_message.action',
    secondaryAction: 'msg.confirm.delete_message.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'confirm.end_call': {
    id: 'confirm.end_call',
    category: 'confirmation',
    severity: 'warning',
    icon: 'PhoneOff',
    title: 'msg.confirm.end_call.title',
    body: 'msg.confirm.end_call.body',
    action: 'msg.confirm.end_call.action',
    secondaryAction: 'msg.confirm.end_call.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'confirm.logout': {
    id: 'confirm.logout',
    category: 'confirmation',
    severity: 'warning',
    icon: 'LogOut',
    title: 'msg.confirm.logout.title',
    body: 'msg.confirm.logout.body',
    action: 'msg.confirm.logout.action',
    secondaryAction: 'msg.confirm.logout.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },

  'confirm.leave_call_for_new': {
    id: 'confirm.leave_call_for_new',
    category: 'confirmation',
    severity: 'warning',
    icon: 'PhoneForwarded',
    title: 'msg.confirm.leave_call_for_new.title',
    body: 'msg.confirm.leave_call_for_new.body',
    action: 'msg.confirm.leave_call_for_new.action',
    secondaryAction: 'msg.confirm.leave_call_for_new.cancel',
    autoHide: 0,
    uiBehavior: 'modal',
  },
};

// ── Public API ──────────────────────────────────────────

/**
 * Get the raw message definition (with i18n keys, not translated).
 */
export function getMessageDef(id: string): AppMessage | undefined {
  return catalog[id];
}

/**
 * Get a resolved message with all strings translated via the current language.
 * Returns undefined if the message ID is not found.
 */
export function getMessage(id: string): ResolvedMessage | undefined {
  const def = catalog[id];
  if (!def) return undefined;

  return {
    id: def.id,
    category: def.category,
    severity: def.severity,
    icon: def.icon,
    title: t(def.title),
    body: t(def.body),
    hint: def.hint ? t(def.hint) : undefined,
    action: def.action ? t(def.action) : undefined,
    secondaryAction: def.secondaryAction ? t(def.secondaryAction) : undefined,
    autoHide: def.autoHide,
    uiBehavior: def.uiBehavior,
  };
}

/**
 * Get all messages for a given category (already translated).
 */
export function getMessagesByCategory(category: MessageCategory): ResolvedMessage[] {
  return Object.values(catalog)
    .filter(m => m.category === category)
    .map(m => getMessage(m.id)!);
}

/**
 * Get all messages for a given severity (already translated).
 */
export function getMessagesBySeverity(severity: MessageSeverity): ResolvedMessage[] {
  return Object.values(catalog)
    .filter(m => m.severity === severity)
    .map(m => getMessage(m.id)!);
}

/**
 * Get all message IDs in the catalog.
 */
export function getAllMessageIds(): string[] {
  return Object.keys(catalog);
}

/**
 * Utility: get the Tailwind color class for a given severity.
 */
export function severityColor(severity: MessageSeverity): string {
  switch (severity) {
    case 'info':    return 'text-blue-400';
    case 'success': return 'text-green-400';
    case 'warning': return 'text-yellow-400';
    case 'error':   return 'text-red-400';
    case 'neutral': return 'text-gray-400';
  }
}

/**
 * Utility: get the Tailwind background class for a given severity.
 */
export function severityBg(severity: MessageSeverity): string {
  switch (severity) {
    case 'info':    return 'bg-blue-600/10';
    case 'success': return 'bg-green-600/10';
    case 'warning': return 'bg-yellow-600/10';
    case 'error':   return 'bg-red-600/10';
    case 'neutral': return 'bg-surface-800/50';
  }
}

/**
 * Utility: get the Tailwind border class for a given severity.
 */
export function severityBorder(severity: MessageSeverity): string {
  switch (severity) {
    case 'info':    return 'border-blue-600/20';
    case 'success': return 'border-green-600/20';
    case 'warning': return 'border-yellow-600/20';
    case 'error':   return 'border-red-600/20';
    case 'neutral': return 'border-surface-700';
  }
}

// Default export for convenience
export default {
  getMessage,
  getMessageDef,
  getMessagesByCategory,
  getMessagesBySeverity,
  getAllMessageIds,
  severityColor,
  severityBg,
  severityBorder,
};
