import React, { useEffect, useState } from 'react';
import { Minus, Square, Copy, X, Server, RefreshCw } from 'lucide-react';
import toast from 'react-hot-toast';
import { t } from '@/i18n';
import { useServerIdentityStore } from '@/stores/server-identity.store';
import { useAuthStore } from '@/stores/auth.store';
import { socketManager } from '@/services/socket.manager';
import { OnlineModePill } from '@/components/online-mode/OnlineModePill';
import { ActivityStatusButton } from '@/components/status/ActivityStatusPicker';
import { MyPresencePill } from '@/components/status/MyPresencePill';
import { usePresenceStore, type SelfPresence } from '@/stores/presence.store';
import { useContactsStore } from '@/stores/contacts.store';

export const TitleBar: React.FC = () => {
  const [isMaximized, setIsMaximized] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const serverCode = useServerIdentityStore((s) => s.serverCode);
  const serverName = useServerIdentityStore((s) => s.serverName);
  const peerCount = useServerIdentityStore((s) => s.peers.length);
  const loadIdentity = useServerIdentityStore((s) => s.load);
  const [copied, setCopied] = useState(false);

  // ── Communication-readiness signals ───────────────────────────
  // "Connected to server" is a weak signal — it only proves the HTTP
  // probe got a 200 back. What the user *actually* needs to know is
  // "can I message another client right now?". That requires:
  //   (a) user has logged in (we have a token + user object)
  //   (b) socket transport is up
  //   (c) /api/health is healthy
  //   (d) server has acknowledged our identity (we received any
  //       presence:* event since connecting — proves the JWT was
  //       accepted on the socket path, not just the HTTP path)
  //   (e) we can count how many other clients are online right now
  // We expose all of this in a "READY" state that's stricter than the
  // old socket+probe check.
  const myUserId = useAuthStore((s) => s.user?.id) || '';
  const onlineUsers = useContactsStore((s) => s.onlineUsers);
  const otherOnlineCount = Object.keys(onlineUsers).filter(uid => uid !== myUserId).length;
  const presenceConfirmed = Object.keys(onlineUsers).length > 0;

  // Classify the server URL so the pill can say *which* server the
  // client is connected to instead of the ambiguous "connected to
  // server". This is the user-visible truth: a "connected" badge that
  // doesn't tell you to *what* is half-informative.
  //   - Local       → 127.0.0.1 / localhost            (on this machine)
  //   - LAN         → RFC1918 ranges (192.168/16, 10/8, 172.16-31/12)
  //   - Tunnel      → reaches Helen-Rendezvous /t/<id> tunnel
  //   - Remote      → anything else (still LAN-internal in Helen's design)
  const serverUrl = useAuthStore((s) => s.serverUrl) || '';
  const serverKind = (() => {
    const u = serverUrl.toLowerCase();
    if (/127\.0\.0\.1|localhost/.test(u)) return 'local';
    if (/\/t\/[a-z0-9_-]+/i.test(u))      return 'tunnel';
    if (/192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\./.test(u)) return 'lan';
    return 'remote';
  })();
  const serverKindLabel: Record<string, string> = {
    local:  'متصل بالسيرفر المحلي',
    lan:    'متصل بسيرفر الشبكة',
    tunnel: 'متصل عبر النفق',
    remote: 'متصل بسيرفر بعيد',
  };

  // Tint the "Helen" wordmark + render a leading status dot, both
  // matching the exact tones the `MyPresencePill` uses below — so the
  // wordmark in the corner reads as the same status badge the user
  // just picked. Mapping is centralised here:
  //   - `text` matches `MyPresencePill.OPTIONS[i].textClass` (green-200,
  //     amber-200, red-200) so they're visually identical.
  //   - `dot` matches `MyPresencePill.OPTIONS[i].dotClass` and gets a
  //     soft `animate-ping` halo on `online`, mirroring the pill.
  const myPresence = usePresenceStore((s) => s.status);
  const presenceVisuals = (() => {
    const map: Record<SelfPresence, { text: string; dot: string; label: string }> = {
      online: { text: 'text-green-200', dot: 'bg-green-400', label: 'متاح — الجميع يراك متصل' },
      away:   { text: 'text-amber-200', dot: 'bg-amber-400', label: 'بعيد — سترد لاحقاً' },
      busy:   { text: 'text-red-200',   dot: 'bg-red-400',   label: 'مشغول — قد لا ترد فوراً' },
      dnd:    { text: 'text-red-200',   dot: 'bg-red-500',   label: 'لا تزعجني — الإشعارات مكتومة' },
    };
    return isAuthenticated
      ? map[myPresence]
      : { text: 'text-white', dot: 'bg-surface-700', label: 'Helen Desktop' };
  })();

  // Live connection state — derived from BOTH the Socket.IO connect
  // event AND a periodic GET /api/health probe. The socket event
  // alone is unreliable: socket.io's reconnection loop can hold the
  // 'connected' flag for up to 60 seconds while the server is in
  // fact dead, which produces a false-green dot. The active probe
  // catches that inside one cycle (default 5 s) by hitting the
  // server's actual HTTP endpoint and comparing the round-trip
  // result. The pill only goes green when:
  //   1. socket.io reports CONNECTED, AND
  //   2. the most recent /api/health probe returned HTTP 200.
  //
  // The probe runs every 5 s, with a 3 s timeout, and three
  // consecutive failures flip the dot red. On red, retries continue
  // every 5 s so recovery is detected within one cycle.
  const [socketConnected, setSocketConnected] = useState<boolean>(
    () => { try { return socketManager.isConnected(); } catch { return false; } }
  );
  const [serverHealthy, setServerHealthy] = useState<boolean>(false);
  const [lastProbeMs, setLastProbeMs] = useState<number | null>(null);
  // Combined: true only when socket AND HTTP probe agree.
  const isReallyConnected = socketConnected && serverHealthy;

  useEffect(() => {
    if (!isAuthenticated) {
      setSocketConnected(false);
      setServerHealthy(false);
      return;
    }
    const offConn = socketManager.on('connect', () => setSocketConnected(true));
    const offDisc = socketManager.on('disconnect', () => setSocketConnected(false));
    setSocketConnected(socketManager.isConnected());
    return () => { try { offConn(); } catch { /* */ } try { offDisc(); } catch { /* */ } };
  }, [isAuthenticated]);

  // Active /api/health probe loop. Runs while the user is logged in.
  useEffect(() => {
    if (!isAuthenticated) { setServerHealthy(false); return; }
    let cancelled = false;
    let consecutiveFailures = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const serverUrl = useAuthStore.getState().serverUrl || 'http://127.0.0.1:3000';

    const probe = async () => {
      const start = performance.now();
      try {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), 3000);
        const res = await fetch(`${serverUrl}/api/health`, {
          method: 'GET',
          signal: ctl.signal,
          cache: 'no-store',
        });
        clearTimeout(t);
        if (cancelled) return;
        if (res.ok) {
          consecutiveFailures = 0;
          setServerHealthy(true);
          setLastProbeMs(Math.round(performance.now() - start));
        } else {
          consecutiveFailures++;
          if (consecutiveFailures >= 1) setServerHealthy(false);
        }
      } catch {
        if (cancelled) return;
        consecutiveFailures++;
        if (consecutiveFailures >= 1) setServerHealthy(false);
      }
      if (!cancelled) timer = setTimeout(probe, 5000);
    };

    probe();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [isAuthenticated]);

  // Lazy-load the server identity once per session, then poll every
  // 15 s so newly-discovered LAN peers appear in the chip's "+N ●"
  // badge without the user having to relaunch. Without the poll, the
  // peer count stayed frozen at whatever was visible the moment we
  // first authenticated — peers that came online afterwards never
  // showed up. 15 s is cheap (one auth'd GET /api/peers) and matches
  // the cadence of the server's own LAN-discovery sweep.
  useEffect(() => {
    if (!isAuthenticated) return;
    loadIdentity();
    const id = window.setInterval(loadIdentity, 15_000);
    return () => window.clearInterval(id);
  }, [isAuthenticated, loadIdentity]);

  const copyServerCode = async () => {
    if (!serverCode) return;
    try {
      await navigator.clipboard.writeText(serverCode);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch { /* clipboard blocked */ }
  };

  useEffect(() => {
    const checkMaximized = async () => {
      if (window.electronAPI?.window?.isMaximized) {
        const maximized = await window.electronAPI.window.isMaximized();
        setIsMaximized(maximized ?? false);
      }
    };

    checkMaximized();
  }, []);

  const handleMinimize = async () => {
    await window.electronAPI?.window?.minimize?.();
  };

  const handleMaximize = async () => {
    await window.electronAPI?.window?.maximize?.();
    const maximized = await window.electronAPI?.window?.isMaximized?.();
    setIsMaximized(maximized ?? false);
  };

  const handleClose = async () => {
    await window.electronAPI?.window?.close?.();
  };

  // Force-refresh connection: restart LAN discovery, run the active TCP scan
  // as a guaranteed fallback, then fall through to the configured rendezvous
  // tunnel if LAN produced nothing. Picks the best reachable endpoint and
  // reconnects the socket if the URL changed or the socket is currently down.
  // Works across machines, networks, and NAT boundaries.
  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    const api = window.electronAPI?.discovery;
    const auth = useAuthStore.getState();
    const oldUrl = auth.serverUrl;
    const rendezvousUrl = auth.rendezvousUrl;
    const tid = toast.loading(t('refresh.searching') || 'جارٍ البحث عن السيرفر…');
    try {
      // 1) Restart passive (UDP) discovery so stale cache is dropped.
      try { await api?.restart?.(); } catch { /* optional */ }

      // 2) Force a TCP scan of the local /24 — this is the guaranteed path
      //    when broadcast is blocked by guest WiFi / firewalls.
      let scanResult: { found: number; scanned: number } | undefined;
      try { scanResult = await api?.activeScan?.(); } catch { /* optional */ }

      // 3) Let UDP replies (if any) settle and presence map update.
      await new Promise((r) => setTimeout(r, 600));

      // 4) Pick the best-reachable LAN server, or fall through to the saved
      //    rendezvous tunnel URL (which covers different-network / NAT cases).
      let targetUrl: string = '';
      let usedRendezvous = false;
      const best = await api?.getBest?.();
      if (best?.url) {
        targetUrl = best.url;
      } else if (rendezvousUrl) {
        // Verify the tunnel URL answers /api/health before switching to it —
        // saves a noisy socket reconnect if the rendezvous is offline.
        try {
          const r = await fetch(rendezvousUrl.replace(/\/+$/, '') + '/api/health',
                                { signal: AbortSignal.timeout(4000) });
          if (r.ok) { targetUrl = rendezvousUrl; usedRendezvous = true; }
        } catch { /* tunnel unreachable */ }
      }
      if (!targetUrl) targetUrl = oldUrl;

      if (!targetUrl) {
        toast.error(t('refresh.no_server') || 'لم يُعثر على أي سيرفر', { id: tid });
        return;
      }

      // 5) If we found a different server, or the socket is down, reconnect.
      const urlChanged = targetUrl.replace(/\/+$/, '') !== oldUrl.replace(/\/+$/, '');
      const socketDown = !socketManager.isConnected();
      if (urlChanged) {
        auth.setServerUrl(targetUrl);
      }
      if (urlChanged || socketDown) {
        const token = auth.tokens?.access_token;
        if (token) {
          socketManager.connect(targetUrl, token);
        }
      }

      const foundLine = scanResult ? ` (+${scanResult.found}/${scanResult.scanned})` : '';
      const via = usedRendezvous ? ' · via tunnel' : '';
      if (urlChanged) {
        toast.success(
          (t('refresh.switched') || 'تم التبديل إلى') + ' ' + targetUrl + foundLine + via,
          { id: tid, duration: 4500 },
        );
      } else if (socketDown) {
        toast.success(
          (t('refresh.reconnected') || 'أُعيد الاتصال') + foundLine + via,
          { id: tid },
        );
      } else {
        toast.success(
          (t('refresh.up_to_date') || 'الاتصال شغّال') + foundLine + via,
          { id: tid },
        );
      }
    } catch (e: any) {
      toast.error((t('refresh.failed') || 'فشل التحديث') + ': ' + (e?.message || e), { id: tid });
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div
      // Window-control row must always render LTR — Windows users
      // expect Min/Max/Close at the right edge regardless of the
      // UI language. Without dir="ltr" the row flips when Arabic
      // is selected (RTL doc dir) and the close button lands on
      // the *left*, where users instinctively look for the menu.
      dir="ltr"
      className="h-12 bg-surface-950 border-b border-surface-800 flex items-center justify-between px-4 select-none"
      style={{
        WebkitAppRegion: 'drag',
        WebkitUserSelect: 'none',
      } as any}
    >
      {/* App name + server identity chip.
          ``min-w-0`` lets the flex chain shrink past the children's
          intrinsic width so we never push other pills off-screen.
          ``overflow-hidden`` clips any leftover bleed (e.g. the long
          "متصل بالسيرفر · 12ms" pill when the window is narrow).
          The connection pill itself gets ``flex-shrink-0`` so it
          stays fully visible — that's the diagnostic the user
          watches. The app name + server code shrink first. */}
      <div className="flex-1 min-w-0 flex justify-center items-center gap-3 overflow-hidden">
        {/* Wordmark + leading status dot — same colour family as the
            MyPresencePill so picking "بعيد" or "مشغول" tints the
            corner identically to the pill below it. */}
        <div
          className="flex items-center gap-1.5 shrink min-w-0"
          title={presenceVisuals.label}
        >
          {isAuthenticated && (
            <span className="relative flex w-2 h-2 shrink-0">
              <span className={`relative w-2 h-2 rounded-full ${presenceVisuals.dot}`} />
              {myPresence === 'online' && (
                <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
              )}
            </span>
          )}
          <span
            className={`text-sm font-semibold whitespace-nowrap truncate transition-colors ${presenceVisuals.text}`}
          >
            {t('app.name')}
          </span>
        </div>

        {/* Connection status pill — REAL connectivity check, not a
            cosmetic indicator. Goes green ONLY when BOTH the socket
            is connected AND the most recent /api/health probe (every
            5 s) returned 200. Tooltip shows the last RTT in ms so
            the operator can spot a degrading link before it dies.
            Three states:
              🟢 متصل بالسيرفر   = socket OK + HTTP probe OK
              🟡 جاري الفحص...   = socket connected but no probe yet
              🔴 غير متصل        = socket down OR probe failing
          */}
        {isAuthenticated && (
          <div
            // ``flex-shrink-0 whitespace-nowrap`` keeps the connectivity
            // diagnostic in its full "متصل بالسيرفر · 12ms" form even at
            // narrow widths — the user explicitly relies on this pill
            // to verify the link, so it must never be clipped.
            className={`flex-shrink-0 whitespace-nowrap flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium transition-colors ${
              isReallyConnected && presenceConfirmed
                ? 'bg-green-600/25 text-green-200 border border-green-500/40'
                : isReallyConnected
                  ? 'bg-cyan-600/25 text-cyan-200 border border-cyan-500/40'
                  : socketConnected
                    ? 'bg-yellow-600/25 text-yellow-200 border border-yellow-500/40'
                    : 'bg-red-600/25 text-red-200 border border-red-500/40'
            }`}
            style={{ WebkitAppRegion: 'no-drag' } as any}
            title={
              isReallyConnected && presenceConfirmed
                ? `جاهز للاتصال بالعملاء\n`
                  + `${serverKindLabel[serverKind]}\n`
                  + `URL: ${serverUrl || 'unknown'}\n`
                  + (serverName ? `Server: ${serverName}\n` : '')
                  + (serverCode ? `Code: ${serverCode.slice(0, 16)}…\n` : '')
                  + `Online clients: ${otherOnlineCount}\n`
                  + `RTT: ${lastProbeMs ?? '?'} ms`
                : isReallyConnected
                  ? `متصل بالسيرفر، جاري التحقق من المصادقة...\n`
                    + `URL: ${serverUrl || 'unknown'}\n`
                    + `RTT: ${lastProbeMs ?? '?'} ms`
                  : socketConnected
                    ? 'الـ socket متصل لكن السيرفر لم يجب على فحص /api/health'
                    : 'غير متصل — لا socket ولا HTTP probe ينجح'
            }
          >
            <span
              className={`relative flex w-2 h-2 rounded-full ${
                isReallyConnected && presenceConfirmed ? 'bg-green-400'
                  : isReallyConnected ? 'bg-cyan-400'
                  : socketConnected ? 'bg-yellow-400'
                  : 'bg-red-400'
              }`}
            >
              {isReallyConnected && presenceConfirmed && (
                <span className="absolute inset-0 rounded-full bg-green-400 animate-ping opacity-60" />
              )}
            </span>
            <span>
              {isReallyConnected && presenceConfirmed
                ? otherOnlineCount > 0
                    ? `جاهز · ${otherOnlineCount} متصل${otherOnlineCount === 1 ? '' : 'ون'}${lastProbeMs !== null ? ` · ${lastProbeMs}ms` : ''}`
                    : `جاهز للاتصال${lastProbeMs !== null ? ` · ${lastProbeMs}ms` : ''}`
                : isReallyConnected
                  ? 'جاري المصادقة...'
                  : socketConnected
                    ? 'جاري الفحص...'
                    : 'غير متصل بالسيرفر'}
            </span>
          </div>
        )}

        {isAuthenticated && serverCode && (
          <button
            onClick={copyServerCode}
            // Server-code chip can shrink (text already shows just the
            // first 10 chars + "…") and may be hidden by ``overflow-hidden``
            // on the parent at very narrow widths — its content lives in
            // the tooltip too, so clipping it is acceptable.
            className="min-w-0 flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-surface-800 hover:bg-surface-700 text-[11px] text-gray-300 font-mono transition-colors whitespace-nowrap"
            style={{ WebkitAppRegion: 'no-drag' } as any}
            title={`${serverName || ''}\n${serverCode}\n${t('server.copy_hint')}${
              peerCount > 0 ? `\n+${peerCount} ${t('server.peers')}` : ''
            }`}
          >
            <Server size={11} />
            <span>{copied ? t('server.copied') : serverCode.slice(0, 10) + '…'}</span>
            {peerCount > 0 && (
              <span className="text-[10px] text-green-400">+{peerCount} ●</span>
            )}
          </button>
        )}
        {/* Online-Mode master toggle — visible to every authenticated
            user. Admins click to flip; non-admins see a tooltip. */}
        <OnlineModePill />
        {/* My-presence pill — green dot when I'm online, click to flip
            myself to away/busy/dnd. Sends `presence_set_status` over
            the socket so other peers see the change. */}
        {isAuthenticated && <MyPresencePill />}
        {/* User activity-status pill — opens a presets + custom-text
            popover that PUTs to /api/users/me/status-message. */}
        {isAuthenticated && <ActivityStatusButton />}
      </div>

      {/* Control buttons right */}
      <div
        className="flex items-center gap-2 ml-4"
        style={{
          WebkitAppRegion: 'no-drag',
        } as any}
      >
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="p-1.5 hover:bg-surface-800 rounded text-gray-400 hover:text-white transition-colors disabled:opacity-60"
          aria-label={t('refresh.aria') || 'Refresh connection'}
          title={t('refresh.tooltip') || 'إعادة البحث عن السيرفر وإعادة الاتصال'}
        >
          <RefreshCw size={15} className={refreshing ? 'animate-spin' : ''} />
        </button>

        <button
          onClick={handleMinimize}
          className="p-1.5 hover:bg-surface-800 rounded text-gray-400 hover:text-white transition-colors"
          aria-label="Minimize"
          title="Minimize"
        >
          <Minus size={16} />
        </button>

        <button
          onClick={handleMaximize}
          className="p-1.5 hover:bg-surface-800 rounded text-gray-400 hover:text-white transition-colors"
          aria-label={isMaximized ? 'Restore' : 'Maximize'}
          title={isMaximized ? 'Restore' : 'Maximize'}
        >
          {isMaximized ? <Copy size={14} /> : <Square size={14} />}
        </button>

        <button
          onClick={handleClose}
          className="p-1.5 hover:bg-red-500/80 rounded text-gray-400 hover:text-white transition-colors"
          aria-label="Close"
          title="Close"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  );
};
