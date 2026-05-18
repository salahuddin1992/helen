/**
 * Shared types for the update subsystem.
 */

export type UpdateChannel = 'stable' | 'beta' | 'canary';

export type UpdateSource = 'lan-mirror' | 'direct-github' | 'custom-feed';

export interface UpdateManifestEntry {
  version: string;
  channel: UpdateChannel;
  releasedAt: string; // ISO 8601
  url: string;        // absolute or mirror-relative
  sha512: string;     // Base64 SHA-512 of the installer
  signature?: string; // Base64 Ed25519 signature of sha512
  size: number;       // bytes
  notes?: string;
  mandatory?: boolean;
}

export interface UpdateStatus {
  state:
    | 'idle'
    | 'checking'
    | 'available'
    | 'not-available'
    | 'downloading'
    | 'downloaded'
    | 'error';
  channel: UpdateChannel;
  source: UpdateSource;
  currentVersion: string;
  target?: UpdateManifestEntry;
  progress?: {
    bytesPerSecond: number;
    percent: number;
    transferred: number;
    total: number;
  };
  error?: string;
  checkedAt?: number; // epoch ms
}

export interface UpdaterOptions {
  channel?: UpdateChannel;
  /** Base LAN-mirror URL, e.g. http://192.168.1.10:8000/api/updates */
  lanMirrorUrl?: string;
  /** Fallback internet feed base URL. */
  internetFeedUrl?: string;
  /** Poll interval for checks (minutes). Default 60. */
  checkIntervalMinutes?: number;
  /** Base64 Ed25519 public key — required for signature verification. */
  publicKeyBase64?: string;
  /** Require signature verification (default true in packaged). */
  requireSignature?: boolean;
  /** Auto-download when an update is found (default true). */
  autoDownload?: boolean;
  /** Auto-install on app quit (default true). */
  autoInstallOnAppQuit?: boolean;
  /** Allow downgrade — false by default. */
  allowDowngrade?: boolean;
}
