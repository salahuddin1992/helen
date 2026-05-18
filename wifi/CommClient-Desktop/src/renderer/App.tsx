/**
 * App root — routing, auth guard, global listeners, call overlay, notifications.
 *
 * Integration points:
 *   - AppBootstrapScreen → one-click startup orchestrator (splash → backend → discover → restore)
 *   - Auth flow → AppBootstrap → v2 engines (CallEngine, MessagingEngine)
 *   - IntegrationBridge → cross-module event coordination
 *   - useAppListeners → keyboard shortcuts, read receipts, call suppression
 *   - Notification store → unread badges, desktop notifications
 *   - Connection overlay → offline/reconnecting banner
 *
 * Startup Flow:
 *   Returning user: Splash → Backend Check → Discovery → Session Restore → Ready (0 clicks)
 *   First-time user: Splash → Backend Check → Discovery → Onboarding (3 steps) → Ready
 *   Error:           Splash → Backend Check → Error → Auto-retry → ...
 */
import React, { useEffect, useState, useCallback, lazy, Suspense } from 'react';
import { HashRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';

// Stores — v2 for call and chat (engine-powered)
import { useAuthStore } from '@/stores/auth.store';
import { useCallStore } from '@/stores/call.store.v2';
import { useChatStore } from '@/stores/chat.store.v2';
import { useSettingsStore } from '@/stores/settings.store';
import { useNotificationStore } from '@/stores/notification.store';
import { useAppStore } from '@/stores/app.store';
import { setLanguage } from '@/i18n';

// Services
import { socketManager } from '@/services/socket.manager';
import { GlobalShortcutsMount } from '@/components/shortcuts/GlobalShortcutsMount';
import { IntegrationBridge } from '@/services/IntegrationBridge';
import { AppLogger } from '@/services/AppLogger';

// Hooks
import { useAppListeners } from '@/hooks/useAppListeners';

// Layout
import { MainLayout } from '@/components/layout/MainLayout';
import { TitleBar } from '@/components/layout/TitleBar';

// Auth
import { LoginForm } from '@/components/auth/LoginForm';
import { RegisterForm } from '@/components/auth/RegisterForm';

// Startup
import AppBootstrapScreen from '@/components/startup/AppBootstrapScreen';

// Pages (inside layout)
import ChatView from '@/components/chat/ChatView';
import ContactList from '@/components/contacts/ContactList';
import { CallHistoryPage } from '@/pages/CallHistoryPage';
import SettingsView from '@/components/settings/SettingsView';
import GroupManager from '@/components/groups/GroupManager';
import NotificationCenter from '@/pages/NotificationCenter';
// AdminPanel is large (12 sub-panels, ~30 KB minified) and only relevant
// for admin-role users. Lazy-load it so the main bundle stays lean for
// the 99% of users who never see this UI. The panel itself enforces a
// role gate on mount and the server gates every endpoint, so deferred
// load is purely a performance optimization, not a security boundary.
const AdminPanel = lazy(() => import('@/components/admin/AdminPanel'));
import WhiteboardPage from '@/pages/WhiteboardPage';
import SavedMessagesPage from '@/pages/SavedMessagesPage';
import CalendarPage from '@/pages/CalendarPage';
import GlobalSearch from '@/components/common/GlobalSearch';
import KeyboardShortcuts from '@/components/common/KeyboardShortcuts';
import Lightbox from '@/components/common/Lightbox';

// Overlays
import CallView from '@/components/call/CallView';
import IncomingCall from '@/components/call/IncomingCall';
import CallEndedToast from '@/components/call/CallEndedToast';
import CallSummary from '@/components/call/CallSummary';
import PreJoinScreen from '@/components/call/PreJoinScreen';
import { DebugCallPanel } from '@/components/dev/DebugCallPanel';

const log = AppLogger.create('App');

// ── Configure logger based on environment ──────────
if (window.electronAPI?.isDev) {
  window.electronAPI.isDev().then((isDev: boolean) => {
    AppLogger.setLevel(isDev ? 'DEBUG' : 'INFO');
  });
}

// ── Auth Guard ─────────────────────────────────────
const AuthGuard: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
};

// ── Connection Status Tracker ──────────────────────
//
// Disconnected used to render as a single opaque banner. That hid the
// real failure mode (split-brain, expired token, wrong server, missing
// origin allowlist) and forced users to file bug reports rather than
// self-diagnose. Now: click the banner and we fetch GET /api/connection/
// diagnostics + the central client config and surface every signal in
// the chain so the user (or support) can pinpoint the exact failure.
type DiagPayload = {
  serverReachable: boolean;
  serverInfo?: { server_id?: string; lan_ip?: string; port?: number; online_users?: number; client_ip?: string };
  authValid: boolean | null;
  authError: string | null;
  user: { id: string; username: string; role: string } | null;
  userOnline: boolean | null;
  socketCount: number | null;
  sessionCount: number | null;
};

const ConnectionTracker: React.FC = () => {
  const [isConnected, setIsConnected] = useState(true);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [diag, setDiag] = useState<DiagPayload | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [clientConfig, setClientConfig] = useState<{ mode: string; serverUrl: string; allowEmbeddedServer: boolean; allowLanDiscovery: boolean; allowAutoServerSwitch: boolean; deviceId: string } | null>(null);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const tokens = useAuthStore((s) => (s as any).tokens);
  const serverUrl = useAuthStore((s) => (s as any).serverUrl);

  useEffect(() => {
    if (!isAuthenticated) return;

    const unsubConnect = socketManager.on('connect', () => {
      setIsConnected(true);
      setIsReconnecting(false);
      log.info('Socket connected');
    });

    const unsubDisconnect = socketManager.on('disconnect', () => {
      setIsConnected(false);
      setIsReconnecting(true);
      log.warn('Socket disconnected');
    });

    const unsubReconnectFailed = socketManager.on('reconnect_failed', () => {
      setIsReconnecting(false);
      log.error('Socket reconnection failed');
    });

    setIsConnected(socketManager.isConnected());

    return () => {
      unsubConnect();
      unsubDisconnect();
      unsubReconnectFailed();
    };
  }, [isAuthenticated]);

  // Pull config + structured diagnostics whenever the user expands the panel.
  useEffect(() => {
    if (!showDetails) return;
    let cancelled = false;
    (async () => {
      try {
        const cfg = await (window as any).electronAPI?.getClientConfig?.();
        if (!cancelled && cfg) setClientConfig(cfg);
      } catch { /* preload may not expose it in older builds */ }

      const url = serverUrl || (clientConfig?.serverUrl) || 'http://127.0.0.1:3000';
      const headers: Record<string, string> = {};
      if (tokens?.access_token) headers['Authorization'] = `Bearer ${tokens.access_token}`;
      try {
        const res = await fetch(`${url}/api/connection/diagnostics`, { headers });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) { setDiag(data); setDiagError(null); }
      } catch (err: any) {
        if (!cancelled) { setDiag(null); setDiagError(err?.message || String(err)); }
      }
    })();
    return () => { cancelled = true; };
  }, [showDetails, serverUrl, tokens?.access_token, clientConfig?.serverUrl]);

  if (!isAuthenticated || isConnected) return null;

  const Row = ({ label, ok, value }: { label: string; ok: boolean | null; value: React.ReactNode }) => (
    <div className="flex justify-between gap-4 py-0.5">
      <span className="opacity-80">{label}</span>
      <span className={ok === false ? 'text-red-300' : ok ? 'text-green-300' : 'opacity-70'}>
        {value}
      </span>
    </div>
  );

  return (
    <>
      <button
        onClick={() => setShowDetails((v) => !v)}
        className={`fixed top-8 left-0 right-0 z-50 py-1.5 text-xs font-medium transition-all ${
          isReconnecting ? 'bg-yellow-600/90 text-yellow-100' : 'bg-red-600/90 text-red-100'
        }`}
      >
        {isReconnecting ? '⟳ Reconnecting to server — click for diagnostics' : '✕ Connection lost — click for diagnostics'}
      </button>
      {showDetails && (
        <div className="fixed top-16 right-4 z-50 w-96 max-h-[70vh] overflow-auto rounded-md border border-white/10 bg-zinc-900/95 p-3 text-xs text-zinc-100 shadow-2xl">
          <div className="mb-2 flex items-center justify-between">
            <span className="font-semibold">Connection diagnostics</span>
            <button onClick={() => setShowDetails(false)} className="opacity-60 hover:opacity-100">✕</button>
          </div>
          {diagError && <div className="mb-2 text-red-300">Failed to fetch /api/connection/diagnostics: {diagError}</div>}
          {clientConfig && (
            <div className="mb-2 border-b border-white/10 pb-2">
              <Row label="Client mode" ok={null} value={clientConfig.mode} />
              <Row label="Configured serverUrl" ok={null} value={clientConfig.serverUrl} />
              <Row label="Allow embedded" ok={null} value={String(clientConfig.allowEmbeddedServer)} />
              <Row label="Allow LAN discovery" ok={null} value={String(clientConfig.allowLanDiscovery)} />
              <Row label="Allow auto-switch" ok={null} value={String(clientConfig.allowAutoServerSwitch)} />
            </div>
          )}
          {diag && (
            <div>
              <Row label="Server reachable" ok={diag.serverReachable} value={diag.serverReachable ? 'yes' : 'no'} />
              <Row label="Server" ok={null} value={`${diag.serverInfo?.lan_ip}:${diag.serverInfo?.port}`} />
              <Row label="Server ID" ok={null} value={diag.serverInfo?.server_id?.slice(0, 12) + '…'} />
              <Row label="Online users" ok={null} value={String(diag.serverInfo?.online_users ?? 0)} />
              <Row label="Auth valid" ok={diag.authValid} value={diag.authValid === null ? 'no token sent' : diag.authValid ? 'yes' : (diag.authError || 'no')} />
              <Row label="User on this server" ok={diag.user ? true : false} value={diag.user ? `${diag.user.username} (${diag.user.role})` : 'not found'} />
              <Row label="User online" ok={diag.userOnline} value={diag.userOnline ? 'yes' : 'no'} />
              <Row label="Active sockets" ok={null} value={String(diag.socketCount ?? 0)} />
              <Row label="Active sessions" ok={null} value={String(diag.sessionCount ?? 0)} />
              <Row label="Your client IP (as server sees)" ok={null} value={diag.serverInfo?.client_ip || 'unknown'} />
            </div>
          )}
          <div className="mt-3 flex gap-2">
            <button
              onClick={() => socketManager.connect((serverUrl || clientConfig?.serverUrl)!, tokens?.access_token)}
              className="rounded bg-blue-600 px-2 py-1 text-xs hover:bg-blue-500"
            >
              Retry socket
            </button>
            <button
              onClick={() => { setDiag(null); setDiagError(null); setShowDetails(false); setTimeout(() => setShowDetails(true), 50); }}
              className="rounded bg-zinc-700 px-2 py-1 text-xs hover:bg-zinc-600"
            >
              Refresh
            </button>
          </div>
        </div>
      )}
    </>
  );
};

// Pre-join mount — listens for ``preJoinIntent`` on the call store
// and renders the device-preview screen. Cleared when the user
// confirms (and the engine takes over) or cancels.
const PreJoinMount: React.FC = () => {
  const intent = useCallStore((s) => s.preJoinIntent);
  const clear = useCallStore((s) => s.clearPreJoin);
  if (!intent) return null;
  return (
    <PreJoinScreen
      intent={intent as any}
      title={(intent as any).title}
      onCancel={() => {
        // For an incoming call we should also reject if the user
        // bails on pre-join — otherwise the caller keeps ringing.
        if (intent.kind === 'accept') {
          try { useCallStore.getState().rejectCall(); } catch { /* ignore */ }
        }
        clear();
      }}
      onJoined={clear}
    />
  );
};

// ── Main App Content ───────────────────────────────
const AppContent: React.FC = () => {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const userId = useAuthStore((s) => s.user?.id);
  const settings = useSettingsStore((s) => s.settings);
  const loadSettings = useSettingsStore((s) => s.load);
  const callStatus = useCallStore((s) => s.status);
  const incomingCall = useCallStore((s) => s.incomingCall);
  const appPhase = useAppStore((s) => s.phase);

  // Track if the one-click bootstrap has completed
  const [bootstrapDone, setBootstrapDone] = useState(false);

  // Global cross-module listeners (keyboard shortcuts, read receipts, etc.)
  useAppListeners();

  // Load settings early (during splash)
  useEffect(() => {
    loadSettings();
  }, []);

  // Wire Android heads-up notification → call store. When the user taps
  // Accept/Decline on the native incoming-call notification, the receiver
  // fires a `helen://call/<decision>` deep link; this listener routes it
  // to the existing call store actions. No-op on desktop (the listener
  // returns an empty unsubscribe function on platforms without the bridge).
  useEffect(() => {
    const off = window.electronAPI?.call?.onIncomingDecision?.((d) => {
      const store = useCallStore.getState();
      if (d.decision === 'accept')  void store.acceptCall();
      if (d.decision === 'decline')      store.rejectCall();
    });
    return () => { try { off?.(); } catch { /* ignore */ } };
  }, []);

  // Register the self-managed Telecom PhoneAccount once at boot so Helen
  // calls show up in the OS audio focus, Bluetooth controls, hold-on-GSM
  // collision, Android Auto, and Wear OS routing. No-op on desktop / web
  // and on Android < 8.0 (where Telecom self-management isn't available).
  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const support = await window.electronAPI?.connection?.isSupported?.();
        if (!support?.supported || !mounted) return;
        await window.electronAPI?.connection?.registerPhoneAccount?.();
      } catch { /* best-effort */ }
    })();
    return () => { mounted = false; };
  }, []);

  // Subscribe to Telecom Connection events (answer / reject / disconnect /
  // hold / unhold / audio-route). Routes them to the call store so the OS
  // can drive the call lifecycle the same way the in-app UI does.
  useEffect(() => {
    const off = window.electronAPI?.connection?.onTelecomEvent?.((e) => {
      const store = useCallStore.getState();
      switch (e.event) {
        case 'answer':     void store.acceptCall(); break;
        case 'reject':     store.rejectCall();      break;
        case 'disconnect':
        case 'abort':      store.hangup();          break;
        // hold / unhold / audio-route changes are observed but not yet
        // mirrored to the WebRTC tracks — left here as an extension hook.
      }
    });
    return () => { try { off?.(); } catch { /* ignore */ } };
  }, []);

  // App Shortcuts (long-press launcher) → focus the relevant tab.
  // The shortcut id can be 'chats', 'contacts', 'calls', 'settings'
  // depending on the OS launcher entry the user picked. We dispatch a
  // custom event AND directly route here — previously the dispatch
  // was an orphan event with no listener, so launching via long-press
  // silently did nothing.
  const _navigateToShortcut = useNavigate();
  useEffect(() => {
    const off = window.electronAPI?.shortcuts?.onShortcut?.((id) => {
      window.dispatchEvent(new CustomEvent('helen:shortcut', { detail: id }));
      switch (id) {
        case 'chats':     _navigateToShortcut('/chats'); break;
        case 'contacts':  _navigateToShortcut('/contacts'); break;
        case 'calls':     _navigateToShortcut('/calls'); break;
        case 'settings':  _navigateToShortcut('/settings'); break;
        case 'new-dm':    window.dispatchEvent(new CustomEvent('helen:open-new-dm')); break;
        case 'new-group': window.dispatchEvent(new CustomEvent('helen:open-new-group')); break;
      }
    });
    return () => { try { off?.(); } catch { /* ignore */ } };
  }, [_navigateToShortcut]);

  // Apply language
  useEffect(() => {
    setLanguage(settings.language);
    document.documentElement.dir = settings.language === 'ar' ? 'rtl' : 'ltr';
    document.documentElement.lang = settings.language;
  }, [settings.language]);

  // Best-effort hangup when the window closes mid-call.
  //
  // Without this, the server only notices the departure when its
  // 30-second orphan sweep observes the dead Socket.IO connection,
  // and other peers see a frozen tile until then. We try two things
  // in parallel:
  //   1. socketManager.emitNoAck — Socket.IO usually flushes the
  //      buffered frame before the WebSocket closes.
  //   2. fetch(..., { keepalive: true }) — the modern replacement
  //      for sendBeacon, which supports Authorization headers so we
  //      can authenticate the request even during teardown.
  //
  // Both are fire-and-forget; whichever arrives first wins. The
  // server endpoint is idempotent.
  useEffect(() => {
    if (!isAuthenticated) return;

    const handler = () => {
      const cs = useCallStore.getState();
      if (cs.status !== 'active' || !cs.callId) return;

      try { cs.hangup(); } catch { /* ignore */ }

      try {
        const tokens = (useAuthStore.getState() as any).tokens;
        const serverUrl = (useAuthStore.getState() as any).serverUrl;
        if (tokens?.access_token && serverUrl) {
          void fetch(
            `${serverUrl}/api/calls/${encodeURIComponent(cs.callId)}/leave-on-close`,
            {
              method: 'POST',
              keepalive: true,
              headers: { Authorization: `Bearer ${tokens.access_token}` },
            },
          ).catch(() => { /* fire-and-forget */ });
        }
      } catch { /* ignore */ }
    };

    window.addEventListener('beforeunload', handler);
    window.addEventListener('pagehide', handler);
    return () => {
      window.removeEventListener('beforeunload', handler);
      window.removeEventListener('pagehide', handler);
    };
  }, [isAuthenticated]);

  // Start IntegrationBridge when authenticated
  useEffect(() => {
    if (!isAuthenticated || !userId) return;

    log.info('User authenticated — starting IntegrationBridge');
    IntegrationBridge.start(userId);

    // Fetch initial notification count
    useNotificationStore.getState().fetchUnreadCount();
    useNotificationStore.getState().fetchNotifications();

    return () => {
      log.info('User deauthenticated — stopping IntegrationBridge');
      IntegrationBridge.stop();
    };
  }, [isAuthenticated, userId]);

  // ── Bootstrap Complete Handlers ─────────────────
  const handleBootstrapReady = useCallback(() => {
    log.info('Bootstrap complete — app is ready');
    setBootstrapDone(true);
  }, []);

  const handleBootstrapGoToLogin = useCallback(() => {
    log.info('Bootstrap → redirecting to login');
    setBootstrapDone(true);
  }, []);

  // ── Show Bootstrap Screen Until Done ────────────
  // The bootstrap orchestrator handles: splash → backend → discovery → session restore
  // Once it completes, it either auto-authenticates (returning user) or shows login
  if (!bootstrapDone) {
    return (
      <div className="h-screen w-screen flex flex-col bg-surface-900 text-white overflow-hidden">
        <TitleBar />
        <div className="flex-1 overflow-auto">
          <AppBootstrapScreen
            onReady={handleBootstrapReady}
            onGoToLogin={handleBootstrapGoToLogin}
          />
        </div>
      </div>
    );
  }

  // Wrap unauthenticated auth screens with TitleBar so users always have
  // window controls (close/min/max) — even when not logged in.
  const withTitleBar = (child: React.ReactNode) => (
    <div className="h-screen w-screen flex flex-col bg-surface-900 text-white overflow-hidden">
      <TitleBar />
      <div className="flex-1 overflow-auto">{child}</div>
    </div>
  );

  return (
    <>
      {/* Connection status banner */}
      <ConnectionTracker />

      {/* Global keyboard-shortcut listeners (toggle_mute, end_call,
          jump_to_unread, mark_all_read, new_dm, new_group, etc.).
          Mounts once at the app root so listeners are alive even
          when no chat view is on screen. */}
      {isAuthenticated && <GlobalShortcutsMount />}

      <Routes>
        {/* Auth routes */}
        <Route path="/login" element={
          isAuthenticated ? <Navigate to="/chats" replace /> : withTitleBar(<LoginForm />)
        } />
        <Route path="/register" element={
          isAuthenticated ? <Navigate to="/chats" replace /> : withTitleBar(<RegisterForm />)
        } />

        {/* App routes inside layout */}
        <Route path="/" element={
          <AuthGuard><MainLayout /></AuthGuard>
        }>
          <Route index element={<Navigate to="/chats" replace />} />
          <Route path="chats" element={<ChatView />} />
          <Route path="contacts" element={<ContactList />} />
          <Route path="calls" element={<CallHistoryPage />} />
          <Route path="groups" element={<GroupManager />} />
          <Route path="notifications" element={<NotificationCenter />} />
          <Route path="settings" element={<SettingsView />} />
          <Route
            path="admin"
            element={
              <Suspense fallback={<div className="p-8 text-center text-gray-400">Loading admin…</div>}>
                <AdminPanel />
              </Suspense>
            }
          />
          <Route path="whiteboard/:id" element={<WhiteboardPage />} />
          <Route path="saved" element={<SavedMessagesPage />} />
          <Route path="calendar" element={<CalendarPage />} />
        </Route>

        {/* Fallback — authenticated goes to chats, unauthenticated goes to login */}
        <Route path="*" element={
          isAuthenticated ? <Navigate to="/chats" replace /> : <Navigate to="/login" replace />
        } />
      </Routes>

      {/* Global overlays — always rendered on top.
          Render the call UI during 'reconnecting' too, otherwise the
          tiles vanish on every transient disconnect and users assume
          the call dropped. */}
      {(callStatus === 'active' || callStatus === 'reconnecting') && <CallView />}
      {incomingCall && <IncomingCall />}
      {/* Pre-join screen — camera/mic test before actually joining. */}
      <PreJoinMount />
      {/* Post-call toast explaining why the call ended. */}
      <CallEndedToast />
      {/* Post-call summary modal — duration, participants, transcript. */}
      <CallSummary />

      {/* Global search — Ctrl+K. Self-mounts a hidden listener and only
          renders the modal when triggered, so the cost when closed is just
          one keydown handler. */}
      {isAuthenticated && <GlobalSearch />}

      {/* Keyboard shortcuts modal — opens on ? (Shift+/) or Ctrl+/. */}
      <KeyboardShortcuts />

      {/* Image lightbox — listens for openLightbox() events. */}
      <Lightbox />

      {/* Dev-only diagnostic surface. Toggle with Ctrl+Shift+D. */}
      <DebugCallPanel />
    </>
  );
};

// ── Root App ───────────────────────────────────────
const App: React.FC = () => {
  return (
    <HashRouter>
      <AppContent />
      {/* Global toast container — required by react-hot-toast.
          Without this mounted, every `toast.success/.error/...` call
          across the app silently no-ops, AND goober (the CSS-in-JS
          engine react-hot-toast uses) injects its keyframe + container
          rules into the document with no consumer, occasionally
          surfacing as raw CSS text under the title bar. Mounting the
          Toaster here gives goober a target and lets toast feedback
          actually appear. */}
      <Toaster
        position="top-center"
        toastOptions={{
          duration: 4000,
          style: {
            background: '#1e293b',     // surface-800
            color: '#f1f5f9',          // text-100
            border: '1px solid #334155',
            fontSize: '13px',
            direction: 'rtl',
          },
          success: { iconTheme: { primary: '#22c55e', secondary: '#0f172a' } },
          error:   { iconTheme: { primary: '#ef4444', secondary: '#0f172a' } },
        }}
      />
    </HashRouter>
  );
};

export default App;
