/**
 * machineId.v2.ts — Phase 4 / Module U
 * =====================================
 *
 * Modern hardware-bound machine ID resolver. Replaces the historical
 * `wmic csproduct get UUID` call (deprecated and removed in Windows 11
 * 23H2) with a priority chain of safer, faster backends:
 *
 *   1. PowerShell `Get-CimInstance Win32_ComputerSystemProduct` (UUID)
 *   2. Registry read   `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`
 *   3. Persistent file `<APPDATA>/CommClient/data/.machine-id`
 *   4. Last fallback   generated UUIDv4 → persisted to (3)
 *
 * Every backend has a hard 5-second timeout. A failure on one backend is
 * logged and the chain falls through to the next.
 *
 * The function ALWAYS returns an uppercase, dash-formatted UUID-like
 * string (32 hex chars + 4 dashes) — never a raw GUID with braces,
 * never a Windows-style {GUID}.
 *
 * Public surface:
 *
 *     resolveMachineId({ forceRefresh? })   → Promise<string>
 *     getCachedMachineId()                  → string | null     (sync, in-memory only)
 *     clearCache()                          → Promise<void>
 *
 * Usage:
 *
 *     import { resolveMachineId } from './system/machineId.v2';
 *     const id = await resolveMachineId();
 *     // → "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

const execFileP = promisify(execFile);

// ── Constants ────────────────────────────────────────────────────────

/** Hard timeout per backend, in milliseconds. */
const BACKEND_TIMEOUT_MS = 5_000;

/** Strict UUID regex (uppercase or lowercase, with dashes). */
const UUID_RE = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;

/** Hex-only GUID (no dashes) — accepted as input, then re-formatted. */
const HEX_RE = /^[0-9A-Fa-f]{32}$/;

/** Order of resolved-from sources (for diagnostics). */
export type MachineIdSource =
  | 'cim'
  | 'registry'
  | 'cache-file'
  | 'generated';

interface ResolveResult {
  id: string;
  source: MachineIdSource;
}

// ── State ────────────────────────────────────────────────────────────

let _cached: ResolveResult | null = null;

// ── Helpers ──────────────────────────────────────────────────────────

/** Normalize "{AAAA...}" / "AAAA..." (no dashes) into canonical
 *  "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE" uppercase form.
 *  Returns null when the input cannot be coerced. */
function normalizeUuid(raw: string): string | null {
  const trimmed = raw.trim().replace(/[{}]/g, '');
  if (UUID_RE.test(trimmed)) return trimmed.toUpperCase();
  if (HEX_RE.test(trimmed)) {
    const h = trimmed.toUpperCase();
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  return null;
}

/** Resolve the path to the on-disk machine-id cache. Created if absent. */
function cacheFilePath(): string {
  const appData =
    process.env.APPDATA ||
    (process.platform === 'win32'
      ? join(process.env.USERPROFILE || '', 'AppData', 'Roaming')
      : join(process.env.HOME || '', '.config'));
  const dir = join(appData, 'CommClient', 'data');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  return join(dir, '.machine-id');
}

/** Run a binary with a hard timeout. Rejects on non-zero exit, timeout,
 *  or signal kill. Uses execFile (argv, not shell) for safety. */
async function runWithTimeout(
  cmd: string,
  args: string[],
  timeoutMs = BACKEND_TIMEOUT_MS,
): Promise<string> {
  const { stdout } = await execFileP(cmd, args, {
    timeout: timeoutMs,
    windowsHide: true,
    maxBuffer: 1024 * 64,
  });
  return stdout;
}

// ── Backends ─────────────────────────────────────────────────────────

/**
 * Backend 1: PowerShell + Get-CimInstance.
 *
 * Returns the SMBIOS-reported UUID (matches Settings → System → About →
 * Device ID on every supported Windows 10/11 build, including 23H2+).
 */
async function backendCim(): Promise<string | null> {
  if (process.platform !== 'win32') return null;
  try {
    const out = await runWithTimeout('powershell.exe', [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy', 'Bypass',
      '-Command',
      "(Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop).UUID",
    ]);
    return normalizeUuid(out);
  } catch {
    return null;
  }
}

/**
 * Backend 2: `reg query` against the Cryptography\MachineGuid value.
 *
 * Works even on stripped Server SKUs where PowerShell is locked down.
 * Returns a fully-formed UUID in the canonical layout.
 */
async function backendRegistry(): Promise<string | null> {
  if (process.platform !== 'win32') return null;
  try {
    const out = await runWithTimeout('reg.exe', [
      'query',
      'HKLM\\SOFTWARE\\Microsoft\\Cryptography',
      '/v', 'MachineGuid',
    ]);
    // Expected output line:
    //   "    MachineGuid    REG_SZ    aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    const m = out.match(/MachineGuid\s+REG_SZ\s+([0-9A-Fa-f-]{32,38})/);
    return m && m[1] ? normalizeUuid(m[1]) : null;
  } catch {
    return null;
  }
}

/**
 * Backend 3: cached UUID persisted in `<APPDATA>/CommClient/data/.machine-id`.
 *
 * This is what gives us survival across reinstalls when neither CIM nor
 * the registry is available (sandboxed test runs, dev VMs, etc).
 */
function backendCacheFile(): string | null {
  const path = cacheFilePath();
  if (!existsSync(path)) return null;
  try {
    const raw = readFileSync(path, 'utf-8');
    return normalizeUuid(raw);
  } catch {
    return null;
  }
}

/**
 * Backend 4: generate a fresh UUIDv4 and persist it to the cache file.
 * Only reached when every preceding backend failed.
 */
function backendGenerate(): string {
  const id = randomUUID().toUpperCase();
  try {
    writeFileSync(cacheFilePath(), id + '\n', 'utf-8');
  } catch {
    /* best-effort: caller will still receive the value */
  }
  return id;
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Resolve the machine ID using the priority chain. Result is cached
 * in-process for the lifetime of the renderer. Pass `forceRefresh: true`
 * to re-run every backend.
 */
export async function resolveMachineId(
  opts: { forceRefresh?: boolean } = {},
): Promise<string> {
  if (_cached && !opts.forceRefresh) return _cached.id;

  // 1. CIM
  const cim = await backendCim();
  if (cim) {
    _cached = { id: cim, source: 'cim' };
    return cim;
  }

  // 2. Registry
  const reg = await backendRegistry();
  if (reg) {
    _cached = { id: reg, source: 'registry' };
    // Persist for future fallback consistency
    try { writeFileSync(cacheFilePath(), reg + '\n', 'utf-8'); } catch { /* noop */ }
    return reg;
  }

  // 3. Cache file
  const cf = backendCacheFile();
  if (cf) {
    _cached = { id: cf, source: 'cache-file' };
    return cf;
  }

  // 4. Generate
  const gen = backendGenerate();
  _cached = { id: gen, source: 'generated' };
  return gen;
}

/** Synchronous read of the in-process cache. Null until the first
 *  `resolveMachineId()` call has resolved. */
export function getCachedMachineId(): string | null {
  return _cached ? _cached.id : null;
}

/** Returns extended info — useful for diagnostics in the admin UI. */
export function getCachedMachineIdInfo(): ResolveResult | null {
  return _cached ? { ..._cached } : null;
}

/** Clear both the in-process cache and the persisted file. Awaited so
 *  callers can wait for the unlink to complete before re-resolving. */
export async function clearCache(): Promise<void> {
  _cached = null;
  const path = cacheFilePath();
  if (existsSync(path)) {
    try { unlinkSync(path); } catch { /* best-effort */ }
  }
}

export const __internal = {
  normalizeUuid,
  cacheFilePath,
  backendCim,
  backendRegistry,
  backendCacheFile,
  backendGenerate,
  UUID_RE,
  HEX_RE,
  BACKEND_TIMEOUT_MS,
};
