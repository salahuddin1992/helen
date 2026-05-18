/**
 * Chat-downloads service — bridge between the renderer's chat
 * bubbles and the Electron main-process download IPC.
 *
 * Why a wrapper at all
 * --------------------
 * The bubbles need three things: (1) start a download, (2) watch
 * progress, (3) afterwards open the file with the OS default app.
 * Doing it directly via ``window.electronAPI.downloads.*`` would
 * scatter concerns. This module owns the auth-token plumbing
 * (the file URL needs ``Authorization: Bearer …`` to fetch from
 * Helen-Server) and exposes a small typed surface.
 *
 * Browser-mode fallback
 * ---------------------
 * When the renderer is opened from a regular browser tab (web
 * preview build) ``window.electronAPI`` is undefined. In that case
 * we fall back to a classic ``<a download>`` flow that hands the
 * file to the browser's own downloader.
 */

import {
  getAccessToken,
  getBaseUrl,
  fetchAuthorizedBlobUrl,
} from './api.client';

export interface DownloadProgress {
  bytes_received: number;
  bytes_total: number | null;
}

// The full ``window.electronAPI`` typing lives in preload/index.ts.
// We just check at runtime that the ``downloads`` namespace is
// actually attached — older binaries that don't ship the new IPC
// fall through to the browser-mode anchor download.
function isElectron(): boolean {
  // ``downloads`` is optional on older builds — gate everything on
  // its presence rather than the bare existence of electronAPI.
  const w = (typeof window !== 'undefined' ? window : null) as any;
  return !!w?.electronAPI?.downloads;
}

/** Resolve the absolute URL for a server file_id. */
export function fileUrl(fileId: string): string {
  return `${getBaseUrl()}/api/files/${fileId}`;
}

/** Stream a Helen-Server file to the user's Downloads folder.
 *  In browser mode, falls through to a plain ``<a download>``
 *  (no progress tracking) so chat downloads still work for the
 *  web preview. Returns the absolute path written, or ``null`` in
 *  browser mode (the browser owns the file). */
export async function downloadFileToDisk(
  fileId: string,
  filename: string,
  onProgress?: (p: DownloadProgress) => void,
): Promise<{ path: string | null; bytes: number | null; error?: string }> {
  const url = fileUrl(fileId);
  const token = getAccessToken();

  if (isElectron()) {
    const dl = window.electronAPI!.downloads!;
    let unsubscribe: (() => void) | null = null;
    if (onProgress) {
      unsubscribe = dl.onProgress((p) => {
        if (p.url === url) {
          onProgress({
            bytes_received: p.bytes_received,
            bytes_total: p.bytes_total,
          });
        }
      });
    }
    try {
      const r = await dl.streamUrl(url, filename, token || undefined);
      if (!r.ok) return { path: null, bytes: null, error: r.error };
      return { path: r.path || null, bytes: r.bytes ?? null };
    } finally {
      unsubscribe?.();
    }
  }

  // Browser fallback: blob URL → anchor click.
  try {
    const blobUrl = await fetchAuthorizedBlobUrl(`/api/files/${fileId}`);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename || `file-${fileId}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 30_000);
    return { path: null, bytes: null };
  } catch (e: any) {
    return { path: null, bytes: null, error: String(e?.message || e) };
  }
}

/** Open a previously-downloaded file with the OS default app
 *  (Windows: ShellExecute, macOS: open, Linux: xdg-open). Returns
 *  ``true`` on success, ``false`` if the call returned an error
 *  string from shell.openPath. */
export async function openWithDefaultApp(
  absPath: string,
): Promise<boolean> {
  if (!isElectron()) return false;
  const result = await window.electronAPI!.downloads!.openPath(absPath);
  return result === '';
}

export async function revealInFolder(absPath: string): Promise<void> {
  if (!isElectron()) return;
  await window.electronAPI!.downloads!.revealInFolder(absPath);
}

/** Get an auth'd blob URL for in-app `<video>` / `<audio>` /
 *  `<img>` playback. The blob is owned by the caller — call
 *  ``URL.revokeObjectURL`` when the player unmounts. */
export async function getMediaBlobUrl(fileId: string): Promise<string> {
  return await fetchAuthorizedBlobUrl(`/api/files/${fileId}`);
}
