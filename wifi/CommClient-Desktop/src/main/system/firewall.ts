/**
 * Runtime Windows Firewall gate.
 *
 * Installer already registers the firewall rules (build/installer.nsh →
 * RegisterFirewallRules). At runtime we re-verify that those rules
 * still exist — some AV/EDR suites strip them — and re-add them if
 * missing. Requires admin only when rules are missing; the check itself
 * is non-privileged.
 */

import { app } from 'electron';
import { spawn, spawnSync } from 'child_process';

export interface FirewallRule {
  name: string;
  port: number;
  protocol: 'TCP' | 'UDP';
  direction: 'in' | 'out';
  program?: string; // defaults to app.getPath('exe')
}

const DEFAULT_RULES: FirewallRule[] = [
  // Inbound — connections from other LAN hosts to this app
  { name: 'CommClient HTTP', port: 8000, protocol: 'TCP', direction: 'in' },
  { name: 'CommClient HTTPS', port: 8443, protocol: 'TCP', direction: 'in' },
  { name: 'CommClient Discovery', port: 41234, protocol: 'UDP', direction: 'in' },
  { name: 'CommClient SFU Control', port: 3000, protocol: 'TCP', direction: 'in' },
  { name: 'CommClient STUN', port: 3478, protocol: 'UDP', direction: 'in' },
  { name: 'CommClient TURN TCP', port: 3478, protocol: 'TCP', direction: 'in' },
  { name: 'CommClient TURN TLS', port: 5349, protocol: 'TCP', direction: 'in' },
  // Outbound — UDP broadcasts/multicast for discovery. Without these
  // explicit allows, locked-down Windows Defender profiles silently
  // drop our discovery probes and the user sees "no servers found".
  { name: 'CommClient UDP Discovery Out', port: 41234, protocol: 'UDP', direction: 'out' },
  { name: 'CommClient mDNS Out', port: 5353, protocol: 'UDP', direction: 'out' },
  { name: 'CommClient SSDP Out', port: 1900, protocol: 'UDP', direction: 'out' },
];

function ruleExists(name: string): boolean {
  try {
    const res = spawnSync(
      'netsh',
      ['advfirewall', 'firewall', 'show', 'rule', `name=${name}`],
      { encoding: 'utf-8', windowsHide: true }
    );
    if (res.status !== 0) return false;
    return !/No rules match/i.test(res.stdout || '');
  } catch {
    return false;
  }
}

function addRule(r: FirewallRule, program: string): boolean {
  try {
    const args = [
      'advfirewall',
      'firewall',
      'add',
      'rule',
      `name=${r.name}`,
      `dir=${r.direction === 'in' ? 'in' : 'out'}`,
      'action=allow',
      `protocol=${r.protocol}`,
      `localport=${r.port}`,
      `program=${program}`,
      'profile=private,domain',
      'enable=yes',
    ];
    const res = spawnSync('netsh', args, { encoding: 'utf-8', windowsHide: true });
    return res.status === 0;
  } catch {
    return false;
  }
}

export interface FirewallStatus {
  checked: number;
  missing: string[];
  repaired: string[];
  failed: string[];
  elevated: boolean;
}

function isElevated(): boolean {
  try {
    // netsh show status works without admin; 'add' silently fails without it.
    // Use `net session` as a cheap admin probe.
    const res = spawnSync('net', ['session'], { encoding: 'utf-8', windowsHide: true });
    return res.status === 0;
  } catch {
    return false;
  }
}

/**
 * Single-shot probe of every rule in one PowerShell call.
 * Replaces `rules.length` separate `netsh show` invocations with a
 * single `Get-NetFirewallRule` query, which is ~14× faster on
 * Windows. Returns the set of rule names that are present.
 */
function listExistingRuleNames(): Set<string> {
  try {
    const res = spawnSync(
      'powershell',
      [
        '-NoProfile', '-NonInteractive', '-Command',
        // -ErrorAction SilentlyContinue so unknown rules don't blow up
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        + "Select-Object -ExpandProperty DisplayName",
      ],
      { encoding: 'utf-8', windowsHide: true, timeout: 8000 },
    );
    if (res.status !== 0 || !res.stdout) return new Set();
    return new Set(
      res.stdout.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
    );
  } catch {
    return new Set();
  }
}

/**
 * Add many rules in a single PowerShell session. Builds the command
 * string with strict allowlist checks on every interpolation point —
 * the rule name / port / protocol / direction must match a SAFE_*
 * regex or the entry is skipped.
 */
function addRulesBatch(rules: FirewallRule[], program: string): {
  ok: string[]; failed: string[];
} {
  const SAFE_NAME = /^[A-Za-z0-9 \-_.]{1,64}$/;
  const SAFE_DIR = /^(in|out)$/i;
  const SAFE_PROTO = /^(TCP|UDP)$/;
  const SAFE_PORT = /^[0-9]{1,5}$/;
  const ok: string[] = [];
  const failed: string[] = [];
  const safeProgram = program.replace(/'/g, "''");
  const lines: string[] = [];
  for (const r of rules) {
    if (!SAFE_NAME.test(r.name)
        || !SAFE_DIR.test(r.direction)
        || !SAFE_PROTO.test(r.protocol)
        || !SAFE_PORT.test(String(r.port))) {
      failed.push(r.name);
      continue;
    }
    lines.push(
      "New-NetFirewallRule "
      + `-DisplayName '${r.name.replace(/'/g, "''")}' `
      + `-Direction ${r.direction === 'in' ? 'Inbound' : 'Outbound'} `
      + "-Action Allow "
      + `-Protocol ${r.protocol} `
      + `-LocalPort ${r.port} `
      + `-Program '${safeProgram}' `
      + "-Profile Private,Domain "
      + "-ErrorAction SilentlyContinue | Out-Null;",
    );
  }
  if (!lines.length) {
    return { ok, failed };
  }
  const script = lines.join(" ") + " 'OK'";
  const res = spawnSync(
    'powershell',
    ['-NoProfile', '-NonInteractive', '-Command', script],
    { encoding: 'utf-8', windowsHide: true, timeout: 30000 },
  );
  if (res.status === 0 && res.stdout?.includes('OK')) {
    for (const r of rules) {
      if (!failed.includes(r.name)) ok.push(r.name);
    }
  } else {
    for (const r of rules) {
      if (!failed.includes(r.name)) failed.push(r.name);
    }
  }
  return { ok, failed };
}

export async function ensureFirewall(
  rules: FirewallRule[] = DEFAULT_RULES
): Promise<FirewallStatus> {
  if (process.platform !== 'win32') {
    return { checked: 0, missing: [], repaired: [], failed: [], elevated: false };
  }

  const program = app.getPath('exe');
  const repaired: string[] = [];
  const failed: string[] = [];

  // ── 1. Single existence query (~1 spawn instead of N) ──────
  const existing = listExistingRuleNames();
  const missing: string[] = rules
    .filter(r => !existing.has(r.name))
    .map(r => r.name);

  // ── 2. Batched repair (1 spawn instead of N) ──────────────
  const elevated = isElevated();
  if (missing.length > 0 && elevated) {
    const toAdd = rules.filter(r => missing.includes(r.name));
    const result = addRulesBatch(toAdd, program);
    repaired.push(...result.ok);
    failed.push(...result.failed);
  }

  console.log(
    `[firewall] checked=${rules.length} missing=${missing.length} `
    + `repaired=${repaired.length} elevated=${elevated} `
    + `(batched, 1-2 spawns total)`
  );

  return {
    checked: rules.length,
    missing,
    repaired,
    failed,
    elevated,
  };
}

/**
 * Spawns an elevated helper process to run `netsh advfirewall ... add rule`
 * without blocking the main process. Used when `ensureFirewall` reports
 * missing rules but the current process is not elevated.
 */
export function requestElevatedFirewallRepair(
  rules: FirewallRule[] = DEFAULT_RULES
): void {
  if (process.platform !== 'win32') return;
  const program = app.getPath('exe');

  // Audit fix C4: previous implementation built a PowerShell command
  // via string-template with only `"` escaped. Any path or rule name
  // containing `$()`, single-quotes, or backticks triggered command
  // injection — and `program` is `app.getPath('exe')` which can sit
  // under a user-controllable home directory.
  //
  // Hardened: build the inner script as a PS-encoded string, then
  // pass it via `-EncodedCommand` so PS sees a raw UTF-16-LE base64
  // payload that's NEVER reparsed. We also reject rule names /
  // ports / directions that aren't in a strict allowlist.
  const SAFE_RULE_NAME = /^[A-Za-z0-9 \-_.]{1,64}$/;
  const SAFE_DIR = /^(in|out)$/i;
  const SAFE_PROTO = /^(tcp|udp)$/i;
  const SAFE_PORT = /^[0-9]{1,5}(-[0-9]{1,5})?$/;
  const validRules = rules.filter((r) =>
    SAFE_RULE_NAME.test(r.name) &&
    SAFE_DIR.test(r.direction) &&
    SAFE_PROTO.test(r.protocol) &&
    SAFE_PORT.test(String(r.port)),
  );
  if (validRules.length === 0) {
    console.warn('[firewall] no safe rules to install');
    return;
  }

  // Inner PS script — quotes are PS literals (single quote = no
  // expansion), and we use $args / -ArgumentList to keep the path
  // out of the parsed text entirely.
  const lines = validRules.map((r) =>
    `netsh advfirewall firewall add rule name='${r.name}' dir=${r.direction} action=allow protocol=${r.protocol} localport=${r.port} program=$args[0] profile=private,domain enable=yes`,
  );
  const innerScript = lines.join('; ');

  // Encode for -EncodedCommand: UTF-16-LE base64 of the script.
  // PS treats this as a single opaque blob, no second-pass parsing.
  const utf16leBytes = Buffer.from(innerScript, 'utf16le');
  const b64 = utf16leBytes.toString('base64');

  // Outer launcher: Start-Process with -ArgumentList as ARRAY
  // (proper argv) → the program path is NEVER concatenated into a
  // command string. -Verb RunAs prompts UAC.
  const launcher =
    `Start-Process -Verb RunAs -FilePath powershell -ArgumentList ` +
    `'-NoProfile','-WindowStyle','Hidden','-EncodedCommand','${b64}',` +
    `'-args','${program.replace(/'/g, "''")}'`;

  try {
    spawn(
      'powershell',
      ['-NoProfile', '-WindowStyle', 'Hidden', '-Command', launcher],
      {
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      },
    ).unref();
  } catch (err) {
    console.warn('[firewall] elevation spawn failed:', (err as Error).message);
  }
}
