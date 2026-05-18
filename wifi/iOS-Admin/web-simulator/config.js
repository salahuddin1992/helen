/*
  Default runtime config for Helen Admin Mobile.

  Resolution order:
    1. If the page is served from Helen itself (path starts with
       /admin-mobile/), use location.origin — the operator is already
       talking to the right server and no configuration is needed.
    2. Otherwise fall back to http://localhost:3000 which matches
       Helen-Server's default HTTP port.
*/
(function () {
  try {
    if (window.location.pathname.indexOf('/admin-mobile/') === 0) {
      window.HELEN_BASE = window.location.origin;
      return;
    }
  } catch (_e) { /* file:// etc — fall through */ }
  window.HELEN_BASE = "http://localhost:3000";
})();
