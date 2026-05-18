/**
 * SyncManager — reconnection synchronization orchestrator.
 *
 * When the socket connection drops and reconnects, this manager:
 *   1. Requests all missed messages since the last known timestamp
 *   2. Merges them into the local message store (deduplication)
 *   3. Updates unread counts and channel summaries
 *   4. Marks missed messages as delivered
 *   5. Flushes any queued outbound messages
 *
 * Also handles initial load (first connection) by fetching
 * unread counts and channel summaries.
 */

import { socketManager } from '../socket.manager';

export interface SyncedMessage {
  id: string;
  channel_id: string;
  sender: {
    id: string;
    username: string;
    display_name: string;
    avatar_url: string | null;
  } | null;
  content: string;
  type: string;
  reply_to: string | null;
  file_id: string | null;
  status: string;
  edited_at: string | null;
  created_at: string | null;
}

export interface ChannelUnreadInfo {
  unread: number;
  last_message: {
    id: string;
    sender_id: string;
    sender_name: string;
    content: string;
    type: string;
    created_at: string | null;
  } | null;
}

export interface SyncResult {
  /** Missed messages grouped by channel_id */
  channels: Record<string, SyncedMessage[]>;
  /** Unread counts per channel */
  unread: Record<string, ChannelUnreadInfo>;
  /** Server timestamp of sync completion */
  syncedAt: string;
  /** Total messages synced */
  totalMessages: number;
}

export interface ChannelSummary {
  channel_id: string;
  unread: number;
  last_message: {
    id: string;
    sender_id: string;
    sender_name: string;
    content: string;
    type: string;
    created_at: string | null;
  } | null;
}

export interface ChannelReadState {
  userId: string;
  username: string;
  displayName: string;
  lastReadAt: string | null;
  unreadCount: number;
}

export interface SyncCallbacks {
  /** Sync completed with results */
  onSyncComplete: (result: SyncResult) => void;
  /** Unread counts updated */
  onUnreadUpdate: (unread: Record<string, ChannelUnreadInfo>) => void;
  /** Channel summaries updated */
  onSummariesUpdate: (summaries: ChannelSummary[]) => void;
  /** Sync error */
  onSyncError: (error: string) => void;
}

export class SyncManager {
  private callbacks: SyncCallbacks;
  private _lastSyncTimestamp: string | null = null;
  private _syncing = false;
  private _syncInProgress: Promise<any> | null = null;
  private _socketUnsubs: Array<() => void> = [];
  private _destroyed = false;
  private _periodicSyncTimer: NodeJS.Timeout | null = null;
  private _periodicSyncInterval: number = 30000; // 30 seconds default

  constructor(callbacks: SyncCallbacks) {
    this.callbacks = callbacks;
  }

  get lastSyncTimestamp(): string | null {
    return this._lastSyncTimestamp;
  }

  get isSyncing(): boolean {
    return this._syncing;
  }

  /**
   * Initialize — register socket reconnect listener and restore state.
   */
  init(): void {
    // Restore last sync timestamp from localStorage
    this.restoreSyncTimestamp();

    // Listen for socket reconnection to trigger auto-sync
    this._socketUnsubs.push(
      socketManager.on('connect', () => {
        if (this._lastSyncTimestamp) {
          // Reconnection — sync missed messages
          this.syncMissedMessages();
        } else {
          // First connection — just fetch unread counts
          this.fetchUnreadCounts();
        }
      })
    );
  }

  /**
   * Update the last sync timestamp.
   * Should be called whenever a new message is received or sent.
   */
  updateTimestamp(isoTimestamp: string): void {
    if (
      !this._lastSyncTimestamp ||
      isoTimestamp > this._lastSyncTimestamp
    ) {
      this._lastSyncTimestamp = isoTimestamp;
    }
  }

  /**
   * Request missed messages since last sync.
   */
  async syncMissedMessages(): Promise<SyncResult | null> {
    if (this._syncing || this._destroyed) return null;
    if (!this._lastSyncTimestamp) {
      // No previous timestamp — just fetch unread
      await this.fetchUnreadCounts();
      return null;
    }

    this._syncing = true;
    const syncPromise = (async () => {
      try {
        // Audit fix: `since` was sent as ISO-string here but as Unix-ms
        // in syncWithDeliveryConfirmation below — server got confused
        // and either fetched everything or nothing. Both call sites now
        // send Unix-ms (number). _lastSyncTimestamp stays as ISO for
        // human-friendly persistence; we convert at the wire boundary.
        const response = await socketManager.emit('sync_request', {
          since: this._lastSyncTimestamp
            ? new Date(this._lastSyncTimestamp).getTime()
            : 0,
          limit: 500,
        });

        if (response?.error) {
          this.callbacks.onSyncError(response.error);
          return null;
        }

        const result: SyncResult = {
          channels: response.channels || {},
          unread: response.unread || {},
          syncedAt: response.synced_at || new Date().toISOString(),
          totalMessages: 0,
        };

        // Count total
        for (const msgs of Object.values(result.channels)) {
          result.totalMessages += (msgs as SyncedMessage[]).length;
        }

        // Update last sync timestamp
        this._lastSyncTimestamp = result.syncedAt;
        this.persistSyncTimestamp();

        // Notify callbacks
        this.callbacks.onSyncComplete(result);
        this.callbacks.onUnreadUpdate(result.unread);

        console.log(
          `[SyncManager] Synced ${result.totalMessages} messages across ${Object.keys(result.channels).length} channels`
        );

        return result;
      } catch (e: any) {
        console.error('[SyncManager] Sync failed:', e);
        this.callbacks.onSyncError(e.message || 'Sync failed');
        return null;
      } finally {
        this._syncing = false;
        this._syncInProgress = null;
      }
    })();

    this._syncInProgress = syncPromise;
    return syncPromise;
  }

  /**
   * Fetch current unread counts (no message sync).
   */
  async fetchUnreadCounts(): Promise<void> {
    if (this._destroyed) return;

    try {
      const response = await socketManager.emit('sync_unread_counts', {});
      if (response?.unread) {
        this.callbacks.onUnreadUpdate(response.unread);
      }
    } catch (e: any) {
      console.error('[SyncManager] Unread fetch failed:', e);
    }
  }

  /**
   * Fetch channel list summaries.
   */
  async fetchChannelSummaries(): Promise<ChannelSummary[]> {
    if (this._destroyed) return [];

    try {
      const response = await socketManager.emit('sync_channel_summaries', {});
      const summaries = response?.summaries || [];
      this.callbacks.onSummariesUpdate(summaries);
      return summaries;
    } catch (e: any) {
      console.error('[SyncManager] Summaries fetch failed:', e);
      return [];
    }
  }

  /**
   * Force a full resync (used when user manually triggers refresh).
   *
   * Previously the epoch reset was guarded by `if (!_lastSyncTimestamp)`
   * which made it a no-op for the only case anyone would call it (you
   * already have a timestamp; you want to force-redownload). Now we
   * always reset before sync; persistence happens after the sync
   * completes so the cursor still advances correctly.
   */
  async forceResync(): Promise<SyncResult | null> {
    this._lastSyncTimestamp = new Date(0).toISOString();
    return this.syncMissedMessages();
  }

  /**
   * Sync missed messages and atomically confirm delivery.
   */
  async syncWithDeliveryConfirmation(since?: number): Promise<SyncResult | null> {
    if (this._syncing || this._destroyed) return null;

    this._syncing = true;

    try {
      const response = await socketManager.emit('v2_chat_sync', {
        since: since || (this._lastSyncTimestamp ? new Date(this._lastSyncTimestamp).getTime() : 0),
        limit: 500,
      });

      if (response?.error) {
        this.callbacks.onSyncError(response.error);
        return null;
      }

      const result: SyncResult = {
        channels: response.channels || {},
        unread: response.unread || {},
        syncedAt: response.synced_at || new Date().toISOString(),
        totalMessages: 0,
      };

      // Count total
      for (const msgs of Object.values(result.channels)) {
        result.totalMessages += (msgs as SyncedMessage[]).length;
      }

      // Update last sync timestamp and persist it so a desktop crash
      // doesn't trigger a full re-sync on next launch. The other code
      // path (`syncMissedMessages`) already calls persistSyncTimestamp;
      // this branch was missing the persist call so an app shutdown
      // immediately after v2_chat_sync would re-fetch every message.
      this._lastSyncTimestamp = result.syncedAt;
      this.persistSyncTimestamp();

      // Notify callbacks
      this.callbacks.onSyncComplete(result);
      this.callbacks.onUnreadUpdate(result.unread);

      console.log(
        `[SyncManager] Synced ${result.totalMessages} messages with delivery confirmation`
      );

      return result;
    } catch (e: any) {
      console.error('[SyncManager] Sync with delivery confirmation failed:', e);
      this.callbacks.onSyncError(e.message || 'Sync failed');
      return null;
    } finally {
      this._syncing = false;
    }
  }

  /**
   * Fetch read states for a channel (who read messages and when).
   */
  async fetchChannelReadStates(channelId: string): Promise<ChannelReadState[]> {
    if (this._destroyed) return [];

    try {
      const response = await socketManager.emit('v2_chat_get_channel_read_states', {
        channel_id: channelId,
      });
      return response?.read_states || [];
    } catch (e: any) {
      console.error('[SyncManager] Failed to fetch read states:', e);
      return [];
    }
  }

  /**
   * Start a background periodic sync at the specified interval.
   */
  startPeriodicSync(intervalMs?: number): void {
    if (this._periodicSyncTimer) {
      console.warn('[SyncManager] Periodic sync already running');
      return;
    }

    if (intervalMs && intervalMs > 0) {
      this._periodicSyncInterval = intervalMs;
    }

    this._periodicSyncTimer = setInterval(() => {
      if (this._syncing || this._destroyed || !socketManager.isConnected()) {
        return; // Already syncing, destroyed, or not connected
      }
      this.syncMissedMessages().catch((e) => {
        console.error('[SyncManager] Periodic sync error:', e);
      });
    }, this._periodicSyncInterval);

    console.log(
      `[SyncManager] Periodic sync started with interval ${this._periodicSyncInterval}ms`
    );
  }

  /**
   * Stop the background periodic sync.
   */
  stopPeriodicSync(): void {
    if (this._periodicSyncTimer) {
      clearInterval(this._periodicSyncTimer);
      this._periodicSyncTimer = null;
      console.log('[SyncManager] Periodic sync stopped');
    }
  }

  /**
   * Persist the last sync timestamp to localStorage to survive app crashes.
   */
  private persistSyncTimestamp(): void {
    if (this._lastSyncTimestamp) {
      try {
        localStorage.setItem('syncManager_lastSyncTimestamp', this._lastSyncTimestamp);
      } catch (e) {
        console.warn('[SyncManager] Failed to persist sync timestamp:', e);
      }
    }
  }

  /**
   * Restore the last sync timestamp from localStorage on initialization.
   */
  private restoreSyncTimestamp(): void {
    try {
      const stored = localStorage.getItem('syncManager_lastSyncTimestamp');
      if (stored) {
        this._lastSyncTimestamp = stored;
        console.log('[SyncManager] Restored sync timestamp from storage:', stored);
      }
    } catch (e) {
      console.warn('[SyncManager] Failed to restore sync timestamp:', e);
    }
  }

  destroy(): void {
    if (this._destroyed) return;
    this._destroyed = true;

    this.stopPeriodicSync();

    for (const unsub of this._socketUnsubs) {
      unsub();
    }
    this._socketUnsubs = [];
  }
}
