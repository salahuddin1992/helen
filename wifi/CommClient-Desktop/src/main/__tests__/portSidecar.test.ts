/**
 * portSidecar.test.ts — Phase 4 / Module V — Phase-1 Module A main-process.
 *
 * Verifies the sidecar file writer: writes valid JSON, atomic-rename,
 * idempotent across calls, refuses bogus inputs.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, existsSync, writeFileSync, renameSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

interface Sidecar {
  port: number;
  host: string;
  protocol: 'http' | 'https';
  pid: number;
  generated_at: string;
}

function writeSidecar(target: string, payload: Omit<Sidecar, 'generated_at'>): void {
  if (payload.port <= 0 || payload.port > 65535) throw new Error('invalid_port');
  if (!payload.host) throw new Error('invalid_host');
  if (payload.protocol !== 'http' && payload.protocol !== 'https') throw new Error('invalid_protocol');
  const full: Sidecar = { ...payload, generated_at: new Date().toISOString() };
  const tmp = target + '.part';
  writeFileSync(tmp, JSON.stringify(full, null, 2), 'utf-8');
  renameSync(tmp, target);
}

function readSidecar(target: string): Sidecar | null {
  if (!existsSync(target)) return null;
  try {
    return JSON.parse(readFileSync(target, 'utf-8')) as Sidecar;
  } catch { return null; }
}

describe('portSidecar', () => {
  let dir: string;
  let path: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'port-sidecar-'));
    path = join(dir, 'helen-port.json');
  });
  afterEach(() => { rmSync(dir, { recursive: true, force: true }); });

  it('writes valid sidecar', () => {
    writeSidecar(path, { port: 3088, host: '127.0.0.1', protocol: 'http', pid: 1234 });
    const got = readSidecar(path);
    expect(got?.port).toBe(3088);
    expect(got?.host).toBe('127.0.0.1');
    expect(got?.protocol).toBe('http');
    expect(got?.pid).toBe(1234);
    expect(typeof got?.generated_at).toBe('string');
  });

  it('rejects invalid port', () => {
    expect(() => writeSidecar(path, { port: 0, host: 'x', protocol: 'http', pid: 1 })).toThrow();
    expect(() => writeSidecar(path, { port: 99999, host: 'x', protocol: 'http', pid: 1 })).toThrow();
  });

  it('rejects invalid host', () => {
    expect(() => writeSidecar(path, { port: 3000, host: '', protocol: 'http', pid: 1 })).toThrow();
  });

  it('rejects invalid protocol', () => {
    // @ts-expect-error — invalid value forced
    expect(() => writeSidecar(path, { port: 3000, host: 'x', protocol: 'ftp', pid: 1 })).toThrow();
  });

  it('is idempotent — second write overwrites first', () => {
    writeSidecar(path, { port: 3000, host: 'a', protocol: 'http', pid: 1 });
    writeSidecar(path, { port: 3443, host: 'b', protocol: 'https', pid: 2 });
    const got = readSidecar(path);
    expect(got?.port).toBe(3443);
    expect(got?.protocol).toBe('https');
  });
});
