/*
  Default runtime config for the Helen Mobile web simulator.

  `HELEN_BASE` — Helen server origin the app will use by default on
  onboarding. The user can override it in the onboarding screen.

  Resolution order:
    1. If the page is served from Helen itself (path starts with
       /mobile/), use location.origin — the phone is already talking
       to the right server via WiFi/LAN and no configuration is
       needed. This is the "open Safari and it just works" path.
    2. Otherwise, fall back to http://localhost:3000 which matches
       Helen-Server's default HTTP port for local development.
*/
(function () {
  try {
    if (window.location.pathname.indexOf('/mobile/') === 0) {
      window.HELEN_BASE = window.location.origin;
      return;
    }
  } catch (_e) { /* file:// etc — fall through */ }
  window.HELEN_BASE = "http://localhost:3000";
})();
