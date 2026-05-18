/**
 * playwright.config.ts — Phase 4 / Module V — Electron E2E.
 *
 * Playwright targets Electron via `_electron` from '@playwright/test'.
 * Tests live under `e2e/specs/*.spec.ts`. The fixture
 * `e2e/fixtures/testServer.ts` boots Helen-Server before the suite and
 * shuts it down after.
 */

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,                // electron — serial only
  workers: 1,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
    ['junit', { outputFile: 'playwright-report/junit.xml' }],
  ],
  use: {
    headless: false,
    actionTimeout: 15_000,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  retries: process.env.CI ? 1 : 0,
  globalSetup: undefined,
});
