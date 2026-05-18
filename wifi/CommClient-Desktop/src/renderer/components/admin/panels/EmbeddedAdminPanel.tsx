/**
 * EmbeddedAdminPanel — renders one of the 11 new server-side admin SPAs
 * (HTML/JS shipped under /admin/modules/<slug>.html) inside the desktop
 * client via a sandboxed iframe.
 *
 * Why iframe and not <webview>?
 *   - We want the panel to share the renderer process for tight integration
 *     with toasts/notifications. The Electron <webview> partitions the
 *     renderer in a way that complicates token forwarding.
 *   - The server pages are already same-origin to the API; loading them
 *     through the desktop's existing serverUrl makes cookies/CORS irrelevant.
 *
 * Auth bridging
 * ─────────────
 * The server pages expect a Bearer token in:
 *   1. an Authorization header on XHR/fetch calls, OR
 *   2. a postMessage handshake at boot time.
 * We rely on (2) so we never expose the access token in a URL/query string
 * (which would persist in browser history, server access logs, etc.).
 *
 * Sandbox model
 * ─────────────
 * The iframe is sandboxed with the minimum set of privileges needed for the
 * admin SPA to render and call the same-origin API. Specifically:
 *   - allow-scripts, allow-same-origin, allow-forms (panels submit forms)
 *   - NO allow-top-navigation (so a hostile panel can't escape the desktop)
 *   - NO allow-popups (file pickers go through the Electron IPC bridge)
 *   - NO allow-modals (we use the desktop's own modal stack)
 */

import React, { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { ExternalLink, RefreshCw, AlertTriangle, ShieldAlert } from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import type { AdminPanelMeta, AdminPanelSlug } from './AdminPanelRegistry';
import { getPanel } from './AdminPanelRegistry';

export interface EmbeddedAdminPanelProps {
  slug: AdminPanelSlug;
  /** Optional override (mostly for tests / storybook). */
  overrideMeta?: AdminPanelMeta;
  /** Notify host when the iframe finishes loading. */
  onReady?: () => void;
  /** Forward panel errors up to the parent (e.g. for toast). */
  onError?: (msg: string) => void;
}

/** Handshake message contract.
 *
 *  parent → iframe:  { type: 'HELEN_ADMIN_AUTH', accessToken, serverUrl, lang, theme }
 *  iframe → parent:  { type: 'HELEN_ADMIN_READY' }
 *                    { type: 'HELEN_ADMIN_REQUEST_TOKEN' }   (re-handshake on 401)
 *                    { type: 'HELEN_ADMIN_NOTIFY', severity, text }
 *                    { type: 'HELEN_ADMIN_OPEN_EXTERNAL', url }
 *                    { type: 'HELEN_ADMIN_ERROR', message } */
type ChildMessage =
  | { type: 'HELEN_ADMIN_READY' }
  | { type: 'HELEN_ADMIN_REQUEST_TOKEN' }
  | { type: 'HELEN_ADMIN_NOTIFY'; severity: 'info' | 'success' | 'warn' | 'error'; text: string }
  | { type: 'HELEN_ADMIN_OPEN_EXTERNAL'; url: string }
  | { type: 'HELEN_ADMIN_ERROR'; message: string };

const SANDBOX = 'allow-scripts allow-same-origin allow-forms';
// Permissions Policy stays restrictive: no camera/mic/etc. from inside the
// admin frame. The desktop client already mediates these via the main
// process and would re-prompt for permission anyway.
const ALLOW = "fullscreen 'self'; clipboard-read; clipboard-write";

const EmbeddedAdminPanel: React.FC<EmbeddedAdminPanelProps> = ({
  slug,
  overrideMeta,
  onReady,
  onError,
}) => {
  const meta = overrideMeta ?? getPanel(slug);
  const serverUrl = useAuthStore((s) => s.serverUrl);
  const tokens = useAuthStore((s) => s.tokens);
  const user = useAuthStore((s) => s.user);

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const src = useMemo(() => {
    if (!serverUrl || !meta) return '';
    // We append a cache-busting key on manual refresh so the iframe re-loads
    // even when the underlying URL is unchanged.
    const url = `${serverUrl.replace(/\/+$/, '')}/admin/modules/${meta.slug}.html`;
    return refreshKey ? `${url}?_r=${refreshKey}` : url;
  }, [serverUrl, meta, refreshKey]);

  // ── postMessage handshake ─────────────────────────────────────────────
  const sendAuth = useCallback(() => {
    if (!iframeRef.current?.contentWindow || !tokens?.access_token) return;
    iframeRef.current.contentWindow.postMessage(
      {
        type: 'HELEN_ADMIN_AUTH',
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
        serverUrl,
        // Forward language so the panel can pick its locale without a
        // separate API call. Defaults to ar — the project's primary RTL.
        lang: (window as any).__helenLang || 'ar',
        // Forward dark theme by default — matches the desktop chrome.
        theme: 'dark',
        userId: user?.id,
        role: user?.role,
      },
      // We trust the server origin because we just navigated to it. Using
      // an exact origin is critical here so the iframe can't be spoofed.
      new URL(serverUrl).origin,
    );
  }, [serverUrl, tokens, user]);

  // Listen for child messages.
  useEffect(() => {
    const handler = (ev: MessageEvent) => {
      // Origin gate: we only accept messages from the configured server.
      try {
        if (!serverUrl) return;
        const expected = new URL(serverUrl).origin;
        if (ev.origin !== expected) return;
      } catch { return; }

      const data = ev.data as ChildMessage | undefined;
      if (!data || typeof data !== 'object') return;

      switch (data.type) {
        case 'HELEN_ADMIN_READY':
          setLoading(false);
          setError(null);
          sendAuth();
          onReady?.();
          break;
        case 'HELEN_ADMIN_REQUEST_TOKEN':
          // Child detected a 401, ask us to re-handshake with a fresh token.
          sendAuth();
          break;
        case 'HELEN_ADMIN_OPEN_EXTERNAL':
          // Route through main-process IPC so URLs open in the user's
          // system browser instead of inside the iframe.
          (window as any).electronAPI?.shell?.openExternal?.(data.url);
          break;
        case 'HELEN_ADMIN_ERROR':
          setError(data.message);
          onError?.(data.message);
          break;
        case 'HELEN_ADMIN_NOTIFY':
          // Bubble up via a custom event so AdminNotifications can pick it up.
          window.dispatchEvent(
            new CustomEvent('helen-admin-notify', {
              detail: {
                slug,
                severity: data.severity,
                text: data.text,
                ts: Date.now(),
              },
            }),
          );
          break;
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [serverUrl, sendAuth, slug, onReady, onError]);

  // Push fresh tokens whenever they rotate (auth.store handles refresh).
  useEffect(() => {
    if (!loading) sendAuth();
  }, [tokens?.access_token, sendAuth, loading]);

  // Fallback: if the panel never sends a HELEN_ADMIN_READY (e.g. legacy
  // HTML that just uses the URL token), push auth on iframe `load` event
  // and clear loading after a short grace period.
  const handleIframeLoad = useCallback(() => {
    sendAuth();
    // Give the page 800ms to send its READY before we forcibly hide the
    // spinner — keeps the experience snappy for static pages.
    const id = setTimeout(() => setLoading(false), 800);
    return () => clearTimeout(id);
  }, [sendAuth]);

  // ── Guards ────────────────────────────────────────────────────────────
  if (!meta) {
    return (
      <div className="p-6 text-center text-red-400 text-sm">
        <AlertTriangle className="mx-auto mb-2" size={24} />
        Unknown admin panel: <code>{slug}</code>
      </div>
    );
  }

  if (!serverUrl) {
    return (
      <div className="p-6 text-center text-yellow-400 text-sm">
        <ShieldAlert className="mx-auto mb-2" size={24} />
        Server URL not configured.
      </div>
    );
  }

  if (user?.role !== 'admin') {
    return (
      <div className="p-6 text-center text-yellow-400 text-sm">
        <ShieldAlert className="mx-auto mb-2" size={24} />
        This panel requires the admin role.
      </div>
    );
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full bg-surface-950">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-surface-800 bg-surface-900">
        <div className="flex items-center gap-2 min-w-0">
          <meta.icon size={14} className={meta.colorClass} />
          <span className="text-xs font-semibold text-white truncate">
            {meta.labelAr} <span className="text-gray-500">· {meta.labelEn}</span>
          </span>
          <span className="text-[10px] text-gray-500 px-1.5 py-0.5 rounded bg-surface-800">
            {meta.requiresPermission}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => { setLoading(true); setRefreshKey((k) => k + 1); }}
            className="px-2 py-1 text-xs rounded bg-surface-800 hover:bg-surface-700 text-gray-300 inline-flex items-center gap-1"
            title="Reload panel"
          >
            <RefreshCw size={11} /> Reload
          </button>
          <button
            onClick={() => (window as any).electronAPI?.shell?.openExternal?.(src)}
            className="px-2 py-1 text-xs rounded bg-surface-800 hover:bg-surface-700 text-gray-300 inline-flex items-center gap-1"
            title="Open in external browser"
          >
            <ExternalLink size={11} /> External
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 relative">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface-950/80 z-10">
            <div className="text-center">
              <div className="inline-block w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
              <div className="text-xs text-gray-400 mt-2">جارِ تحميل اللوحة…</div>
            </div>
          </div>
        )}
        {error && (
          <div className="absolute inset-x-0 top-0 z-20 m-2 px-3 py-2 rounded bg-red-900/80 text-red-100 text-xs flex items-start gap-2">
            <AlertTriangle size={14} className="shrink-0 mt-0.5" />
            <div className="flex-1">{error}</div>
            <button onClick={() => setError(null)} className="text-red-200 hover:text-white">×</button>
          </div>
        )}
        <iframe
          ref={iframeRef}
          key={refreshKey}
          src={src}
          title={meta.labelEn}
          sandbox={SANDBOX}
          allow={ALLOW}
          referrerPolicy="strict-origin"
          onLoad={handleIframeLoad}
          className="w-full h-full border-0 bg-white"
          style={{ colorScheme: 'dark' }}
        />
      </div>
    </div>
  );
};

export default EmbeddedAdminPanel;
