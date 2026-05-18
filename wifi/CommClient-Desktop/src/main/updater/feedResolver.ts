/**
 * Feed resolver — picks the best update source at runtime.
 *
 * Order of preference:
 *   1. LAN mirror — the CommClient server exposes /api/updates/manifest.json
 *      with entries cached locally. Preferred because it works offline
 *      (for the LAN clients) and is signed by the leader.
 *   2. Internet feed — GitHub Releases-style JSON (channel-stable.json).
 *
 * The chosen source is probed by HEAD request with a 3s timeout; failure
 * falls through to the next option.
 */

import type { UpdateChannel, UpdateSource, UpdaterOptions } from './types';
import { net } from 'electron';

export interface ResolvedFeed {
  source: UpdateSource;
  manifestUrl: string;
  baseUrl: string;
}

function channelFile(channel: UpdateChannel): string {
  return `channel-${channel}.json`;
}

function headProbe(url: string, timeoutMs = 3000): Promise<boolean> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      resolve(ok);
    };
    try {
      const req = net.request({ method: 'HEAD', url });
      req.on('response', (resp) => {
        finish(resp.statusCode >= 200 && resp.statusCode < 400);
      });
      req.on('error', () => finish(false));
      req.on('abort', () => finish(false));
      setTimeout(() => {
        try {
          req.abort();
        } catch {
          /* ignore */
        }
        finish(false);
      }, timeoutMs);
      req.end();
    } catch {
      finish(false);
    }
  });
}

export async function resolveFeed(
  opts: UpdaterOptions
): Promise<ResolvedFeed | null> {
  const channel = opts.channel || 'stable';
  const sources: Array<{ source: UpdateSource; base?: string }> = [];

  if (opts.lanMirrorUrl) {
    sources.push({ source: 'lan-mirror', base: opts.lanMirrorUrl.replace(/\/$/, '') });
  }
  if (opts.internetFeedUrl) {
    sources.push({ source: 'direct-github', base: opts.internetFeedUrl.replace(/\/$/, '') });
  }

  for (const s of sources) {
    if (!s.base) continue;
    const manifestUrl = `${s.base}/${channelFile(channel)}`;
    const ok = await headProbe(manifestUrl);
    if (ok) {
      console.log(`[feedResolver] source=${s.source} manifest=${manifestUrl}`);
      return { source: s.source, manifestUrl, baseUrl: s.base };
    }
    console.log(`[feedResolver] probe failed: ${manifestUrl}`);
  }

  console.log('[feedResolver] no feed available');
  return null;
}
