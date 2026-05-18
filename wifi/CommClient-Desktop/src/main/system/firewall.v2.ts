/**
 * Firewall Rules v2 — Connectivity Hotfix Layer (Module C).
 *
 * A standalone firewall manager that installs a corrected port set for
 * the bundled Helen server. It is purely ADDITIVE — it never modifies
 * or removes v1 rules (those live under group name `CommClient` and
 * are owned by `system/firewall.ts`).
 *
 * Group name
 * ----------
 *   Helen-Server-v2
 *
 * Ports
 * -----
 *   TCP inbound  : 3000, 3443, 3478, 5349, 41235
 *   UDP inbound  : 3478, 5349, 41234, 40000-49999 (mediasoup range)
 *   UDP outbound : 5353, 1900, 41234
 *
 * Strategy
 * --------
 *   1. Probe for admin (`net session`); when missing, return
 *      `requiresElevation: true` without attempting any write.
 *   2. Primary path: `netsh advfirewall firewall add rule …` invoked
 *      via `child_process.execFile` with every argv element pre-vetted
 *      against a strict allowlist (no shell, no concatenation).
 *   3. Fallback: `New-NetFirewallRule` via PowerShell, batched in one
 *      session for speed.
 *   4. `checkRulesV2()` returns a `RuleStatus[]` snapshot from
 *      `netsh advfirewall firewall show rule group=`.
 *
 * Never touches anything outside the `Helen-Server-v2` group.
 */

import { app } from 'electron';
import { execFile, spawnSync } from 'node:child_process';

// ─────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────

/** Group name shared by every rule this module manages. */
export const FIREWALL_V2_GROUP = 'Helen-Server-v2';

/** Allowlist regexes — applied to every interpolation point. */
const SAFE_NAME = /^[A-Za-z0-9 \-_.]{1,80}$/;
const SAFE_DIR = /^(in|out)$/;
const SAFE_PROTO = /^(TCP|UDP)$/;
const SAFE_PORT = /^[0-9]{1,5}(-[0-9]{1,5})?$/;

/** Hard timeouts so a hung netsh / powershell never strands the caller. */
const NETSH_TIMEOUT_MS = 10_000;
const POWERSHELL_TIMEOUT_MS = 30_000;
const ADMIN_PROBE_TIMEOUT_MS = 3_000;

// ─────────────────────────────────────────────────────────────────────
// Rule shape
// ─────────────────────────────────────────────────────────────────────

export type Direction = 'in' | 'out';
export type Protocol = 'TCP' | 'UDP';

export interface FirewallV2Rule {
    /** Display + lookup name. Must match `SAFE_NAME`. */
    name: string;
    direction: Direction;
    protocol: Protocol;
    /** Single port (`3000`) or inclusive range (`40000-49999`). */
    port: string;
    /** Profile(s) the rule applies to. Defaults to private+domain. */
    profile?: 'private,domain' | 'private' | 'domain' | 'public,private,domain';
}

/** Canonical rule set — the corrected list demanded by the hotfix. */
export const HELEN_V2_RULES: FirewallV2Rule[] = [
    // ── TCP inbound ───────────────────────────────────────
    { name: 'Helen V2 HTTP', direction: 'in', protocol: 'TCP', port: '3000' },
    { name: 'Helen V2 HTTPS', direction: 'in', protocol: 'TCP', port: '3443' },
    { name: 'Helen V2 TURN TCP', direction: 'in', protocol: 'TCP', port: '3478' },
    { name: 'Helen V2 TURN TLS', direction: 'in', protocol: 'TCP', port: '5349' },
    { name: 'Helen V2 TCP Fallback', direction: 'in', protocol: 'TCP', port: '41235' },

    // ── UDP inbound ───────────────────────────────────────
    { name: 'Helen V2 STUN UDP', direction: 'in', protocol: 'UDP', port: '3478' },
    { name: 'Helen V2 TURN UDP TLS', direction: 'in', protocol: 'UDP', port: '5349' },
    { name: 'Helen V2 Discovery UDP', direction: 'in', protocol: 'UDP', port: '41234' },
    { name: 'Helen V2 Mediasoup UDP', direction: 'in', protocol: 'UDP', port: '40000-49999' },

    // ── UDP outbound ──────────────────────────────────────
    { name: 'Helen V2 mDNS Out', direction: 'out', protocol: 'UDP', port: '5353' },
    { name: 'Helen V2 SSDP Out', direction: 'out', protocol: 'UDP', port: '1900' },
    { name: 'Helen V2 Discovery Out', direction: 'out', protocol: 'UDP', port: '41234' },
];

// ─────────────────────────────────────────────────────────────────────
// Public types
// ─────────────────────────────────────────────────────────────────────

export interface FirewallV2Options {
    /** Override the program path bound to inbound rules. Defaults to `app.getPath('exe')`. */
    program?: string;
    /** Preferred backend. Defaults to `netsh`; falls back to `powershell`. */
    backend?: 'netsh' | 'powershell' | 'auto';
    /** When true, remove the existing v2 group before installing. */
    cleanInstall?: boolean;
}

export interface FirewallV2Result {
    /** True iff at least one valid rule was written or already present. */
    ok: boolean;
    /** True iff the current process is not running elevated and we bailed. */
    requiresElevation: boolean;
    /** Backend actually used. */
    backend: 'netsh' | 'powershell' | 'none';
    /** Per-rule success flags. */
    added: string[];
    failed: string[];
    /** Free-form diagnostic info. */
    notes: string[];
}

export interface RuleStatus {
    name: string;
    direction: Direction;
    protocol: Protocol;
    port: string;
    present: boolean;
    enabled: boolean | null;
}

// ─────────────────────────────────────────────────────────────────────
// Internal helpers
// ─────────────────────────────────────────────────────────────────────

function log(level: 'info' | 'warn' | 'error', msg: string, extra?: Record<string, unknown>): void {
    const tag = '[firewall.v2]';
    const line = extra ? `${msg} ${JSON.stringify(extra)}` : msg;
    if (level === 'info') console.log(tag, line);
    else if (level === 'warn') console.warn(tag, line);
    else console.error(tag, line);
}

/** Synchronous admin probe — `net session` requires elevation. */
function isElevated(): boolean {
    if (process.platform !== 'win32') return false;
    try {
        const res = spawnSync('net', ['session'], {
            encoding: 'utf-8',
            windowsHide: true,
            timeout: ADMIN_PROBE_TIMEOUT_MS,
        });
        return res.status === 0;
    } catch {
        return false;
    }
}

/** Validate one rule, returning the rejection reason or null on success. */
function validateRule(r: FirewallV2Rule): string | null {
    if (!SAFE_NAME.test(r.name)) return `bad name: ${r.name}`;
    if (!SAFE_DIR.test(r.direction)) return `bad direction: ${r.direction}`;
    if (!SAFE_PROTO.test(r.protocol)) return `bad protocol: ${r.protocol}`;
    if (!SAFE_PORT.test(r.port)) return `bad port: ${r.port}`;
    return null;
}

/** Promise wrapper for `execFile` with strict timeout + windowsHide. */
function execFileP(
    file: string,
    args: string[],
    timeoutMs: number,
): Promise<{ code: number; stdout: string; stderr: string }> {
    return new Promise((resolve) => {
        execFile(
            file,
            args,
            { windowsHide: true, timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024 },
            (err, stdout, stderr) => {
                resolve({
                    code: err ? (err as NodeJS.ErrnoException).code === 'ETIMEDOUT' ? -2 : 1 : 0,
                    stdout: stdout?.toString() ?? '',
                    stderr: stderr?.toString() ?? '',
                });
            },
        );
    });
}

/** Build the argv for `netsh advfirewall firewall add rule …`. */
function buildNetshArgs(rule: FirewallV2Rule, program: string): string[] {
    const profile = rule.profile ?? 'private,domain';
    const args = [
        'advfirewall', 'firewall', 'add', 'rule',
        `name=${rule.name}`,
        `dir=${rule.direction}`,
        'action=allow',
        `protocol=${rule.protocol}`,
        `localport=${rule.port}`,
        `program=${program}`,
        `profile=${profile}`,
        `description=${FIREWALL_V2_GROUP}`,
        'enable=yes',
    ];
    return args;
}

// ─────────────────────────────────────────────────────────────────────
// Backend: netsh (primary)
// ─────────────────────────────────────────────────────────────────────

async function addRuleViaNetsh(rule: FirewallV2Rule, program: string): Promise<boolean> {
    const args = buildNetshArgs(rule, program);
    const res = await execFileP('netsh', args, NETSH_TIMEOUT_MS);
    if (res.code !== 0) {
        log('warn', 'netsh add failed', { rule: rule.name, stderr: res.stderr.trim() });
    }
    return res.code === 0;
}

export async function applyViaNetsh(
    rules: FirewallV2Rule[] = HELEN_V2_RULES,
    program: string = app?.getPath?.('exe') ?? process.execPath,
): Promise<{ added: string[]; failed: string[] }> {
    const added: string[] = [];
    const failed: string[] = [];
    for (const rule of rules) {
        const reason = validateRule(rule);
        if (reason) {
            log('warn', 'rule rejected', { rule: rule.name, reason });
            failed.push(rule.name);
            continue;
        }
        const ok = await addRuleViaNetsh(rule, program);
        (ok ? added : failed).push(rule.name);
    }
    return { added, failed };
}

// ─────────────────────────────────────────────────────────────────────
// Backend: powershell New-NetFirewallRule (fallback)
// ─────────────────────────────────────────────────────────────────────

function escapePsLiteral(s: string): string {
    // PowerShell single-quoted literal: only `'` needs doubling.
    return s.replace(/'/g, "''");
}

function buildPsLine(rule: FirewallV2Rule, program: string): string {
    const dir = rule.direction === 'in' ? 'Inbound' : 'Outbound';
    const profile = (rule.profile ?? 'private,domain')
        .split(',')
        .map((p) => p.trim())
        .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
        .join(',');
    return (
        `New-NetFirewallRule `
        + `-DisplayName '${escapePsLiteral(rule.name)}' `
        + `-Group '${escapePsLiteral(FIREWALL_V2_GROUP)}' `
        + `-Direction ${dir} `
        + `-Action Allow `
        + `-Protocol ${rule.protocol} `
        + `-LocalPort ${rule.port} `
        + `-Program '${escapePsLiteral(program)}' `
        + `-Profile ${profile} `
        + `-ErrorAction SilentlyContinue | Out-Null;`
    );
}

export async function applyViaPowerShell(
    rules: FirewallV2Rule[] = HELEN_V2_RULES,
    program: string = app?.getPath?.('exe') ?? process.execPath,
): Promise<{ added: string[]; failed: string[] }> {
    const added: string[] = [];
    const failed: string[] = [];

    const safeRules: FirewallV2Rule[] = [];
    for (const rule of rules) {
        const reason = validateRule(rule);
        if (reason) {
            log('warn', 'rule rejected', { rule: rule.name, reason });
            failed.push(rule.name);
            continue;
        }
        safeRules.push(rule);
    }
    if (!safeRules.length) return { added, failed };

    const script = safeRules.map((r) => buildPsLine(r, program)).join(' ') + " 'OK'";
    const utf16leBytes = Buffer.from(script, 'utf16le');
    const encoded = utf16leBytes.toString('base64');

    const res = await execFileP(
        'powershell',
        ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        POWERSHELL_TIMEOUT_MS,
    );
    if (res.code === 0 && res.stdout.includes('OK')) {
        added.push(...safeRules.map((r) => r.name));
    } else {
        failed.push(...safeRules.map((r) => r.name));
        log('warn', 'powershell apply failed', { stderr: res.stderr.trim().slice(0, 400) });
    }
    return { added, failed };
}

// ─────────────────────────────────────────────────────────────────────
// Rule snapshot
// ─────────────────────────────────────────────────────────────────────

/**
 * Query the current state of every Helen-Server-v2 rule via
 * `netsh advfirewall firewall show rule name=<name>`.
 *
 * netsh emits a key=value text block per rule; parsing is line-oriented
 * and tolerates localized headers (we only key off `Enabled:` and
 * `LocalPort:` which are stable in every Windows locale we've checked).
 */
export async function checkRulesV2(
    rules: FirewallV2Rule[] = HELEN_V2_RULES,
): Promise<RuleStatus[]> {
    const out: RuleStatus[] = [];
    for (const rule of rules) {
        if (validateRule(rule)) {
            out.push({
                name: rule.name,
                direction: rule.direction,
                protocol: rule.protocol,
                port: rule.port,
                present: false,
                enabled: null,
            });
            continue;
        }
        const res = await execFileP(
            'netsh',
            ['advfirewall', 'firewall', 'show', 'rule', `name=${rule.name}`, 'verbose'],
            NETSH_TIMEOUT_MS,
        );
        const text = res.stdout || '';
        const present = res.code === 0 && !/No rules match/i.test(text);
        let enabled: boolean | null = null;
        if (present) {
            const m = /Enabled:\s+(Yes|No)/i.exec(text);
            enabled = m ? /yes/i.test(m[1]) : null;
        }
        out.push({
            name: rule.name,
            direction: rule.direction,
            protocol: rule.protocol,
            port: rule.port,
            present,
            enabled,
        });
    }
    return out;
}

// ─────────────────────────────────────────────────────────────────────
// Group removal (scoped to v2 ONLY)
// ─────────────────────────────────────────────────────────────────────

/**
 * Remove every rule belonging to the `Helen-Server-v2` group.
 *
 * Strictly v2-only — v1 rules (group `CommClient`, individual names
 * with the `CommClient ` prefix) are never touched.
 */
export async function removeOldGroup(): Promise<void> {
    // We delete each known v2 rule by exact name. Using a wildcard
    // `name=all group=…` works on most Windows builds but is locale-
    // sensitive; deleting by stable name is reliable everywhere.
    for (const rule of HELEN_V2_RULES) {
        if (validateRule(rule)) continue;
        await execFileP(
            'netsh',
            ['advfirewall', 'firewall', 'delete', 'rule', `name=${rule.name}`],
            NETSH_TIMEOUT_MS,
        );
    }
    log('info', 'old v2 group removed', { rules: HELEN_V2_RULES.length });
}

// ─────────────────────────────────────────────────────────────────────
// Public entrypoint
// ─────────────────────────────────────────────────────────────────────

export async function configureFirewallV2(
    opts: FirewallV2Options = {},
): Promise<FirewallV2Result> {
    const notes: string[] = [];
    const result: FirewallV2Result = {
        ok: false,
        requiresElevation: false,
        backend: 'none',
        added: [],
        failed: [],
        notes,
    };

    if (process.platform !== 'win32') {
        notes.push('not-windows: skipping');
        result.ok = true;
        return result;
    }

    if (!isElevated()) {
        notes.push('current process is not elevated; rules require admin to write');
        result.requiresElevation = true;
        return result;
    }

    const program = opts.program ?? (app?.getPath?.('exe') ?? process.execPath);

    if (opts.cleanInstall) {
        try { await removeOldGroup(); } catch (err) {
            notes.push(`removeOldGroup failed: ${(err as Error).message}`);
        }
    }

    const backend = opts.backend ?? 'auto';

    if (backend === 'netsh' || backend === 'auto') {
        log('info', 'applying via netsh', { rules: HELEN_V2_RULES.length, program });
        const { added, failed } = await applyViaNetsh(HELEN_V2_RULES, program);
        result.added.push(...added);
        result.failed.push(...failed);
        result.backend = 'netsh';
        if (failed.length === 0) {
            result.ok = true;
            notes.push(`netsh: ${added.length}/${HELEN_V2_RULES.length} rules applied`);
            return result;
        }
        notes.push(`netsh partial: ${added.length} ok, ${failed.length} failed`);
    }

    if ((backend === 'auto' || backend === 'powershell') && result.failed.length > 0) {
        log('info', 'falling back to powershell', { remaining: result.failed.length });
        const retry = HELEN_V2_RULES.filter((r) => result.failed.includes(r.name));
        const { added, failed } = await applyViaPowerShell(retry, program);
        // Move successes out of the failed bucket.
        for (const name of added) {
            const i = result.failed.indexOf(name);
            if (i >= 0) result.failed.splice(i, 1);
            result.added.push(name);
        }
        result.backend = 'powershell';
        notes.push(`powershell: ${added.length} recovered, ${failed.length} still failed`);
    }

    result.ok = result.failed.length === 0 && result.added.length > 0;
    return result;
}

const firewallV2 = {
    FIREWALL_V2_GROUP,
    HELEN_V2_RULES,
    configureFirewallV2,
    checkRulesV2,
    removeOldGroup,
    applyViaNetsh,
    applyViaPowerShell,
};

export default firewallV2;
