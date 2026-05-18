# Helen-Router — Standalone Admin UI

This directory contains the router's OWN admin panel. It is served by
the router itself (FastAPI app in `app/admin_routes.py`) and reaches
the router's API on the same origin. It does NOT depend on
`Helen-Server` or `CommClient-Server`.

## Layout

```
Helen-Router/
├── admin/
│   ├── index.html       ← single-file SPA, RTL Arabic, ~3000+ lines
│   ├── vendor/          ← drop chart.min.js + d3.v7.min.js here
│   │   └── .gitkeep
│   └── README.md        ← (this file)
└── app/
    ├── admin_routes.py  ← APIRouter that serves admin/ statically
    └── main.py          ← include_router(admin_ui_router) added
```

## First-time setup

1. **Drop the JS libs locally** (no CDN at runtime — LAN/air-gapped):

   ```bash
   curl -sL https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js \
        -o admin/vendor/chart.min.js
   curl -sL https://d3js.org/d3.v7.min.js \
        -o admin/vendor/d3.v7.min.js
   ```

   The exact pin doesn't matter as long as the files load. The SPA
   detects missing libs gracefully and disables charts that need them.

2. **Set the router secret** (also used as the panel login token):

   ```bash
   export HELEN_ROUTER_TOKEN=$(openssl rand -hex 32)
   ```

3. **Run the router** (uvicorn or however you normally launch it):

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8080
   ```

## Login

* URL: `http://router.helen.lan:8080/admin/`
* Token: whatever you set in `HELEN_ROUTER_TOKEN`.
* The SPA stashes the token in `localStorage` under the key
  `helen_router_token`. Every API call carries it as
  `Authorization: Bearer <token>`.
* Logout clears the localStorage entry and reloads the SPA shell.

## Sections

The panel exposes these tabs (Arabic labels in the UI):

1.  **Overview** — uptime, version, mesh nodes, active sessions,
    req/s, mem/cpu, 24h timeseries (req/s + DNS queries + NTP clients).
2.  **Mesh** — LSA + Dijkstra table view, neighbours, force re-route,
    SVG topology diagram.
3.  **Service Registry** — CRUD over upstream Helen-Servers.
4.  **Reverse Proxy** — Request log (filterable), rate-limit rules,
    allow/deny IP lists.
5.  **DNS** — A/AAAA/CNAME/MX/SRV/TXT/PTR records, blocklist editor,
    query log, upstreams, stats.
6.  **NTP** — Status, peers, drift chart, force-sync.
7.  **UPnP** — Port maps list, add/delete, discovery scan.
8.  **Vendor Adapters** — Mikrotik/Ubiquiti/OpenWrt/pfSense/Cisco
    config templates + push.
9.  **External Routers** — List, scan, status.
10. **Broker** — Connection broker status, pending sessions,
    hole-punch stats.
11. **Security** — Token rotate, allowed CIDR subnets,
    enforcement mode.
12. **Diagnostics** — Ping, traceroute, DNS lookup, port scan,
    bandwidth test.
13. **Configuration** — YAML/JSON editor, validate, reload, save.
14. **Logs** — Live tail with filters (severity, module, search).
15. **About** — Version, license, build hash, useful links.

All sections fetch data through the existing router endpoints in
`app/main.py` (`/router/*`, `/mesh/*`). Sections that don't have a
matching backend route degrade gracefully (the SPA shows
"endpoint unavailable" and disables the section).

## Why a separate panel?

The `CommClient-Server` admin already has a `router_control.html`
that proxies through the server. That version is the right tool when
operators sit in front of the Helen-Server admin. But when there's no
server present (greenfield deploy, server down, or you literally want
to manage just the router), nothing should depend on the server. This
panel runs entirely from the router itself.
