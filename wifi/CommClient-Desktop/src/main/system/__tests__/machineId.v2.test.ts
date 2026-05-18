/**
 * machineId.v2.test.ts — Phase 4 / Module U + V
 *
 * vitest scaffold for the new WMIC-replacement resolver. Mocks the
 * filesystem + child_process layer so unit tests are deterministic and
 * platform-independent (no live Windows registry access).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';

import {
  resolveMachineId,
  getCachedMachineId,
  getCachedMachineIdInfo,
  clearCache,
  __internal,
} from '../machineId.v2';

const FAKE_APPDATA = join(tmpdir(), 'commclient-test-' + Date.now());

beforeEach(() => {
  process.env.APPDATA = FAKE_APPDATA;
  if (!existsSync(FAKE_APPDATA)) mkdirSync(FAKE_APPDATA, { recursive: true });
});

afterEach(async () => {
  await clearCache();
  if (existsSync(FAKE_APPDATA)) rmSync(FAKE_APPDATA, { recursive: true, force: true });
  vi.restoreAllMocks();
});

// ── normalizeUuid ───────────────────────────────────────────────────

describe('normalizeUuid', () => {
  it('accepts canonical UUID and uppercases it', () => {
    expect(__internal.normalizeUuid('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'))
      .toBe('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
  });

  it('strips braces around a UUID', () => {
    expect(__internal.normalizeUuid('{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}'))
      .toBe('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
  });

  it('formats a bare 32-hex string into canonical layout', () => {
    expect(__internal.normalizeUuid('aabbccddeeff00112233445566778899'))
      .toBe('AABBCCDD-EEFF-0011-2233-445566778899');
  });

  it('rejects garbage', () => {
    expect(__internal.normalizeUuid('not-a-uuid')).toBeNull();
    expect(__internal.normalizeUuid('')).toBeNull();
    expect(__internal.normalizeUuid('A'.repeat(31))).toBeNull();
  });
});

// ── Regex sanity ────────────────────────────────────────────────────

describe('regexes', () => {
  it('UUID_RE matches well-formed UUIDs only', () => {
    expect(__internal.UUID_RE.test('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE')).toBe(true);
    expect(__internal.UUID_RE.test('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEE')).toBe(false);  // short
    expect(__internal.UUID_RE.test('GGGGGGGG-BBBB-CCCC-DDDD-EEEEEEEEEEEE')).toBe(false); // non-hex
  });
});

// ── Backend fallback ordering ───────────────────────────────────────

describe('resolveMachineId — backend ordering', () => {
  it('returns CIM result first when available', async () => {
    vi.spyOn(__internal, 'backendCim').mockResolvedValue('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
    vi.spyOn(__internal, 'backendRegistry').mockResolvedValue('FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF');
    await clearCache();
    const id = await resolveMachineId({ forceRefresh: true });
    expect(id).toBe('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
    expect(getCachedMachineIdInfo()?.source).toBe('cim');
  });

  it('falls back to registry when CIM fails', async () => {
    vi.spyOn(__internal, 'backendCim').mockResolvedValue(null);
    vi.spyOn(__internal, 'backendRegistry').mockResolvedValue('11111111-2222-3333-4444-555555555555');
    await clearCache();
    const id = await resolveMachineId({ forceRefresh: true });
    expect(id).toBe('11111111-2222-3333-4444-555555555555');
    expect(getCachedMachineIdInfo()?.source).toBe('registry');
  });

  it('falls back to cache file when CIM + registry both fail', async () => {
    const cachePath = __internal.cacheFilePath();
    writeFileSync(cachePath, '99999999-AAAA-BBBB-CCCC-DDDDDDDDDDDD\n', 'utf-8');

    vi.spyOn(__internal, 'backendCim').mockResolvedValue(null);
    vi.spyOn(__internal, 'backendRegistry').mockResolvedValue(null);
    await clearCache();
    // Recreate the cache file after clearCache deleted it
    writeFileSync(cachePath, '99999999-AAAA-BBBB-CCCC-DDDDDDDDDDDD\n', 'utf-8');

    const id = await resolveMachineId({ forceRefresh: true });
    expect(id).toBe('99999999-AAAA-BBBB-CCCC-DDDDDDDDDDDD');
    expect(getCachedMachineIdInfo()?.source).toBe('cache-file');
  });

  it('generates a fresh UUIDv4 when every backend fails', async () => {
    vi.spyOn(__internal, 'backendCim').mockResolvedValue(null);
    vi.spyOn(__internal, 'backendRegistry').mockResolvedValue(null);
    await clearCache();
    const id = await resolveMachineId({ forceRefresh: true });
    expect(__internal.UUID_RE.test(id)).toBe(true);
    expect(getCachedMachineIdInfo()?.source).toBe('generated');
  });
});

// ── Caching ─────────────────────────────────────────────────────────

describe('caching', () => {
  it('returns the cached value without re-running backends', async () => {
    const spy = vi.spyOn(__internal, 'backendCim').mockResolvedValue('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
    await clearCache();
    const first = await resolveMachineId();
    const second = await resolveMachineId();
    expect(first).toBe(second);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('getCachedMachineId returns null before first resolve', async () => {
    await clearCache();
    expect(getCachedMachineId()).toBeNull();
  });

  it('clearCache wipes both in-memory + on-disk state', async () => {
    vi.spyOn(__internal, 'backendCim').mockResolvedValue('AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE');
    await resolveMachineId({ forceRefresh: true });
    expect(getCachedMachineId()).not.toBeNull();
    await clearCache();
    expect(getCachedMachineId()).toBeNull();
  });
});
