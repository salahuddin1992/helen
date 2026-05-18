/**
 * AppBootstrapScreen.tsx — Master startup orchestrator.
 *
 * Drives the entire startup sequence from cold boot to fully operational.
 * This component owns the state machine transitions and renders the
 * appropriate screen for each phase.
 *
 * Lifecycle:
 *   1. splash (1.2s min) → brand impression
 *   2. backend_check → HTTP health check on port 3000
 *   3. discovery → auto-discover LAN server via UDP/mDNS
 *   4. session_restore → try to log in with saved credentials
 *   5. onboarding (first-run) OR login (returning, expired) OR ready
 *
 * Returning user with valid session: splash → check → discover → restore → ready
 *   Total time: ~2-3 seconds, zero clicks.
 *
 * First-time user: splash → check → discover → onboarding (3 steps)
 *   Total time: ~30 seconds, 2 screens.
 */

import React, { useEffect, useRef, useCallback, useState } from 'react';
import { useAppStore } from '@/stores/app.store';
import { useAuthStore } from '@/stores/auth.store';
import { useDiscoveryStore } from '@/stores/discovery.store';
import SplashScreen from './SplashScreen';
import OnboardingWizard from './OnboardingWizard';
import ErrorRecovery from './ErrorRecovery';
import { runAutoConnect, AutoConnectStepEvent } from '@/services/autoConnect';

// ── Constants ────────────────────────────────────────────

const SPLASH_MIN_MS = 1200;         // Minimum splash display time
const BACKEND_CHECK_TIMEOUT = 8000; // How long to wait for backend
const DISCOVERY_TIMEOUT = 6000;     // How long to wait for server discovery
const HEALTH_POLL_INTERVAL = 800;   // Health check poll interval

// ── Orchestrator Component ───────────────────────────────

interface AppBootstrapScreenProps {
  onReady: () => void;
  onGoToLogin: () => void;
}

const AppBootstrapScreen: React.FC<AppBootstrapScreenProps> = ({ onReady, onGoToLogin }) => {
  const {
    phase,
    error,
    errorMessage,
    retryCount,
    maxRetries,
    isFirstRun,
    serverUrl,
    setPhase,
    setError,
    clearError,
    setSplashElapsed,
    setBackendHealthy,
    setServerUrl,
    setHasSession,
    setIsFirstRun,
    incrementRetry,
    resetRetries,
    transitionToNextPhase,
  } = useAppStore();

  const { restoreSession, isAuthenticated, setServerUrl: setAuthServerUrl, rendezvousUrl } = useAuthStore();
  const { startSearching, stopSearching, bestServer, phase: discoveryPhase } = useDiscoveryStore();

  // Live status of the forced search chain — surfaced in the UI so the
  // user sees exactly what we're trying right now ("searching LAN…",
  // "trying TCP scan…", etc.). Stays empty until the discovery phase
  // actually runs.
  const [searchSteps, setSearchSteps] = useState<AutoConnectStepEvent[]>([]);
  const [searchAttempt, setSearchAttempt] = useState(0);
  // When strict-mode config is detected drifting from the renderer's
  // saved serverUrl, surface a one-line banner so the operator knows
  // their config.json overrides what the app remembers.
  const [strictDriftBanner, setStrictDriftBanner] = useState<string | null>(null);

  const isMounted = useRef(true);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  useEffect(() => {
    isMounted.current = true;
    return () => { isMounted.current = false; };
  }, []);

  // ── Phase: splash ──────────────────────────────────────

  useEffect(() => {
    if (phase !== 'splash') return;

    // Central client config wins over everything else — it's the single
    // source of truth for serverUrl in production. Falls back to the
    // legacy localStorage path only if config is unavailable (e.g. older
    // builds without the IPC handler).
    //
    // Strict mode (allowAutoServerSwitch=false) means we MUST NOT
    // overwrite localStorage on top of the config — otherwise a stale
    // saved URL keeps re-asserting itself the next time the user boots
    // with config.json absent (e.g. portable mode). Honor the config
    // *exclusively* and skip localStorage writes.
    const applyConfigUrl = (url: string, strict: boolean) => {
      setServerUrl(url);
      setAuthServerUrl(url);
      if (!strict) {
        try { localStorage.setItem('commclient_server_url', url); } catch {}
      }
    };

    if (window.electronAPI?.getClientConfig) {
      window.electronAPI.getClientConfig().then((cfg) => {
        if (!isMounted.current) return;
        if (cfg && typeof cfg.serverUrl === 'string' && /^https?:\/\//.test(cfg.serverUrl)) {
          const strict = cfg.allowAutoServerSwitch === false;
          // Detect drift between config.json and the renderer's last
          // remembered URL — surface it as a small banner so the
          // operator notices their config has overridden what the app
          // had cached. Without this, the override is invisible.
          try {
            const stale = localStorage.getItem('commclient_server_url');
            if (stale && stale !== cfg.serverUrl) {
              const msg = `Config override: ${cfg.serverUrl} (was ${stale})`;
              console.warn('[Bootstrap] strict-mode drift —', msg);
              if (strict) setStrictDriftBanner(msg);
            }
          } catch { /* ignore drift detection failure */ }
          applyConfigUrl(cfg.serverUrl, strict);
          return;
        }
        // Config didn't yield a URL — fall through to legacy detection.
        legacyDetect();
      }).catch(() => legacyDetect());
    } else {
      legacyDetect();
    }

    function legacyDetect() {
      let savedUrl = localStorage.getItem('commclient_server_url');
      if (savedUrl && savedUrl.includes('://localhost')) {
        savedUrl = savedUrl.replace('://localhost', '://127.0.0.1');
        try { localStorage.setItem('commclient_server_url', savedUrl); } catch {}
      }
      if (savedUrl) {
        setServerUrl(savedUrl);
      } else if (window.electronAPI?.getServerPort) {
        window.electronAPI.getServerPort().then((port: number) => {
          if (port && isMounted.current) {
            const url = `http://127.0.0.1:${port}`;
            setServerUrl(url);
            setAuthServerUrl(url);
          }
        }).catch(() => {});
      }
    }

    const timer = setTimeout(() => {
      if (isMounted.current) {
        setSplashElapsed();
        transitionToNextPhase();
      }
    }, SPLASH_MIN_MS);

    return () => clearTimeout(timer);
  }, [phase]);

  // ── Phase: backend_check ───────────────────────────────

  useEffect(() => {
    if (phase !== 'backend_check') return;

    let cancelled = false;
    let attempts = 0;
    const startTime = Date.now();

    const checkBackend = async () => {
      if (cancelled) return;

      // Build the probe list. With central client config, serverUrl is
      // canonical and we ONLY probe that one URL — no fallback to other
      // ports. Falling back is what created the 3000/3001 split-brain:
      // the renderer would silently end up on a different server than
      // the admin, and the user would see "disconnected" with no clue.
      //
      // Legacy fallback path (no config available, e.g. old build) keeps
      // the rescue probes so the user isn't stranded.
      const probes: string[] = [];
      let strictMode = false;
      try {
        const cfg = await window.electronAPI?.getClientConfig?.();
        if (cfg && cfg.serverUrl) {
          probes.push(cfg.serverUrl);
          strictMode = !cfg.allowAutoServerSwitch;
        }
      } catch { /* fall through to legacy */ }

      if (!strictMode) {
        if (serverUrl && !probes.includes(serverUrl)) probes.push(serverUrl);
        for (const p of ['http://127.0.0.1:3000', 'http://127.0.0.1:3001', 'http://127.0.0.1:3088']) {
          if (!probes.includes(p)) probes.push(p);
        }
      } else if (serverUrl && !probes.includes(serverUrl)) {
        // Strict mode but renderer somehow has a different serverUrl —
        // probe both so we can detect drift and log it.
        probes.push(serverUrl);
      }

      for (const checkUrl of probes) {
        if (cancelled) return;
        try {
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), 3000);

          const res = await fetch(`${checkUrl}/api/health`, {
            signal: controller.signal,
          });
          clearTimeout(timeoutId);

          if (res.ok && !cancelled) {
            // Persist whichever URL actually worked — next boot skips the
            // probe list and goes straight to this address. Skip the
            // write in strict mode: config.serverUrl is canonical, we
            // must not let a fallback URL override it on next boot.
            if (!serverUrl || serverUrl !== checkUrl) {
              setServerUrl(checkUrl);
              setAuthServerUrl(checkUrl);
              if (!strictMode) {
                try { localStorage.setItem('commclient_server_url', checkUrl); } catch {}
              }
            }
            setBackendHealthy(true);
            transitionToNextPhase();
            return;
          }
        } catch {
          // Try the next probe.
        }
      }

      attempts++;
      if (Date.now() - startTime > BACKEND_CHECK_TIMEOUT) {
        if (!cancelled) {
          // No local backend reachable on the probe list. Don't surface an
          // error yet — fall through to the discovery phase, which runs the
          // full LAN orchestrator (UDP 41234, mDNS, TCP scan, rendezvous).
          // This is the path that lets a CLIENT_ONLY install (Computer B)
          // find the master Helen-Server running on Computer A. The
          // discovery phase will surface 'no_server_found' if it also
          // exhausts every option.
          console.warn('[Bootstrap] localhost probes exhausted — escalating to LAN discovery');
          transitionToNextPhase();
        }
        return;
      }

      // Poll again
      if (!cancelled) {
        setTimeout(checkBackend, HEALTH_POLL_INTERVAL);
      }
    };

    checkBackend();
    return () => { cancelled = true; };
  }, [phase, serverUrl]);

  // ── Phase: discovery — FORCED auto-connect chain ───────
  //
  // Don't let the user past this point until we've found a Helen
  // server somewhere. Run the full chain (local → saved → LAN orch →
  // TCP scan → rendezvous) on a loop with backoff. If all paths fail,
  // surface the error UI but keep retrying in the background. The user
  // can still escape via "Manual URL" in ErrorRecovery, but we never
  // simply give up.
  useEffect(() => {
    if (phase !== 'discovery') return;

    let cancelled = false;

    // In production, central config disables LAN discovery entirely. The
    // serverUrl from config is the only allowed connection target — if it
    // didn't respond during backend_check, we surface a clear error rather
    // than scanning the LAN and silently switching to a different host
    // (which is what created the split-brain in the first place).
    (async () => {
      try {
        const cfg = await window.electronAPI?.getClientConfig?.();
        if (cfg && cfg.allowLanDiscovery === false) {
          console.warn('[Bootstrap] LAN discovery disabled by config — surfacing connection error.');
          setError('backend_unreachable',
            `The configured server (${cfg.serverUrl}) is not responding. ` +
            `Edit %APPDATA%/CommClient/config.json or start Helen-Server on that address.`
          );
          return;
        }
      } catch { /* fall through to discovery loop */ }
      runDiscoveryLoop();
    })();

    function runDiscoveryLoop() {
    // Kick the legacy UDP/mDNS service alongside the chain so its live
    // server list also fills (other UI consumes useDiscoveryStore).
    startSearching();

    // Stop hammering the LAN forever — after this many failed rounds,
    // surface a hard error and stop the loop. The user can still retry
    // (handleRetry → setPhase('backend_check')) which restarts the
    // chain from scratch, OR enter a manual URL via ErrorRecovery.
    const MAX_DISCOVERY_ATTEMPTS = 10;

    const loop = async () => {
      let attempt = 0;
      while (!cancelled && phaseRef.current === 'discovery') {
        attempt++;
        if (!cancelled) setSearchAttempt(attempt);
        if (!cancelled) setSearchSteps([]);

        const result = await runAutoConnect({
          savedUrl: serverUrl || localStorage.getItem('commclient_server_url'),
          rendezvousUrl,
          onStep: (ev) => {
            if (cancelled) return;
            // Replace any prior event for this step id, then push new.
            setSearchSteps((prev) => {
              const next = prev.filter((p) => p.id !== ev.id);
              next.push(ev);
              return next;
            });
          },
        });

        if (cancelled) return;

        if (result.ok && result.url) {
          setServerUrl(result.url);
          setAuthServerUrl(result.url);
          try { localStorage.setItem('commclient_server_url', result.url); } catch {}
          stopSearching();
          transitionToNextPhase();
          return;
        }

        // No server found this round. Surface the error UI on the first
        // total miss so the user can configure a custom URL if they
        // want. Backoff: 5s, 10s, 20s, 30s, 30s… so we don't hammer.
        if (attempt === 1) setError('no_server_found');
        if (attempt >= MAX_DISCOVERY_ATTEMPTS) {
          // Ceiling reached — stop the loop. The error UI is already
          // up; the user retries via the Retry button (full reset) or
          // enters a manual URL via the Manual Connect form. Both
          // paths re-enter discovery from a known state.
          console.warn('[Bootstrap] discovery ceiling hit — stopping loop');
          stopSearching();
          return;
        }
        const backoff = Math.min(5_000 * Math.pow(2, attempt - 1), 30_000);
        await new Promise((r) => setTimeout(r, backoff));
      }
    };
    loop();
    } // end runDiscoveryLoop

    return () => { cancelled = true; stopSearching(); };
  }, [phase, serverUrl, rendezvousUrl]);

  // ── Phase: session_restore ─────────────────────────────

  useEffect(() => {
    if (phase !== 'session_restore') return;

    let cancelled = false;

    const tryRestore = async () => {
      try {
        const restored = await restoreSession();

        if (cancelled) return;

        if (restored) {
          setHasSession(true);
          transitionToNextPhase();
        } else {
          setHasSession(false);
          transitionToNextPhase();
        }
      } catch {
        if (!cancelled) {
          setHasSession(false);
          transitionToNextPhase();
        }
      }
    };

    tryRestore();
    return () => { cancelled = true; };
  }, [phase]);

  // ── Phase: ready → notify parent ──────────────────────

  useEffect(() => {
    if (phase === 'ready') {
      onReady();
    }
  }, [phase]);

  // ── Phase: login → notify parent ──────────────────────

  useEffect(() => {
    if (phase === 'login') {
      onGoToLogin();
    }
  }, [phase]);

  // ── Retry Handler ──────────────────────────────────────

  const handleRetry = useCallback(() => {
    incrementRetry();
    clearError();
    setPhase('backend_check');
  }, []);

  // ── Manual Connect Handler ─────────────────────────────

  const handleManualConnect = useCallback(async (url: string) => {
    try {
      const normalized = url.replace(/\/+$/, '');
      const res = await fetch(`${normalized}/api/health`, {
        signal: AbortSignal.timeout(3000),
      });
      if (res.ok) {
        setServerUrl(normalized);
        setBackendHealthy(true);
        try { localStorage.setItem('commclient_server_url', normalized); } catch {}
        clearError();
        // Go to session_restore or onboarding
        if (isFirstRun) {
          setPhase('onboarding');
        } else {
          setPhase('session_restore');
        }
      } else {
        setError('backend_unreachable', 'Server responded but is not healthy.');
      }
    } catch {
      setError('no_server_found', 'Could not connect to that address.');
    }
  }, [isFirstRun]);

  // ── Onboarding Complete Handler ────────────────────────

  const handleOnboardingComplete = useCallback(() => {
    setIsFirstRun(false);
    setHasSession(true);
    setPhase('ready');
  }, []);

  // ── Session Expired → Login ────────────────────────────

  const handleGoToLogin = useCallback(() => {
    onGoToLogin();
  }, []);

  // ── Render ─────────────────────────────────────────────

  // Splash screen (also shown during backend_check, discovery, session_restore).
  // When we're forcing the auto-connect chain, surface the live step
  // ("searching the LAN…", "trying TCP scan…") into the splash status
  // line so the user sees we're still working and which path is being
  // tried right now.
  if (phase === 'splash' || phase === 'backend_check' || phase === 'discovery' || phase === 'session_restore') {
    let statusText: string | undefined;
    if (phase === 'discovery' && searchSteps.length) {
      const last = searchSteps[searchSteps.length - 1];
      const labels: Record<string, string> = {
        local: 'Same computer',
        saved: 'Saved server',
        lan: 'LAN router',
        tcp_scan: 'Deep LAN scan',
        rendezvous: 'Remote rendezvous',
      };
      const label = labels[last.id] || last.id;
      const verb = last.state === 'running' ? 'Trying' :
                   last.state === 'ok'      ? 'Found via'  :
                   last.state === 'fail'    ? 'No luck on' :
                   last.state === 'skipped' ? 'Skipped'    : '';
      const attemptSuffix = searchAttempt > 1 ? ` · attempt ${searchAttempt}` : '';
      statusText = `${verb} ${label}${attemptSuffix}`;
    }
    // Strict-mode config override is rare but important to surface —
    // operator changed config.json since the last successful run, and
    // we want them to know we're using the new URL not the cached one.
    if (strictDriftBanner) {
      statusText = statusText
        ? `${statusText} · ${strictDriftBanner}`
        : strictDriftBanner;
    }
    return <SplashScreen phase={phase} statusText={statusText} />;
  }

  // Onboarding
  if (phase === 'onboarding') {
    return (
      <OnboardingWizard
        serverUrl={serverUrl || 'http://127.0.0.1:3000'}
        onComplete={handleOnboardingComplete}
      />
    );
  }

  // Error recovery
  if (phase === 'error' && error) {
    return (
      <ErrorRecovery
        error={error}
        errorMessage={errorMessage}
        retryCount={retryCount}
        maxRetries={maxRetries}
        onRetry={handleRetry}
        onManualConnect={handleManualConnect}
        onGoToLogin={handleGoToLogin}
      />
    );
  }

  // login / ready phases handled by parent (App.tsx)
  return null;
};

export default AppBootstrapScreen;
