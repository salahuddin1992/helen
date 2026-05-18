# Helen Admin — iOS

This folder is the **admin** counterpart to `iOS/` (which houses the end-user
client). It is a completely separate app: different bundle, different icon,
different user (an operator, not an end user).

Layout matches the client folder:

    iOS-Admin/
    ├── README.md                  you are here
    ├── web-simulator/             runs today on Windows + any browser
    │   ├── index.html             iPhone 16 Pro Max-sized admin panel
    │   ├── styles.css             Arabic/RTL, glass cards, diagnostic palette
    │   ├── app.js                 REST calls to /api/admin/*, no framework
    │   ├── config.js              HELEN_BASE resolver (same pattern as client)
    │   └── screenshots/           430×932 captures for review
    └── Native-App-Spec/           (future) Swift target for a Mac developer

## What this panel does

- **Overview** — server name, version, health, connected clients, users total
- **Users** — list, search, ban/kick/set-role/sessions actions
- **Network** — federation bridges, tunnel status, diagnostics summary
- **Backups** — list, run-now, download

It talks to the same Helen-Server as the client; the only difference is the
REST endpoints are under `/api/admin/*` and require a user whose role is
`admin`.

## How to run it today

Open `iOS-Admin/web-simulator/index.html` in a Chromium-based browser with
device emulation set to iPhone 16 Pro Max (430 × 932 CSS points), or browse
to `http://<helen-server>:3000/admin-mobile/` from a phone on the same WiFi
once Helen-Server is rebuilt with the `/admin-mobile/` mount (see
`CommClient-Server/CommClient-Server.spec`).
