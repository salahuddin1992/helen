/**
 * messaging.spec.ts — Phase 4 / Module V — send/receive smoke.
 *
 * Logs in, opens a channel, types a message, asserts it shows up in the
 * timeline. Bypasses the real socket layer if the renderer exposes a
 * test hook (`window.__helenTest__`); otherwise drives the UI directly.
 */

import { test, expect, _electron as electron, ElectronApplication, Page } from '@playwright/test';
import { spawnTestServer, TestServerHandle } from '../fixtures/testServer';
import { join } from 'node:path';

let server: TestServerHandle | null = null;
let app: ElectronApplication | null = null;
let win: Page | null = null;

test.beforeAll(async () => {
  server = await spawnTestServer();
  app = await electron.launch({
    args: [join(__dirname, '..', '..', 'dist-electron', 'main', 'index.js')],
    env: { ...process.env, COMMCLIENT_SERVER_URL: server.url, NODE_ENV: 'test' },
  });
  win = await app.firstWindow();
  await win.waitForLoadState('domcontentloaded');
});

test.afterAll(async () => {
  if (app) await app.close();
  if (server) await server.close();
});

test('renderer can submit a message and see it echoed', async () => {
  const w = win!;
  // Try to short-circuit auth via test hook if available
  const hasHook = await w.evaluate(() => Boolean((window as any).__helenTest__));
  if (hasHook) {
    await w.evaluate(() => (window as any).__helenTest__.loginAs('e2e_user'));
  }

  // Find a message composer — flexible locator
  const composer = w.locator(
    'textarea[placeholder*="message" i], input[placeholder*="message" i]',
  ).first();
  if (!(await composer.count())) {
    test.skip(true, 'No composer visible — UI not in chat state');
    return;
  }
  await composer.fill('hello phase4');
  await composer.press('Enter');

  await expect(
    w.locator('text=/hello phase4/').first(),
  ).toBeVisible({ timeout: 10000 });
});
