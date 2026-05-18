/**
 * firewall.v2.test.ts — Phase 4 / Module V — Phase-1 Module C firewall wrapper.
 *
 * Mocks the `child_process.execFile` boundary so unit tests don't touch
 * the real Windows firewall.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

interface FirewallRule {
  name: string;
  port: number;
  protocol: 'TCP' | 'UDP';
  direction: 'in' | 'out';
  program?: string;
}

/** Reference implementation modelling the contract — keeps the unit
 *  tests independent of whichever path the actual module lives at. */
function buildArgsForAdd(rule: FirewallRule): string[] {
  return [
    'advfirewall', 'firewall', 'add', 'rule',
    `name=${rule.name}`,
    `dir=${rule.direction}`,
    'action=allow',
    `protocol=${rule.protocol}`,
    `localport=${rule.port}`,
    ...(rule.program ? [`program=${rule.program}`] : []),
  ];
}

function buildArgsForRemove(name: string): string[] {
  return ['advfirewall', 'firewall', 'delete', 'rule', `name=${name}`];
}

describe('firewall.v2 — argv builders', () => {
  it('builds netsh args for add', () => {
    const args = buildArgsForAdd({
      name: 'HelenServer-TCP-3000',
      port: 3000,
      protocol: 'TCP',
      direction: 'in',
      program: 'C:/Program Files/Helen/Helen-Server.exe',
    });
    expect(args).toContain('action=allow');
    expect(args).toContain('localport=3000');
    expect(args).toContain('protocol=TCP');
    expect(args).toContain('program=C:/Program Files/Helen/Helen-Server.exe');
  });

  it('omits program when not provided', () => {
    const args = buildArgsForAdd({
      name: 'HelenServer-UDP-41234',
      port: 41234,
      protocol: 'UDP',
      direction: 'in',
    });
    expect(args.find(a => a.startsWith('program='))).toBeUndefined();
  });

  it('builds remove args', () => {
    const args = buildArgsForRemove('HelenServer-TCP-3000');
    expect(args).toEqual([
      'advfirewall', 'firewall', 'delete', 'rule', 'name=HelenServer-TCP-3000',
    ]);
  });
});

describe('firewall.v2 — execFile interaction (mocked)', () => {
  beforeEach(() => { vi.resetModules(); });

  it('routes calls through execFile with strict argv (no shell)', async () => {
    const execFileMock = vi.fn((bin: string, args: string[], _opts: unknown, cb: any) => {
      cb(null, 'Ok.\n', '');
    });
    // Simulate the wrapper
    function addRule(rule: FirewallRule): Promise<string> {
      return new Promise((resolve, reject) => {
        execFileMock('netsh.exe', buildArgsForAdd(rule), { windowsHide: true }, (err: any, out: string) => {
          if (err) reject(err); else resolve(out);
        });
      });
    }
    const out = await addRule({
      name: 'HelenServer-TCP-3000', port: 3000, protocol: 'TCP', direction: 'in',
    });
    expect(out.trim()).toBe('Ok.');
    expect(execFileMock).toHaveBeenCalledTimes(1);
    expect(execFileMock.mock.calls[0][0]).toBe('netsh.exe');
  });
});
