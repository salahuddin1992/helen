/**
 * Filename / MIME helpers for chat media bubbles.
 *
 * The Message type carries no mime field today — we infer from the
 * filename in ``content``. Conservative inference: matches by
 * extension only, no magic-byte sniffing (those bytes live on disk
 * and would cost a fetch). False negatives end up in the generic
 * file bubble, which is the safe fallback.
 */

const VIDEO_EXTS = [
  'mp4', 'm4v', 'mov', 'webm', 'mkv', 'avi', 'wmv',
  'flv', 'mpeg', 'mpg', 'ts', '3gp', '3g2', 'ogv',
];

const AUDIO_EXTS = [
  'mp3', 'wav', 'ogg', 'oga', 'flac', 'aac', 'm4a',
  'wma', 'opus', 'aiff', 'aif',
];

const IMAGE_EXTS = [
  'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'avif',
  'heic', 'heif', 'tiff', 'tif', 'svg',
];

/** Browser-native (Chromium / Electron) playable subset. The rest
 *  fall back to "open in system default app" via shell.openPath. */
const CHROMIUM_PLAYABLE_VIDEO = new Set([
  'mp4', 'm4v', 'webm', 'ogv', 'mov',
]);
const CHROMIUM_PLAYABLE_AUDIO = new Set([
  'mp3', 'wav', 'ogg', 'oga', 'aac', 'm4a', 'flac', 'opus',
]);

export function getExtension(filename: string | null | undefined): string {
  if (!filename) return '';
  const dot = filename.lastIndexOf('.');
  if (dot < 0) return '';
  return filename.slice(dot + 1).toLowerCase();
}

export function isVideoFile(filename: string | null | undefined): boolean {
  return VIDEO_EXTS.includes(getExtension(filename));
}

export function isAudioFile(filename: string | null | undefined): boolean {
  return AUDIO_EXTS.includes(getExtension(filename));
}

export function isImageFile(filename: string | null | undefined): boolean {
  return IMAGE_EXTS.includes(getExtension(filename));
}

export function isPlayableVideoInChromium(
  filename: string | null | undefined,
): boolean {
  return CHROMIUM_PLAYABLE_VIDEO.has(getExtension(filename));
}

export function isPlayableAudioInChromium(
  filename: string | null | undefined,
): boolean {
  return CHROMIUM_PLAYABLE_AUDIO.has(getExtension(filename));
}

/** "1.4 MB", "240 KB", "732 B" — same units users see in Telegram /
 *  Discord. ``null`` -> em-dash so the bubble layout is stable while
 *  size metadata is still loading. */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

/** Pick a Lucide-style emoji icon for a generic file by extension.
 *  Keeps the bubble informative even before the user clicks. */
export function fileIconForExtension(ext: string): string {
  if (!ext) return '📄';
  if (['pdf'].includes(ext)) return '📕';
  if (['doc', 'docx', 'rtf', 'odt'].includes(ext)) return '📝';
  if (['xls', 'xlsx', 'ods', 'csv'].includes(ext)) return '📊';
  if (['ppt', 'pptx', 'odp'].includes(ext)) return '📈';
  if (['zip', '7z', 'rar', 'tar', 'gz', 'xz', 'bz2'].includes(ext)) return '🗜️';
  if (['exe', 'msi', 'dmg', 'pkg', 'deb', 'rpm', 'apk'].includes(ext)) return '⚙️';
  if (['html', 'htm', 'xml', 'json', 'yml', 'yaml', 'toml'].includes(ext)) return '🌐';
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'go', 'rs', 'c', 'cpp', 'h', 'java', 'rb', 'php', 'sh'].includes(ext)) return '👨‍💻';
  if (['txt', 'log', 'md'].includes(ext)) return '📋';
  if (['ttf', 'otf', 'woff', 'woff2'].includes(ext)) return '🔤';
  return '📄';
}
