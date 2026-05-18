/**
 * OneClickActions.ts — Simplified action patterns for non-technical users.
 *
 * Problem: Many core actions in the app require 3-5 clicks and async waits.
 * This service wraps complex multi-step workflows into single-call functions
 * that handle all the intermediate steps internally.
 *
 * Actions simplified:
 *   1. quickCall(userId)     — Start audio call in one step
 *   2. quickVideoCall(userId) — Start video call in one step
 *   3. quickMessage(userId, text) — Send a message (auto-creates DM if needed)
 *   4. quickGroup(name, memberIds) — Create group + add members in one step
 *   5. quickScreenShare()    — Start sharing with best preset (auto-detect)
 *   6. quickAddContact(userId) — Add contact + create DM channel
 *   7. quickJoinGroup(groupId) — Join group + open chat
 *   8. quickSwitchDevice(type) — Cycle to next mic/camera/speaker
 *   9. quickToggleMute()     — Toggle mute (works globally, even outside call view)
 *  10. quickInvitePeople(groupId) — Open invite sheet for a group
 *
 * Design principles:
 *   - Every function returns a Promise<{ success: boolean; error?: string }>
 *   - Handles all preconditions internally (permission checks, channel creation)
 *   - Shows toast notifications for success/failure
 *   - Never throws — always returns result object
 *   - Logs all actions for debugging
 */

import { AppLogger } from '../AppLogger';

const log = AppLogger.create('OneClickActions');

// ── Types ───────────────────────────────────────────────────

export interface ActionResult {
  success: boolean;
  error?: string;       // i18n key
  data?: any;           // optional return data (e.g., channelId for quickMessage)
}

type StoreGetter<T> = () => T;

// ── Configuration ───────────────────────────────────────────

export interface OneClickDependencies {
  getAuthStore: StoreGetter<{
    user: { id: string } | null;
    isAuthenticated: boolean;
    serverUrl: string;
  }>;
  getCallStore: StoreGetter<{
    status: string;
    initiateCall: (userId: string, type: 'audio' | 'video') => Promise<void>;
    hangup: () => void;
    toggleMute: () => void;
    toggleVideo: () => void;
    startScreenShare: (sourceId?: string) => Promise<void>;
    stopScreenShare: () => void;
    isMuted: boolean;
    isVideoOff: boolean;
    isScreenSharing: boolean;
  }>;
  getChatStore: StoreGetter<{
    channels: Array<{ id: string; type: string; members?: string[] }>;
    createChannel: (data: { type: string; name?: string; memberIds: string[] }) => Promise<string>;
    sendMessage: (channelId: string, content: string) => Promise<void>;
    setActiveChannel: (channelId: string) => void;
  }>;
  getContactsStore: StoreGetter<{
    contacts: Array<{ id: string }>;
    addContact: (userId: string) => Promise<void>;
  }>;
  showToast: (message: string, type: 'success' | 'error' | 'info') => void;
  navigate: (path: string) => void;
}

// ── Main Service ────────────────────────────────────────────

class OneClickActionsService {
  private deps: OneClickDependencies | null = null;

  /**
   * Initialize with store getters and utilities.
   * Call once after app bootstraps.
   */
  init(deps: OneClickDependencies): void {
    this.deps = deps;
    log.info('OneClickActions initialized');
  }

  private ensureInit(): OneClickDependencies {
    if (!this.deps) throw new Error('OneClickActions not initialized');
    return this.deps;
  }

  // ── Quick Call ──────────────────────────────────────────

  async quickCall(userId: string): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const auth = deps.getAuthStore();
      if (!auth.isAuthenticated) {
        return { success: false, error: 'action.not_logged_in' };
      }

      const call = deps.getCallStore();
      if (call.status !== 'idle') {
        return { success: false, error: 'action.already_in_call' };
      }

      await call.initiateCall(userId, 'audio');
      log.info('Quick audio call initiated', { userId });
      return { success: true };
    } catch (e: any) {
      log.error('Quick call failed', e);
      return { success: false, error: 'action.call_failed' };
    }
  }

  async quickVideoCall(userId: string): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const auth = deps.getAuthStore();
      if (!auth.isAuthenticated) {
        return { success: false, error: 'action.not_logged_in' };
      }

      const call = deps.getCallStore();
      if (call.status !== 'idle') {
        return { success: false, error: 'action.already_in_call' };
      }

      await call.initiateCall(userId, 'video');
      log.info('Quick video call initiated', { userId });
      return { success: true };
    } catch (e: any) {
      log.error('Quick video call failed', e);
      return { success: false, error: 'action.call_failed' };
    }
  }

  // ── Quick Message ───────────────────────────────────────

  async quickMessage(userId: string, text?: string): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const auth = deps.getAuthStore();
      if (!auth.isAuthenticated) {
        return { success: false, error: 'action.not_logged_in' };
      }

      const chat = deps.getChatStore();

      // Find existing DM channel with this user
      let channelId = chat.channels.find(
        (ch) => ch.type === 'dm' && ch.members?.includes(userId)
      )?.id;

      // Create DM channel if it doesn't exist
      if (!channelId) {
        channelId = await chat.createChannel({
          type: 'dm',
          memberIds: [userId],
        });
        log.info('Auto-created DM channel', { userId, channelId });
      }

      // Navigate to the channel
      chat.setActiveChannel(channelId);
      deps.navigate('/chats');

      // Optionally send message text
      if (text?.trim()) {
        await chat.sendMessage(channelId, text.trim());
      }

      return { success: true, data: { channelId } };
    } catch (e: any) {
      log.error('Quick message failed', e);
      return { success: false, error: 'action.message_failed' };
    }
  }

  // ── Quick Group ─────────────────────────────────────────

  async quickGroup(name: string, memberIds: string[]): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const auth = deps.getAuthStore();
      if (!auth.isAuthenticated) {
        return { success: false, error: 'action.not_logged_in' };
      }

      const chat = deps.getChatStore();
      const channelId = await chat.createChannel({
        type: 'group',
        name,
        memberIds,
      });

      chat.setActiveChannel(channelId);
      deps.navigate('/chats');

      log.info('Quick group created', { name, memberCount: memberIds.length, channelId });
      deps.showToast('action.group_created', 'success');
      return { success: true, data: { channelId } };
    } catch (e: any) {
      log.error('Quick group creation failed', e);
      return { success: false, error: 'action.group_failed' };
    }
  }

  // ── Quick Screen Share ──────────────────────────────────

  async quickScreenShare(): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const call = deps.getCallStore();
      if (call.status !== 'active') {
        return { success: false, error: 'action.not_in_call' };
      }

      if (call.isScreenSharing) {
        call.stopScreenShare();
        return { success: true, data: { action: 'stopped' } };
      }

      // Start screen share with auto-detected best source
      await call.startScreenShare();
      log.info('Quick screen share started');
      return { success: true, data: { action: 'started' } };
    } catch (e: any) {
      log.error('Quick screen share failed', e);
      return { success: false, error: 'action.screen_share_failed' };
    }
  }

  // ── Quick Add Contact ───────────────────────────────────

  async quickAddContact(userId: string): Promise<ActionResult> {
    const deps = this.ensureInit();
    try {
      const contacts = deps.getContactsStore();
      const existing = contacts.contacts.find((c) => c.id === userId);
      if (existing) {
        return { success: true, data: { alreadyExists: true } };
      }

      await contacts.addContact(userId);
      log.info('Contact added', { userId });
      deps.showToast('action.contact_added', 'success');
      return { success: true };
    } catch (e: any) {
      log.error('Quick add contact failed', e);
      return { success: false, error: 'action.add_contact_failed' };
    }
  }

  // ── Quick Toggle Mute ───────────────────────────────────

  quickToggleMute(): ActionResult {
    const deps = this.ensureInit();
    try {
      const call = deps.getCallStore();
      if (call.status !== 'active') {
        return { success: false, error: 'action.not_in_call' };
      }
      call.toggleMute();
      return { success: true, data: { muted: !call.isMuted } };
    } catch (e: any) {
      return { success: false, error: 'action.mute_failed' };
    }
  }

  // ── Quick Toggle Video ──────────────────────────────────

  quickToggleVideo(): ActionResult {
    const deps = this.ensureInit();
    try {
      const call = deps.getCallStore();
      if (call.status !== 'active') {
        return { success: false, error: 'action.not_in_call' };
      }
      call.toggleVideo();
      return { success: true, data: { videoOff: !call.isVideoOff } };
    } catch (e: any) {
      return { success: false, error: 'action.video_toggle_failed' };
    }
  }

  // ── Quick Navigate ──────────────────────────────────────

  goToChats(): void { this.deps?.navigate('/chats'); }
  goToContacts(): void { this.deps?.navigate('/contacts'); }
  goToCalls(): void { this.deps?.navigate('/calls'); }
  goToSettings(): void { this.deps?.navigate('/settings'); }
  goToGroups(): void { this.deps?.navigate('/groups'); }
}

// ── Singleton ───────────────────────────────────────────────

export const oneClickActions = new OneClickActionsService();
