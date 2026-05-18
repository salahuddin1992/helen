import type { CapacitorConfig } from '@capacitor/cli';

/**
 * Helen Mobile — Capacitor configuration.
 *
 * Wraps the renderer bundle that ships with the desktop client. The
 * web assets live in `./www` and are populated by `scripts/sync-
 * renderer.mjs` from `../CommClient-Desktop/dist-electron/renderer`.
 *
 * Server discovery
 * ----------------
 * The mobile client is server-agnostic. At first launch the user
 * picks a server URL (Helen-Server.exe on Windows OR the Linux
 * systemd unit). The discovery flow is the same as desktop:
 *   1. Probe LAN via mDNS (`_helen-server._tcp.local.`)
 *   2. UDP broadcast scan on 41234
 *   3. Manual URL entry
 *
 * The chosen URL is stored via @capacitor/preferences and reused on
 * subsequent launches. WebRTC traffic uses the same ICE/TURN config
 * the server hands back via /api/turn/ice-config — no platform-
 * specific signalling.
 */
const config: CapacitorConfig = {
  appId: 'com.helen.mobile',
  appName: 'Helen Mobile',
  webDir: 'www',
  // Bundled web assets, no remote loader — the renderer connects to
  // the server via REST + Socket.IO over the network, not by loading
  // remote HTML.
  server: {
    androidScheme: 'https',
    // Allow plain-text traffic to LAN servers that haven't enabled
    // HTTPS yet (typical home / office deployments). The
    // android:usesCleartextTraffic flag is also set in the manifest
    // network-security-config below.
    cleartext: true,
    // Allow the renderer to call into LAN IPs (mDNS hostnames + raw
    // 192.168.* / 10.* / 172.16-31.*). In production over the
    // internet this list should be tightened to the organisation's
    // server domain.
    allowNavigation: [
      '*.local',
      '127.0.0.1',
      'localhost',
      '10.*',
      '192.168.*',
      '172.16.*',
      '172.17.*',
      '172.18.*',
      '172.19.*',
      '172.20.*',
      '172.21.*',
      '172.22.*',
      '172.23.*',
      '172.24.*',
      '172.25.*',
      '172.26.*',
      '172.27.*',
      '172.28.*',
      '172.29.*',
      '172.30.*',
      '172.31.*',
    ],
  },
  android: {
    // Allow mixed content so an HTTPS-served bundle can call HTTP
    // LAN endpoints (the typical Helen-Server.exe deployment).
    allowMixedContent: true,
    // Default WebView is fine; Android 7+ supports the WebRTC stack
    // we need. Older devices fall back to a degraded experience.
    captureInput: true,
    webContentsDebuggingEnabled: true,
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 1500,
      backgroundColor: '#0d1117',
      androidSplashResourceName: 'splash',
      androidScaleType: 'CENTER_CROP',
      showSpinner: false,
      splashFullScreen: true,
      splashImmersive: true,
    },
    PushNotifications: {
      presentationOptions: ['badge', 'sound', 'alert'],
    },
    LocalNotifications: {
      smallIcon: 'ic_stat_icon_config_sample',
      iconColor: '#0a84ff',
      sound: 'beep.wav',
    },
    Keyboard: {
      resize: 'native',
      style: 'dark',
      resizeOnFullScreen: true,
    },
  },
};

export default config;
