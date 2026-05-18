/**
 * downloads.ts — main-process IPC handlers for chat downloads.
 *
 * Three operations the renderer needs:
 *   1. ``downloads:save-buffer``  — write bytes already in renderer
 *      memory to ``~/Downloads/<filename>`` (chat sent us the bytes
 *      via authed fetch + arrayBuffer()).
 *   2. ``downloads:stream-url``   — stream a Helen-Server file
 *      endpoint to disk with progress events. Used for big videos
 *      where we don't want the renderer holding the whole buffer.
 *   3. ``downloads:open-path``    — launch the file with the OS
 *      default application via ``shell.openPath``. No shell
 *      injection risk because Electron treats the argument as a
 *      literal path.
 *   4. ``downloads:reveal``       — show the file in Finder /
 *      Explorer / Nautilus.
 *
 * Filename sanitization
 * ---------------------
 * Anything dangerous in the suggested filename (path separators,
 * NUL bytes, drive letters, leading dots) is stripped before the
 * file is written. The user always lands on something inside the
 * configured downloads dir — never anywhere else on disk.
 */

import { app, ipcMain, shell, BrowserWindow } from 'electron';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as https from 'node:https';
import * as http from 'node:http';

function sanitizeFilename(input: string): string {
  // Strip anything that might escape the downloads dir or trick
  // Windows into resolving to a different drive.
  let name = (input || 'download').toString();
  name = name.replace(/[\x00-\x1f]/g, ''); // control chars
  name = name.replace(/[\\/]/g, '_');       // path separators
  name = name.replace(/^[.\s]+/, '');       // leading dots / spaces
  name = name.replace(/[<>:"|?*]/g, '_');   // illegal Windows chars
  if (!name) name = 'download';
  if (name.length > 200) {
    const dot = name.lastIndexOf('.');
    if (dot > 0 && name.length - dot < 16) {
      name = name.slice(0, 200 - (name.length - dot)) + name.slice(dot);
    } else {
      name = name.slice(0, 200);
    }
  }
  return name;
}

/** Resolve a unique destination path under the user's Downloads
 *  folder. If a file with the same name already exists we append
 *  ``-2``, ``-3``, etc. so two messages with the same filename
 *  don't clobber each other. */
function resolveUniquePath(filename: string): string {
  const dir = app.getPath('downloads');
  fs.mkdirSync(dir, { recursive: true });
  const base = sanitizeFilename(filename);
  let candidate = path.join(dir, base);
  if (!fs.existsSync(candidate)) return candidate;

  const dot = base.lastIndexOf('.');
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const ext = dot > 0 ? base.slice(dot) : '';
  let n = 2;
  while (fs.existsSync(candidate)) {
    candidate = path.join(dir, `${stem}-${n}${ext}`);
    n += 1;
    if (n > 9999) {
      // Pathological loop guard — drop to a timestamped name.
      candidate = path.join(dir, `${stem}-${Date.now()}${ext}`);
      break;
    }
  }
  return candidate;
}

export function registerDownloadHandlers(): void {
  ipcMain.handle('downloads:save-buffer', async (
    _e, filename: string, bytes: ArrayBuffer,
  ) => {
    try {
      const dest = resolveUniquePath(filename);
      fs.writeFileSync(dest, Buffer.from(bytes));
      return { ok: true, path: dest };
    } catch (err: any) {
      return { ok: false, error: String(err?.message || err) };
    }
  });

  ipcMain.handle('downloads:stream-url', async (
    e, url: string, filename: string, bearerToken?: string,
  ) => {
    return new Promise((resolve) => {
      let dest = '';
      try {
        dest = resolveUniquePath(filename);
      } catch (err: any) {
        resolve({ ok: false, error: String(err?.message || err) });
        return;
      }

      const isHttps = url.startsWith('https:');
      const lib = isHttps ? https : http;
      const headers: Record<string, string> = {};
      if (bearerToken) {
        headers['Authorization'] = `Bearer ${bearerToken}`;
      }

      const req = lib.get(url, { headers }, (res) => {
        if (!res.statusCode || res.statusCode >= 400) {
          resolve({
            ok: false,
            error: `HTTP ${res.statusCode || '?'}`,
          });
          res.resume();
          return;
        }
        const total = res.headers['content-length']
          ? parseInt(String(res.headers['content-length']), 10)
          : null;
        let received = 0;
        const out = fs.createWriteStream(dest);
        const win = BrowserWindow.fromWebContents(e.sender);

        res.on('data', (chunk: Buffer) => {
          received += chunk.length;
          // Throttle progress events: every 256 KiB or so. Renderer
          // already polls — flooding the channel doesn't help.
          if (received % (256 * 1024) < chunk.length) {
            win?.webContents.send('downloads:progress', {
              url, bytes_received: received, bytes_total: total,
            });
          }
        });
        res.pipe(out);
        out.on('finish', () => {
          out.close();
          win?.webContents.send('downloads:progress', {
            url, bytes_received: received, bytes_total: total,
          });
          resolve({ ok: true, path: dest, bytes: received });
        });
        out.on('error', (werr) => {
          resolve({ ok: false, error: String(werr.message) });
        });
      });
      req.on('error', (rerr) => {
        resolve({ ok: false, error: String(rerr.message) });
      });
      req.setTimeout(60_000, () => {
        req.destroy(new Error('download timeout'));
      });
    });
  });

  ipcMain.handle('downloads:open-path', async (_e, absPath: string) => {
    // shell.openPath returns an empty string on success, error string
    // on failure. We pass it back unchanged for the renderer to surface.
    return await shell.openPath(absPath);
  });

  ipcMain.handle('downloads:reveal', async (_e, absPath: string) => {
    shell.showItemInFolder(absPath);
  });
}
