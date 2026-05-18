# Coverage Philosophy — Phase 4 / Module V

This document describes how the CommClient/Helen project measures and
enforces test coverage across its three runtimes (Python backend,
Electron renderer, Electron main).

## Targets

| Component                | Coverage target | Hard floor |
|--------------------------|-----------------|------------|
| Backend (`CommClient-Server/app/`) | 75% lines | 60%        |
| Renderer (`src/renderer/`) | 70% lines | 50%        |
| Main (`src/main/`)         | 70% lines | 50%        |
| Critical paths (auth, RBAC, federation HMAC) | 90% branches | 80% |

We don't chase 100% — diminishing returns past 80%, especially in glue
code. We DO enforce floors on critical paths (auth + RBAC + crypto) where
a regression has security implications.

## Running locally

```powershell
# Backend
cd CommClient-Server
pip install pytest pytest-cov pytest-asyncio
pytest -c pytest.phase4.ini --cov=app --cov-report=html
start htmlcov/index.html

# Renderer + Main
cd CommClient-Desktop
npm install --save-dev @vitest/coverage-v8
npx vitest run --coverage
start coverage/index.html

# E2E (after building renderer)
npx playwright test --config e2e/playwright.config.ts
start playwright-report/index.html
```

## What we count

* **Backend**: line + branch coverage of `app/`. Excluded:
  `app/migrations/`, generated transport adapters, `*/tests/`.
* **Renderer**: line coverage of `src/renderer/`. Excluded: generated
  assets, build artifacts.
* **Main**: line coverage of `src/main/`. Excluded: electron-builder
  hooks.

## CI enforcement

`.github/workflows/test.yml` uploads to Codecov on every push but does
NOT fail the build on coverage drops yet — we're collecting baseline
data for the first quarter. The hard floors above will be enforced once
the baseline stabilizes.

## Phase 4 additions

The Phase-4 test pack adds (all under `*_phase4.py` / `__tests__/`):

* Backend: 10 new pytest files covering health, auth, RBAC, admin
  logs/metrics, workspaces, agents, OAuth, secrets resolver.
* Renderer: 4 new vitest files covering port resolver, OAuth client,
  API client, auth store.
* Main: 4 new vitest files covering port sidecar, firewall v2, cert
  trust dialog, machine-ID v2.
* E2E: 3 Playwright specs covering auth, messaging, connectivity.

## When to add a test

* New public endpoint? Add a smoke test + an authz negative test.
* New Pydantic model? Add a serialize/deserialize round-trip test.
* Bug fix? Add a regression test capturing the bug's input.
* Performance optimization? Add a perf gate to `tests/perf/`.

## When NOT to add a test

* Pure glue / re-exports (the domain facade): excluded already.
* Generated code (Alembic migrations, transport adapter scaffolds).
* Throwaway debug scripts.
