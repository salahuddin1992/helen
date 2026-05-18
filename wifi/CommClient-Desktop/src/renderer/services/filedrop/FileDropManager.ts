/**
 * FileDropManager — Manage file offers/accepts, coordinate uploads,
 * handle incoming offers via socket events.
 *
 * Upload backend selection
 * ------------------------
 * Two uploaders are supported:
 *   • ChunkedUploader   — legacy, uses `/api/files/upload`, no resume.
 *   • ResumableUploader — new `/api/files/resumable/*` protocol with CRC32
 *     + SHA-256 per chunk, IndexedDB-backed resume, parallel chunks.
 *
 * The ResumableUploader is the default. Callers or tests can force the
 * legacy path via either:
 *   - the `useResumable: false` constructor option, or
 *   - setting `localStorage.setItem('COMMCLIENT_USE_RESUMABLE', '0')`
 * (the localStorage flag is checked at construction time so a user can
 * pin the uploader backend without recompiling).
 */
import { ChunkedUploader } from './ChunkedUploader';
import { resumableUploader, type ResumableUploader } from './ResumableUploader';
import {
  groupFileMulticastManager,
  type GroupFileOfferPayload,
} from './GroupFileMulticastManager';
import { socketManager } from '../socket.manager';
import { getBaseUrl } from '../api.client';
import { useAuthStore } from '../../stores/auth.store';

export interface FileOffer {
  id: string;
  senderId: string;
  senderName: string;
  fileName: string;
  fileSize: number;
  fileType: string;
  fileId?: string;
  timestamp: number;
}

export interface ActiveTransfer {
  id: string;
  fileName: string;
  fileSize: number;
  uploadedBytes: number;
  speed: number;
  direction: 'upload' | 'download';
  status: 'uploading' | 'downloading' | 'paused' | 'completed' | 'error';
  error?: string;
}

export interface FileDropManagerOptions {
  /** Force the legacy ChunkedUploader path. Default: auto (prefer resumable). */
  useResumable?: boolean;
  /** Per-chunk concurrency for the resumable uploader. */
  concurrency?: number;
  /** Per-chunk size override (bytes) for the resumable uploader. */
  chunkSize?: number;
  /** Minimum channel size to auto-trigger group multicast instead of per-recipient unicast. */
  groupMulticastThreshold?: number;
}

export interface SendGroupFileOptions {
  /** Channel members participating in the multicast swarm. */
  memberCount: number;
  /** Optional caption attached to the offer. */
  caption?: string;
  /** Enable BitTorrent-style swarm (default true). If false, peers fall back
   * to server-only chunks. */
  swarmEnabled?: boolean;
  /** Offer TTL in seconds. Server caps at MAX_OFFER_TTL (7 days). */
  expiresInSec?: number;
  /** Optional file-level checksum (hex sha256). */
  checksum?: string;
}

export class FileDropManager {
  private uploader: ChunkedUploader;
  private resumable: ResumableUploader;
  private useResumable: boolean;
  private resumableConcurrency: number;
  private resumableChunkSize: number | undefined;
  private groupMulticastThreshold: number;
  private offers: Map<string, FileOffer> = new Map();
  private activeTransfers: Map<string, ActiveTransfer> = new Map();
  private uploadUrl: string = '';
  /** Maps transferKey -> uploader's internal uploadId for cancel support */
  private transferToUploadId: Map<string, string> = new Map();
  /** Maps transferKey -> resumable session_id for pause/resume/abort on new path */
  private transferToSession: Map<string, string> = new Map();

  constructor(uploadUrl: string, opts: FileDropManagerOptions = {}) {
    this.uploader = new ChunkedUploader();
    this.resumable = resumableUploader;
    this.uploadUrl = uploadUrl;

    // Resolve the uploader-backend feature flag: explicit option > localStorage
    // override > default (true). The localStorage escape hatch lets operators
    // downgrade to the legacy code path without a rebuild.
    const lsFlag =
      typeof localStorage !== 'undefined'
        ? localStorage.getItem('COMMCLIENT_USE_RESUMABLE')
        : null;
    const lsForcedOff = lsFlag === '0' || lsFlag === 'false';
    const lsForcedOn = lsFlag === '1' || lsFlag === 'true';
    this.useResumable =
      opts.useResumable ?? (lsForcedOff ? false : lsForcedOn ? true : true);

    this.resumableConcurrency = Math.max(1, Math.min(opts.concurrency ?? 4, 16));
    this.resumableChunkSize = opts.chunkSize;
    this.groupMulticastThreshold = Math.max(
      2,
      opts.groupMulticastThreshold ?? 3,
    );

    this.setupSocketListeners();
  }

  /**
   * Send file to user(s).
   * Uploads the file first, then emits the offer with the file_id.
   */
  async sendFile(
    file: File,
    recipientIds: string[],
    channelId: string
  ): Promise<void> {
    const transferId = `transfer-${Date.now()}`;

    for (const recipientId of recipientIds) {
      const transferKey = `${transferId}-${recipientId}`;

      this.activeTransfers.set(transferKey, {
        id: transferKey,
        fileName: file.name,
        fileSize: file.size,
        uploadedBytes: 0,
        speed: 0,
        direction: 'upload',
        status: 'uploading',
      });

      // Start upload FIRST, then emit offer after completion
      try {
        let fileId: string;

        if (this.useResumable) {
          fileId = await this.resumable.uploadFile(
            file,
            {
              onSessionCreated: (session_id) => {
                this.transferToSession.set(transferKey, session_id);
              },
              onResumed: (session_id) => {
                this.transferToSession.set(transferKey, session_id);
              },
              onProgress: (p) => {
                const transfer = this.activeTransfers.get(transferKey);
                if (transfer) {
                  transfer.uploadedBytes = p.uploaded;
                  transfer.speed = p.speedBps;
                }
              },
              onComplete: (_fid) => {
                const transfer = this.activeTransfers.get(transferKey);
                if (transfer) {
                  transfer.status = 'completed';
                  transfer.uploadedBytes = transfer.fileSize;
                }
              },
              onError: (message) => {
                const transfer = this.activeTransfers.get(transferKey);
                if (transfer) {
                  transfer.status = 'error';
                  transfer.error = message;
                }
              },
            },
            {
              channelId,
              concurrency: this.resumableConcurrency,
              chunkSize: this.resumableChunkSize,
            },
          );
        } else {
          fileId = await this.uploader.uploadFile(file, this.uploadUrl, {
            onProgress: (uploaded, total, speed) => {
              const transfer = this.activeTransfers.get(transferKey);
              if (transfer) {
                transfer.uploadedBytes = uploaded;
                transfer.speed = speed;
              }
            },
            onComplete: (completedFileId) => {
              const transfer = this.activeTransfers.get(transferKey);
              if (transfer) {
                transfer.status = 'completed';
                transfer.uploadedBytes = transfer.fileSize;
              }
            },
            onError: (error) => {
              const transfer = this.activeTransfers.get(transferKey);
              if (transfer) {
                transfer.status = 'error';
                transfer.error = error;
              }
            },
          }, channelId);

          // Track the uploader's internal ID for cancel support
          const activeUploadId = this.uploader.getActiveUploadId();
          if (activeUploadId) {
            this.transferToUploadId.set(transferKey, activeUploadId);
          }
        }

        // Upload succeeded — NOW emit the offer with the file_id
        socketManager.emit('filedrop:offer', {
          recipient_id: recipientId,
          channel_id: channelId,
          file_name: file.name,
          file_size: file.size,
          file_type: file.type,
          file_id: fileId,
        });

        // Send file via message
        socketManager.emit('message:send', {
          channel_id: channelId,
          content: file.name,
          type: 'file',
          file_id: fileId,
          recipient_id: recipientId,
        });
      } catch (error) {
        const transfer = this.activeTransfers.get(transferKey);
        if (transfer) {
          transfer.status = 'error';
          transfer.error = error instanceof Error ? error.message : 'Unknown error';
        }
      }
    }
  }

  /**
   * Send a file to every member of a group channel via the multicast
   * swarm protocol. One resumable upload, one group offer row, per-member
   * availability tracked server-side.
   *
   * Returns the offer id for UI correlation. Use
   * :class:`GroupFileMulticastManager` (singleton
   * ``groupFileMulticastManager``) to observe fan-out state, accept/reject
   * on behalf of recipients, and monitor per-peer chunk availability.
   */
  async sendGroupFile(
    file: File,
    channelId: string,
    opts: SendGroupFileOptions,
  ): Promise<GroupFileOfferPayload> {
    const transferKey = `groupfile-${Date.now()}-${channelId}`;
    this.activeTransfers.set(transferKey, {
      id: transferKey,
      fileName: file.name,
      fileSize: file.size,
      uploadedBytes: 0,
      speed: 0,
      direction: 'upload',
      status: 'uploading',
    });

    // 1. Upload via resumable path — always, even if the legacy flag is
    //    set: the multicast protocol requires a stable server-backed URL.
    const chunkSize =
      this.resumableChunkSize ?? 256 * 1024; // 256 KiB default
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));

    let fileId: string;
    try {
      fileId = await this.resumable.uploadFile(
        file,
        {
          onSessionCreated: (session_id) => {
            this.transferToSession.set(transferKey, session_id);
          },
          onResumed: (session_id) => {
            this.transferToSession.set(transferKey, session_id);
          },
          onProgress: (p) => {
            const t = this.activeTransfers.get(transferKey);
            if (t) {
              t.uploadedBytes = p.uploaded;
              t.speed = p.speedBps;
            }
          },
          onComplete: () => {
            const t = this.activeTransfers.get(transferKey);
            if (t) {
              t.status = 'completed';
              t.uploadedBytes = t.fileSize;
            }
          },
          onError: (message) => {
            const t = this.activeTransfers.get(transferKey);
            if (t) {
              t.status = 'error';
              t.error = message;
            }
          },
        },
        {
          channelId,
          concurrency: this.resumableConcurrency,
          chunkSize,
        },
      );
    } catch (e) {
      const t = this.activeTransfers.get(transferKey);
      if (t) {
        t.status = 'error';
        t.error = e instanceof Error ? e.message : 'upload failed';
      }
      throw e;
    }

    // 2. Announce the multicast offer via REST (socket fan-out happens
    //    server-side to all channel members, including the sender).
    const offer = await groupFileMulticastManager.createOffer(
      channelId,
      fileId,
      {
        chunkSize,
        totalChunks,
        caption: opts.caption,
        swarmEnabled: opts.swarmEnabled ?? true,
        expiresInSec: opts.expiresInSec,
        checksum: opts.checksum,
      },
    );

    return offer;
  }

  /**
   * Decide between unicast (small groups / DMs) and group multicast for a
   * given channel. Callers that don't want to branch themselves can use
   * this instead of ``sendFile`` / ``sendGroupFile`` directly.
   */
  async sendFileSmart(
    file: File,
    channelId: string,
    recipientIds: string[],
  ): Promise<void | GroupFileOfferPayload> {
    const recipients = recipientIds.length;
    if (recipients >= this.groupMulticastThreshold) {
      return this.sendGroupFile(file, channelId, {
        memberCount: recipients + 1,
      });
    }
    return this.sendFile(file, recipientIds, channelId);
  }

  /**
   * Accept incoming file offer and download the file
   */
  async acceptOffer(offerId: string, downloadPath?: string): Promise<void> {
    const offer = this.offers.get(offerId);
    if (!offer) return;

    const transferKey = `download-${offerId}`;

    this.activeTransfers.set(transferKey, {
      id: transferKey,
      fileName: offer.fileName,
      fileSize: offer.fileSize,
      uploadedBytes: 0,
      speed: 0,
      direction: 'download',
      status: 'downloading',
    });

    // Emit acceptance via socket
    socketManager.emit('filedrop:accept', {
      offer_id: offerId,
      download_path: downloadPath,
    });

    // Actually download the file
    if (offer.fileId) {
      try {
        const baseUrl = getBaseUrl();
        const tokens = useAuthStore.getState().tokens;
        const headers: Record<string, string> = {};
        if (tokens?.access_token) {
          headers['Authorization'] = `Bearer ${tokens.access_token}`;
        }

        const startTime = Date.now();
        const response = await fetch(`${baseUrl}/api/files/${offer.fileId}`, {
          method: 'GET',
          headers,
        });

        if (!response.ok) {
          throw new Error(`Download failed: ${response.status} ${response.statusText}`);
        }

        // Read the response as a blob for download
        const contentLength = Number(response.headers.get('content-length')) || offer.fileSize;
        const reader = response.body?.getReader();

        if (reader) {
          const chunks: BlobPart[] = [];
          let downloadedBytes = 0;

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            chunks.push(value);
            downloadedBytes += value.length;

            const elapsed = (Date.now() - startTime) / 1000 || 1;
            const speed = downloadedBytes / elapsed;

            const transfer = this.activeTransfers.get(transferKey);
            if (transfer) {
              transfer.uploadedBytes = downloadedBytes;
              transfer.speed = speed;
            }
          }

          // Create blob from chunks and trigger browser download
          const blob = new Blob(chunks);
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          // Audit fix F4: sanitize filename to defeat path-traversal
          // (some browsers/extensions honor `../` in download="...").
          // Strip directory components, NULL bytes, and Windows-illegal
          // chars. Cap to 255 chars to satisfy NTFS/ext4.
          a.download = (offer.fileName || 'download')
            .replace(/[\\\/:\x00<>:"|?*]/g, '_')
            .replace(/\.+/g, '.')
            .slice(0, 255) || 'download';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        } else {
          // Fallback: direct blob download
          const blob = await response.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          // Audit fix F4: sanitize filename to defeat path-traversal
          // (some browsers/extensions honor `../` in download="...").
          // Strip directory components, NULL bytes, and Windows-illegal
          // chars. Cap to 255 chars to satisfy NTFS/ext4.
          a.download = (offer.fileName || 'download')
            .replace(/[\\\/:\x00<>:"|?*]/g, '_')
            .replace(/\.+/g, '.')
            .slice(0, 255) || 'download';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        }

        const transfer = this.activeTransfers.get(transferKey);
        if (transfer) {
          transfer.status = 'completed';
          transfer.uploadedBytes = transfer.fileSize;
        }

        this.offers.delete(offerId);
      } catch (error) {
        const transfer = this.activeTransfers.get(transferKey);
        if (transfer) {
          transfer.status = 'error';
          transfer.error = error instanceof Error ? error.message : 'Download failed';
        }
      }
    }
  }

  /**
   * Reject incoming file offer
   */
  rejectOffer(offerId: string): void {
    this.offers.delete(offerId);
    socketManager.emit('filedrop:reject', { offer_id: offerId });
  }

  /**
   * Cancel active transfer — correctly maps transferId to the uploader's internal uploadId
   * for the legacy path, or to the resumable session_id for the new path.
   */
  cancelTransfer(transferId: string): void {
    // Resumable path
    const sessionId = this.transferToSession.get(transferId);
    if (sessionId) {
      // Fire-and-forget — server cleanup is best-effort.
      void this.resumable.abort(sessionId);
      this.transferToSession.delete(transferId);
    }

    // Legacy path
    const uploadId = this.transferToUploadId.get(transferId);
    if (uploadId) {
      this.uploader.cancelUpload(uploadId);
      this.transferToUploadId.delete(transferId);
    } else if (!sessionId) {
      // Nothing mapped — nuke all legacy uploads as a fallback.
      this.uploader.cancelAll();
    }

    this.activeTransfers.delete(transferId);
    socketManager.emit('filedrop:cancel', { transfer_id: transferId });
  }

  /**
   * Pause a resumable upload (no-op on legacy path).
   */
  pauseTransfer(transferId: string): void {
    const sessionId = this.transferToSession.get(transferId);
    if (!sessionId) return;
    this.resumable.pause(sessionId);
    const t = this.activeTransfers.get(transferId);
    if (t && t.status === 'uploading') t.status = 'paused';
  }

  /**
   * Resume a paused resumable upload.
   */
  resumeTransfer(transferId: string): void {
    const sessionId = this.transferToSession.get(transferId);
    if (!sessionId) return;
    this.resumable.resume(sessionId);
    const t = this.activeTransfers.get(transferId);
    if (t && t.status === 'paused') t.status = 'uploading';
  }

  /**
   * List sessions in IndexedDB that can be resumed — useful for app startup
   * to offer "resume interrupted uploads" UI.
   */
  async listResumableSessions(): Promise<
    Array<{ session_id: string; file_name: string; file_size: number }>
  > {
    const rows = await this.resumable.listResumable();
    return rows.map(r => ({
      session_id: r.session_id,
      file_name: r.file_name,
      file_size: r.file_size,
    }));
  }

  /**
   * Get all active transfers
   */
  getActiveTransfers(): ActiveTransfer[] {
    return Array.from(this.activeTransfers.values());
  }

  /**
   * Get pending offers
   */
  getPendingOffers(): FileOffer[] {
    return Array.from(this.offers.values());
  }

  private setupSocketListeners(): void {
    // Listen for incoming file offers
    socketManager.on('filedrop:offer', (data: any) => {
      const offerId = `offer-${Date.now()}`;
      const offer: FileOffer = {
        id: offerId,
        senderId: data.sender_id,
        senderName: data.sender_name,
        fileName: data.file_name,
        fileSize: data.file_size,
        fileType: data.file_type,
        fileId: data.file_id,
        timestamp: Date.now(),
      };
      this.offers.set(offerId, offer);
    });

    // Listen for transfer progress updates
    socketManager.on('filedrop:progress', (data: any) => {
      const transfer = this.activeTransfers.get(data.transfer_id);
      if (transfer) {
        transfer.uploadedBytes = data.uploaded_bytes;
        transfer.speed = data.speed;
      }
    });

    // Listen for transfer completion
    socketManager.on('filedrop:complete', (data: any) => {
      const transfer = this.activeTransfers.get(data.transfer_id);
      if (transfer) {
        transfer.status = 'completed';
        transfer.uploadedBytes = transfer.fileSize;
      }
    });

    // Listen for transfer errors
    socketManager.on('filedrop:error', (data: any) => {
      const transfer = this.activeTransfers.get(data.transfer_id);
      if (transfer) {
        transfer.status = 'error';
        transfer.error = data.error;
      }
    });
  }
}
