# CommClient-Desktop — E2E Suite (Phase 4 / Module V)

Playwright-driven end-to-end tests that launch the real packaged Electron
app against a freshly-spawned Helen-Server.

## Layout

```
e2e/
  playwright.config.ts   Config (Electron target, retries, reporters)
  fixtures/
    testServer.ts        Spawns Helen-Server, waits for /api/health
  specs/
    auth.flow.spec.ts    Login form → dashboard transition
    messaging.spec.ts    Composer → message echoes back
    connectivity.spec.ts Renderer reaches server, surfaces URL in UI
```

## Prerequisites

```powershell
npm install --save-dev @playwright/test
npx playwright install --with-deps chromium
```

The fixture invokes `python run.py` from `../CommClient-Server`. The
Python deps from `requirements.txt` must already be installed in that
venv (or be on PATH).

## Running locally

```powershell
# 1. Build the Electron main bundle so Playwright has something to load
npm run build:renderer
npx vite build --mode test     # or `npm run build` for full electron build

# 2. Run all specs
npx playwright test --config e2e/playwright.config.ts

# 3. Run a single spec
npx playwright test e2e/specs/auth.flow.spec.ts --config e2e/playwright.config.ts
```

## Environment variables

| Var                   | Default                                | Used by              |
|-----------------------|----------------------------------------|----------------------|
| `HELEN_PORT`          | random 39000-39999                      | testServer.ts        |
| `HELEN_TEST_MODE`     | `1`                                    | testServer.ts        |
| `HELEN_DISABLE_DISCOVERY` | `1`                                | testServer.ts        |
| `COMMCLIENT_SERVER_URL` | from spawnTestServer().url           | electron app launch  |

## CI

The GitHub Actions workflow at `.github/workflows/test.yml` runs this
suite on Windows runners (Electron has spotty support on Linux CI).

## Debugging

* `--debug` opens the Playwright inspector.
* `--headed` runs the browser visibly (already default in this config).
* `traces`, `videos`, and `screenshots` are written to
  `playwright-report/` on failure. Open `playwright-report/index.html`
  for the report UI.
