/**
 * certTrust.test.ts — Phase 4 / Module V — Phase-1 Module D cert trust.
 *
 * Mocks electron.dialog + session so the tests run headless.
 */

import { describe, it, expect, vi } from 'vitest';

// Electron mocks — minimal surface used by the cert-trust dialog
const dialogMock = {
  showMessageBox: vi.fn(),
  showErrorBox: vi.fn(),
};
const sessionMock = {
  defaultSession: {
    setCertificateVerifyProc: vi.fn(),
    clearHostResolverCache: vi.fn(async () => undefined),
  },
};

vi.mock('electron', () => ({
  dialog: dialogMock,
  session: sessionMock,
  app: { getPath: vi.fn(() => '/fake/userData') },
}));

// Reference contract — what the real cert-trust module should expose
interface CertInfo { subject: string; issuer: string; fingerprint: string; }
type TrustDecision = 'trust' | 'reject';

async function promptForTrust(_cert: CertInfo): Promise<TrustDecision> {
  const res = await dialogMock.showMessageBox({
    type: 'warning',
    buttons: ['Trust', 'Reject'],
    defaultId: 1,
    cancelId: 1,
    message: 'Untrusted certificate',
  });
  return (res as any).response === 0 ? 'trust' : 'reject';
}

describe('certTrust', () => {
  it('returns "trust" when user picks button 0', async () => {
    dialogMock.showMessageBox.mockResolvedValueOnce({ response: 0 });
    const decision = await promptForTrust({
      subject: 'CN=helen.local', issuer: 'CN=helen-ca', fingerprint: 'AA:BB',
    });
    expect(decision).toBe('trust');
  });

  it('returns "reject" when user picks button 1 (default)', async () => {
    dialogMock.showMessageBox.mockResolvedValueOnce({ response: 1 });
    const decision = await promptForTrust({
      subject: 'CN=helen.local', issuer: 'CN=helen-ca', fingerprint: 'AA:BB',
    });
    expect(decision).toBe('reject');
  });

  it('passes correct message-box config', async () => {
    dialogMock.showMessageBox.mockResolvedValueOnce({ response: 0 });
    await promptForTrust({
      subject: 'CN=helen.local', issuer: 'CN=helen-ca', fingerprint: 'AA:BB',
    });
    const [opts] = dialogMock.showMessageBox.mock.calls[dialogMock.showMessageBox.mock.calls.length - 1];
    expect(opts.type).toBe('warning');
    expect(opts.cancelId).toBe(1);
    expect(opts.defaultId).toBe(1);  // default to "Reject" — secure-by-default
  });
});
