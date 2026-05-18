/**
 * testServer.ts — Phase 4 / Module V — Spawns Helen-Server for E2E.
 *
 * Boots the Python backend in a child process, waits for /api/health to
 * return 200, hands the URL back to the test, and tears down on exit.
 *
 * Designed to be used as a Playwright fixture but works standalone too:
 *
 *     const server = await spawnTestServer();
 *     // ... run tests against server.url ...
 *     await server.close();
 */

import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const DEFAULT_PORT = 39000 + Math.floor(Math.random() * 1000);
const SERVER_DIR = join(__dirname, '..', '..', '..', 'CommClient-Server');

export interface TestServerHandle {
  url: string;
  port: number;
  pid: number;
  close(): Promise<void>;
}

async function waitForHealth(url: string, timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${url}/api/health`);
      if (r.ok) return;
    } catch {
      /* not yet */
    }
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error(`Helen-Server did not become healthy within ${timeoutMs} ms`);
}

export async function spawnTestServer(opts: {
  port?: number;
  python?: string;
  cwd?: string;
} = {}): Promise<TestServerHandle> {
  const port = opts.port ?? DEFAULT_PORT;
  const python = opts.python ?? (process.platform === 'win32' ? 'python' : 'python3');
  const cwd = opts.cwd ?? SERVER_DIR;

  if (!existsSync(join(cwd, 'run.py'))) {
    throw new Error(`Helen-Server entrypoint not found at ${cwd}/run.py`);
  }

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    HELEN_PORT: String(port),
    HELEN_TEST_MODE: '1',
    HELEN_DISABLE_DISCOVERY: '1',
    HELEN_DB_URL: 'sqlite+aiosqlite:///:memory:',
    HELEN_SECRET_KEY: 'e2e-test-key-0123456789abcdef0123456789abcdef',
    PYTHONUNBUFFERED: '1',
  };

  const child: ChildProcessWithoutNullStreams = spawn(
    python, ['run.py'], { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] },
  );

  // Pipe logs to stderr for debugging E2E failures
  child.stdout.on('data', d => process.stderr.write(`[helen] ${d}`));
  child.stderr.on('data', d => process.stderr.write(`[helen] ${d}`));

  const url = `http://127.0.0.1:${port}`;
  await waitForHealth(url);

  return {
    url,
    port,
    pid: child.pid ?? -1,
    async close() {
      if (!child.killed) {
        child.kill('SIGTERM');
        await new Promise<void>(resolve => {
          child.once('exit', () => resolve());
          setTimeout(() => { try { child.kill('SIGKILL'); } catch { /* noop */ } resolve(); }, 5000);
        });
      }
    },
  };
}
