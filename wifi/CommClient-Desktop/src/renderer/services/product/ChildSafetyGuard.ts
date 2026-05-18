/**
 * ChildSafetyGuard.ts — Safe defaults and protections for young users.
 *
 * CommClient is designed for families, classrooms, and isolated networks.
 * This service enforces safe defaults and provides guardrails to prevent
 * accidental or harmful actions by children.
 *
 * Safety features:
 *   1. Accidental call prevention: confirm before placing calls
 *   2. Accidental deletion prevention: confirm + undo window for messages
 *   3. Accidental leave prevention: confirm before leaving groups
 *   4. Profile change rate limiting: max 3 name changes per hour
 *   5. Screen share awareness: show "You are sharing" persistent banner
 *   6. Camera awareness: show recording-style indicator when camera is on
 *   7. Link safety: disable clickable links in messages (LAN-only, no internet)
 *   8. File type safety: only allow safe file types (images, documents)
 *   9. Contact safety: prevent messaging users not in contacts (optional)
 *  10. Volume safety: cap maximum speaker volume at 80% for headphones
 *
 * Configuration:
 *   All guards can be enabled/disabled per-feature.
 *   Default: all enabled in Simple Mode, all optional in Advanced Mode.
 */

import { AppLogger } from '../AppLogger';

const log = AppLogger.create('ChildSafetyGuard');

// ── Types ───────────────────────────────────────────────────

export interface SafetyConfig {
  confirmBeforeCalling: boolean;
  confirmBeforeDelete: boolean;
  confirmBeforeLeaveGroup: boolean;
  profileChangeRateLimit: boolean;
  screenShareBanner: boolean;
  cameraIndicator: boolean;
  disableLinks: boolean;
  safeFileTypesOnly: boolean;
  contactsOnlyMessaging: boolean;
  volumeCap: boolean;
  maxVolumePercent: number;
}

export interface SafetyCheckResult {
  allowed: boolean;
  reason?: string;           // i18n key explaining why blocked
  requiresConfirmation: boolean;
  confirmMessageKey?: string; // i18n key for confirm dialog
}

// ── Safe File Types ─────────────────────────────────────────

const SAFE_EXTENSIONS = new Set([
  // Images
  '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico',
  // Documents
  '.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt',
  // Spreadsheets
  '.xls', '.xlsx', '.csv', '.ods',
  // Presentations
  '.ppt', '.pptx', '.odp',
  // Audio
  '.mp3', '.wav', '.ogg', '.m4a', '.flac',
  // Video
  '.mp4', '.webm', '.mkv', '.avi', '.mov',
  // Archives (common, safe)
  '.zip', '.7z', '.rar',
]);

const BLOCKED_EXTENSIONS = new Set([
  '.exe', '.msi', '.bat', '.cmd', '.ps1', '.vbs', '.js', '.jar',
  '.scr', '.com', '.pif', '.reg', '.inf', '.hta', '.cpl', '.msc',
  '.wsf', '.wsh', '.lnk', '.sys', '.dll', '.drv',
]);

// ── Rate Limiter ────────────────────────────────────────────

interface RateLimitEntry {
  action: string;
  timestamps: number[];
}

class RateLimiter {
  private entries = new Map<string, RateLimitEntry>();

  check(action: string, maxCount: number, windowMs: number): boolean {
    const now = Date.now();
    let entry = this.entries.get(action);

    if (!entry) {
      entry = { action, timestamps: [] };
      this.entries.set(action, entry);
    }

    // Clean old entries
    entry.timestamps = entry.timestamps.filter((ts) => now - ts < windowMs);

    if (entry.timestamps.length >= maxCount) {
      return false; // Rate limited
    }

    entry.timestamps.push(now);
    return true;
  }

  remaining(action: string, maxCount: number, windowMs: number): number {
    const now = Date.now();
    const entry = this.entries.get(action);
    if (!entry) return maxCount;

    const recent = entry.timestamps.filter((ts) => now - ts < windowMs);
    return Math.max(0, maxCount - recent.length);
  }
}

// ── Main Service ────────────────────────────────────────────

class ChildSafetyGuardService {
  private config: SafetyConfig;
  private rateLimiter = new RateLimiter();

  constructor() {
    this.config = this.loadConfig();
  }

  // ── Configuration ───────────────────────────────────────

  private loadConfig(): SafetyConfig {
    const defaults: SafetyConfig = {
      confirmBeforeCalling: true,
      confirmBeforeDelete: true,
      confirmBeforeLeaveGroup: true,
      profileChangeRateLimit: true,
      screenShareBanner: true,
      cameraIndicator: true,
      disableLinks: true,
      safeFileTypesOnly: true,
      contactsOnlyMessaging: false,
      volumeCap: true,
      maxVolumePercent: 80,
    };

    try {
      const saved = localStorage.getItem('commclient_safety_config');
      if (saved) {
        return { ...defaults, ...JSON.parse(saved) };
      }
    } catch {}

    return defaults;
  }

  getConfig(): SafetyConfig {
    return { ...this.config };
  }

  updateConfig(partial: Partial<SafetyConfig>): void {
    this.config = { ...this.config, ...partial };
    try {
      localStorage.setItem('commclient_safety_config', JSON.stringify(this.config));
    } catch {}
    log.info('Safety config updated', partial);
  }

  /**
   * Apply full Simple Mode defaults (all guards on).
   */
  applySimpleMode(): void {
    this.updateConfig({
      confirmBeforeCalling: true,
      confirmBeforeDelete: true,
      confirmBeforeLeaveGroup: true,
      profileChangeRateLimit: true,
      screenShareBanner: true,
      cameraIndicator: true,
      disableLinks: true,
      safeFileTypesOnly: true,
      contactsOnlyMessaging: false,
      volumeCap: true,
      maxVolumePercent: 80,
    });
  }

  /**
   * Apply Advanced Mode defaults (minimal guards).
   */
  applyAdvancedMode(): void {
    this.updateConfig({
      confirmBeforeCalling: false,
      confirmBeforeDelete: true,   // Keep this even for advanced users
      confirmBeforeLeaveGroup: false,
      profileChangeRateLimit: false,
      screenShareBanner: true,     // Always show this
      cameraIndicator: true,       // Always show this
      disableLinks: false,
      safeFileTypesOnly: false,
      contactsOnlyMessaging: false,
      volumeCap: false,
      maxVolumePercent: 100,
    });
  }

  // ── Safety Checks ───────────────────────────────────────

  /**
   * Check if placing a call should be confirmed.
   */
  checkCall(targetName: string): SafetyCheckResult {
    if (!this.config.confirmBeforeCalling) {
      return { allowed: true, requiresConfirmation: false };
    }
    return {
      allowed: true,
      requiresConfirmation: true,
      confirmMessageKey: 'safety.confirm_call',
    };
  }

  /**
   * Check if deleting a message should be confirmed.
   */
  checkDeleteMessage(): SafetyCheckResult {
    if (!this.config.confirmBeforeDelete) {
      return { allowed: true, requiresConfirmation: false };
    }
    return {
      allowed: true,
      requiresConfirmation: true,
      confirmMessageKey: 'safety.confirm_delete',
    };
  }

  /**
   * Check if leaving a group should be confirmed.
   */
  checkLeaveGroup(groupName: string): SafetyCheckResult {
    if (!this.config.confirmBeforeLeaveGroup) {
      return { allowed: true, requiresConfirmation: false };
    }
    return {
      allowed: true,
      requiresConfirmation: true,
      confirmMessageKey: 'safety.confirm_leave_group',
    };
  }

  /**
   * Check if a profile name change is allowed (rate limited).
   */
  checkProfileChange(): SafetyCheckResult {
    if (!this.config.profileChangeRateLimit) {
      return { allowed: true, requiresConfirmation: false };
    }

    const allowed = this.rateLimiter.check('profile_change', 3, 3600000); // 3 per hour
    if (!allowed) {
      const remaining = this.rateLimiter.remaining('profile_change', 3, 3600000);
      return {
        allowed: false,
        reason: 'safety.profile_rate_limited',
        requiresConfirmation: false,
      };
    }

    return { allowed: true, requiresConfirmation: false };
  }

  /**
   * Check if a file is safe to send/receive.
   */
  checkFile(filename: string): SafetyCheckResult {
    const ext = '.' + filename.split('.').pop()?.toLowerCase();

    // Always block dangerous executables
    if (BLOCKED_EXTENSIONS.has(ext)) {
      return {
        allowed: false,
        reason: 'safety.file_blocked',
        requiresConfirmation: false,
      };
    }

    // In safe mode, only allow known-safe types
    if (this.config.safeFileTypesOnly && !SAFE_EXTENSIONS.has(ext)) {
      return {
        allowed: false,
        reason: 'safety.file_type_not_allowed',
        requiresConfirmation: false,
      };
    }

    return { allowed: true, requiresConfirmation: false };
  }

  /**
   * Process message text for display (strip/neutralize links if enabled).
   */
  sanitizeMessageForDisplay(text: string): string {
    if (!this.config.disableLinks) return text;

    // Replace URLs with plain text (non-clickable)
    return text.replace(
      /https?:\/\/[^\s<>"']+/gi,
      (match) => `[${match}]`
    );
  }

  /**
   * Get safe volume level.
   */
  getSafeVolume(requestedVolume: number): number {
    if (!this.config.volumeCap) return requestedVolume;
    const max = this.config.maxVolumePercent / 100;
    return Math.min(requestedVolume, max);
  }

  /**
   * Should the "You are sharing your screen" banner be shown?
   */
  shouldShowScreenShareBanner(): boolean {
    return this.config.screenShareBanner;
  }

  /**
   * Should the camera active indicator be shown?
   */
  shouldShowCameraIndicator(): boolean {
    return this.config.cameraIndicator;
  }
}

// ── Singleton ───────────────────────────────────────────────

export const childSafetyGuard = new ChildSafetyGuardService();
