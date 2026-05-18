/**
 * Cert Trust Resolver — Connectivity Hotfix Layer (Module D).
 *
 * The bundled Helen server presents either a Helen-CA-signed cert or a
 * self-signed dev cert. Default Chromium policy rejects both, so every
 * `fetch()` from the renderer fails with `ERR_CERT_AUTHORITY_INVALID`.
 *
 * This module installs a custom certificate verifier on an Electron
 * `Session` that accepts a cert only when AT LEAST ONE of the
 * following is true:
 *
 *   1. The cert (or any issuer in its chain) matches the Helen CA
 *      certificate persisted at `%APPDATA%/CommClient/data/ca.pem`.
 *   2. The cert's SHA-256 fingerprint appears in
 *      `%APPDATA%/CommClient/security/cert-pins.json`.
 *   3. The cert's CN/SAN matches one of the canonical Helen names
 *      (`*.helen.local`, `helen-server.*`, `127.0.0.1`, `localhost`).
 *
 * Everything else falls back to Chromium's default verification result,
 * which the user can then upgrade via :func:`showCertTrustDialog`.
 *
 * Concurrent writes to the pins file are serialised with a small
 * file-lock primitive (`.lock` sidecar with retry) so a startup spike
 * with multiple windows cannot corrupt the JSON.
 */

import { promises as fsp, existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { createHash, X509Certificate } from 'node:crypto';
import { execFile } from 'node:child_process';
import { app, dialog, session, type Certificate, type Session } from 'electron';

// ─────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────

const SECURITY_SUBDIR = 'security';
const DATA_SUBDIR = 'data';
const PINS_FILENAME = 'cert-pins.json';
const CA_FILENAME = 'ca.pem';
const LOCK_SUFFIX = '.lock';

/** Cert names that always pass without prompting the user. */
const HELEN_NAME_PATTERNS: RegExp[] = [
    /^\*\.helen\.local$/i,
    /^.+\.helen\.local$/i,
    /^helen-server\..*$/i,
    /^helen-server$/i,
    /^127\.0\.0\.1$/,
    /^localhost$/i,
];

/** Pins file payload shape. */
interface PinsFile {
    version: 1;
    fingerprints: string[];
    /** Optional metadata keyed by fingerprint. */
    meta?: Record<string, { addedAt: number; cn?: string; reason?: string }>;
}

/** Result returned from `setCertificateVerifyProc`. */
type VerifyResult = -3 | -2 | -1 | 0 | -202 | -200;

// ─────────────────────────────────────────────────────────────────────
// Path helpers
// ─────────────────────────────────────────────────────────────────────

function getAppDataRoot(): string {
    // app.getPath('appData') is %APPDATA% on Windows, ~/Library/Application Support
    // on macOS, and $XDG_CONFIG_HOME or ~/.config on Linux.
    return join(app.getPath('appData'), 'CommClient');
}

function getSecurityDir(): string {
    return join(getAppDataRoot(), SECURITY_SUBDIR);
}

function getDataDir(): string {
    return join(getAppDataRoot(), DATA_SUBDIR);
}

function getPinsPath(): string {
    return join(getSecurityDir(), PINS_FILENAME);
}

function getCaPath(): string {
    return join(getDataDir(), CA_FILENAME);
}

function log(level: 'info' | 'warn' | 'error', msg: string, extra?: Record<string, unknown>): void {
    const tag = '[certTrust]';
    const line = extra ? `${msg} ${JSON.stringify(extra)}` : msg;
    if (level === 'info') console.log(tag, line);
    else if (level === 'warn') console.warn(tag, line);
    else console.error(tag, line);
}

// ─────────────────────────────────────────────────────────────────────
// File lock — defends against concurrent writes from multiple windows
// ─────────────────────────────────────────────────────────────────────

async function acquireLock(path: string, retries = 30, intervalMs = 50): Promise<void> {
    const lockPath = path + LOCK_SUFFIX;
    for (let i = 0; i < retries; i++) {
        try {
            // O_CREAT|O_EXCL — fails atomically if the lock already exists.
            const fd = await fsp.open(lockPath, 'wx');
            await fd.close();
            return;
        } catch (err) {
            if ((err as NodeJS.ErrnoException).code !== 'EEXIST') throw err;
            await new Promise((resolve) => setTimeout(resolve, intervalMs));
        }
    }
    throw new Error(`certTrust: could not acquire lock ${lockPath}`);
}

async function releaseLock(path: string): Promise<void> {
    const lockPath = path + LOCK_SUFFIX;
    try { await fsp.unlink(lockPath); } catch { /* ignore — best-effort */ }
}

// ─────────────────────────────────────────────────────────────────────
// Pins file I/O
// ─────────────────────────────────────────────────────────────────────

async function ensureSecurityDir(): Promise<void> {
    await fsp.mkdir(getSecurityDir(), { recursive: true });
}

function normalizeFingerprint(fp: string): string {
    // Accept "sha256/AB:CD:…", "AB:CD:…", or raw hex; output canonical
    // lower-case hex with no separators.
    return fp
        .replace(/^sha256\//i, '')
        .replace(/[^0-9A-Fa-f]/g, '')
        .toLowerCase();
}

function readPinsFileSync(): PinsFile {
    const path = getPinsPath();
    if (!existsSync(path)) {
        return { version: 1, fingerprints: [], meta: {} };
    }
    try {
        const raw = readFileSync(path, 'utf-8');
        const parsed = JSON.parse(raw) as Partial<PinsFile>;
        const fingerprints = Array.isArray(parsed.fingerprints)
            ? parsed.fingerprints.filter((s): s is string => typeof s === 'string').map(normalizeFingerprint)
            : [];
        return {
            version: 1,
            fingerprints,
            meta: (parsed.meta && typeof parsed.meta === 'object') ? parsed.meta as PinsFile['meta'] : {},
        };
    } catch (err) {
        log('warn', 'pins file unreadable, treating as empty', { error: (err as Error).message });
        return { version: 1, fingerprints: [], meta: {} };
    }
}

async function writePinsFile(payload: PinsFile): Promise<void> {
    await ensureSecurityDir();
    const path = getPinsPath();
    const tmp = path + '.tmp';
    await acquireLock(path);
    try {
        const data = JSON.stringify(payload, null, 2) + '\n';
        await fsp.writeFile(tmp, data, { encoding: 'utf-8', mode: 0o600 });
        await fsp.rename(tmp, path);
    } finally {
        await releaseLock(path);
    }
}

// ─────────────────────────────────────────────────────────────────────
// Public — pin management
// ─────────────────────────────────────────────────────────────────────

/** Return the current set of trusted SHA-256 fingerprints. */
export function getPinnedFingerprints(): Set<string> {
    const file = readPinsFileSync();
    return new Set(file.fingerprints);
}

export async function addPinnedFingerprint(fp: string, reason?: string, cn?: string): Promise<void> {
    const normalized = normalizeFingerprint(fp);
    if (normalized.length !== 64) throw new Error(`Invalid SHA-256 fingerprint: ${fp}`);
    const file = readPinsFileSync();
    if (!file.fingerprints.includes(normalized)) {
        file.fingerprints.push(normalized);
    }
    file.meta = file.meta ?? {};
    file.meta[normalized] = {
        addedAt: Date.now(),
        reason: reason ?? 'user-trust',
        cn: cn ?? undefined,
    };
    await writePinsFile(file);
    log('info', 'pin added', { fp: normalized.slice(0, 12) + '…' });
}

export async function removePin(fp: string): Promise<void> {
    const normalized = normalizeFingerprint(fp);
    const file = readPinsFileSync();
    file.fingerprints = file.fingerprints.filter((f) => f !== normalized);
    if (file.meta) delete file.meta[normalized];
    await writePinsFile(file);
    log('info', 'pin removed', { fp: normalized.slice(0, 12) + '…' });
}

// ─────────────────────────────────────────────────────────────────────
// CA verification
// ─────────────────────────────────────────────────────────────────────

let cachedCa: { pem: string; cert: X509Certificate; spkiSha256: string } | null = null;
let cachedCaMtime = 0;

function loadCaIfPresent(): typeof cachedCa {
    const path = getCaPath();
    if (!existsSync(path)) return null;
    try {
        const stat = require('node:fs').statSync(path);
        if (cachedCa && stat.mtimeMs === cachedCaMtime) return cachedCa;
        const pem = readFileSync(path, 'utf-8');
        const cert = new X509Certificate(pem);
        const spki = cert.publicKey.export({ type: 'spki', format: 'der' });
        const spkiSha256 = createHash('sha256').update(spki).digest('hex');
        cachedCa = { pem, cert, spkiSha256 };
        cachedCaMtime = stat.mtimeMs;
        return cachedCa;
    } catch (err) {
        log('warn', 'ca.pem unreadable', { error: (err as Error).message });
        return null;
    }
}

function chainSignedByHelenCa(leaf: Certificate): boolean {
    const ca = loadCaIfPresent();
    if (!ca) return false;
    // Walk Electron's flattened issuer chain. Each `Certificate` carries
    // `issuerCert` recursively up to (and including) a self-signed root.
    let current: Certificate | undefined = leaf;
    let depth = 0;
    while (current && depth < 10) {
        try {
            const candidate = new X509Certificate(current.data);
            // Match by serial + issuer OR by SPKI digest — issuers can re-sign with new serials.
            if (candidate.serialNumber === ca.cert.serialNumber
                && candidate.issuer === ca.cert.issuer) {
                return true;
            }
            const spki = candidate.publicKey.export({ type: 'spki', format: 'der' });
            const spkiSha256 = createHash('sha256').update(spki).digest('hex');
            if (spkiSha256 === ca.spkiSha256) return true;
        } catch { /* malformed entry — skip */ }
        current = current.issuerCert;
        depth++;
    }
    return false;
}

// ─────────────────────────────────────────────────────────────────────
// Cert helpers
// ─────────────────────────────────────────────────────────────────────

function sha256FingerprintOf(cert: Certificate): string {
    // Electron exposes the DER data via `.data` (PEM string). Re-parse
    // with X509Certificate so we get the canonical DER bytes.
    try {
        const x = new X509Certificate(cert.data);
        return x.fingerprint256.replace(/:/g, '').toLowerCase();
    } catch {
        // Fall back to hashing the PEM body — less precise but never crashes.
        const body = cert.data.replace(/-----[^-]+-----/g, '').replace(/\s+/g, '');
        const der = Buffer.from(body, 'base64');
        return createHash('sha256').update(der).digest('hex');
    }
}

function extractNames(cert: Certificate): string[] {
    const out: string[] = [];
    if (cert.subjectName) out.push(cert.subjectName);
    try {
        const x = new X509Certificate(cert.data);
        if (x.subjectAltName) {
            for (const entry of x.subjectAltName.split(',')) {
                const [, value] = entry.split(':');
                if (value) out.push(value.trim());
            }
        }
        if (x.subject) {
            for (const line of x.subject.split('\n')) {
                const m = /^CN=(.+)$/.exec(line.trim());
                if (m) out.push(m[1]);
            }
        }
    } catch { /* ignore */ }
    return out.map((n) => n.trim()).filter(Boolean);
}

function matchesHelenName(cert: Certificate): boolean {
    const names = extractNames(cert);
    return names.some((name) => HELEN_NAME_PATTERNS.some((re) => re.test(name)));
}

// ─────────────────────────────────────────────────────────────────────
// Verify proc installation
// ─────────────────────────────────────────────────────────────────────

/**
 * Install the verification procedure on the given Electron `Session`.
 *
 * Return codes (Electron contract):
 *   0   — trust the cert (skip Chromium's default check).
 *   -2  — abort the request (use this only on outright malicious certs).
 *   -3  — defer to Chromium's default verification result.
 */
export function installCertVerifyProc(targetSession?: Session): void {
    const ses = targetSession ?? session.defaultSession;
    if (!ses) {
        log('warn', 'no Electron session available; skipping verify proc');
        return;
    }
    ses.setCertificateVerifyProc((request, callback) => {
        try {
            const { hostname, certificate, verificationResult } = request;
            // Quick-path: Chromium already approved the cert.
            if (verificationResult === 'net::OK') {
                callback(0);
                return;
            }

            // Path 1 — SHA-256 pin match.
            const fp = sha256FingerprintOf(certificate);
            const pinned = getPinnedFingerprints();
            if (pinned.has(fp)) {
                log('info', 'cert accepted via pin', { hostname, fp: fp.slice(0, 12) + '…' });
                callback(0);
                return;
            }

            // Path 2 — chain trusted by Helen CA.
            if (chainSignedByHelenCa(certificate)) {
                log('info', 'cert accepted via Helen CA', { hostname });
                callback(0);
                return;
            }

            // Path 3 — canonical Helen name (CN or SAN).
            if (matchesHelenName(certificate)) {
                log('info', 'cert accepted via Helen name pattern', { hostname });
                callback(0);
                return;
            }

            // Otherwise: defer to Chromium's default decision.
            callback(-3);
        } catch (err) {
            log('error', 'verifyProc threw', { error: (err as Error).message });
            callback(-3);
        }
    });
    log('info', 'verify proc installed', { partition: ses.storagePath ?? 'default' });
}

// ─────────────────────────────────────────────────────────────────────
// Interactive trust dialog
// ─────────────────────────────────────────────────────────────────────

export interface CertTrustDialogResult {
    /** True iff the user chose to trust. */
    trusted: boolean;
    /** When true, the pin was persisted. */
    permanent: boolean;
    /** The SHA-256 fingerprint that was offered. */
    fingerprint: string;
}

/**
 * Present a modal trust dialog showing every relevant cert detail and
 * three buttons: Trust Permanently, Trust Once, Cancel.
 *
 * `Trust Permanently` persists the fingerprint to the pins file.
 * `Trust Once` returns `trusted=true` without persisting — caller must
 * keep the fingerprint in-memory for the lifetime of the session.
 */
export async function showCertTrustDialog(cert: Certificate): Promise<CertTrustDialogResult> {
    const names = extractNames(cert);
    const fingerprint = sha256FingerprintOf(cert);
    let validity = 'unknown';
    let issuer = cert.issuerName ?? 'unknown';
    try {
        const x = new X509Certificate(cert.data);
        validity = `${x.validFrom}  →  ${x.validTo}`;
        if (x.issuer) issuer = x.issuer.split('\n').join(' / ');
    } catch { /* fall through */ }

    const detail =
        `Subject names:\n  ${names.join('\n  ') || '(none)'}\n\n`
        + `Issuer:\n  ${issuer}\n\n`
        + `Validity:\n  ${validity}\n\n`
        + `SHA-256:\n  ${fingerprint.match(/.{1,2}/g)?.join(':') ?? fingerprint}`;

    const { response } = await dialog.showMessageBox({
        type: 'warning',
        title: 'Untrusted Server Certificate',
        message: 'The Helen server presented a certificate that is not yet trusted.',
        detail,
        buttons: ['Trust Permanently', 'Trust Once', 'Cancel'],
        defaultId: 2,
        cancelId: 2,
        noLink: true,
    });

    if (response === 0) {
        try {
            await addPinnedFingerprint(fingerprint, 'user-dialog', names[0]);
        } catch (err) {
            log('warn', 'failed to persist pin', { error: (err as Error).message });
        }
        return { trusted: true, permanent: true, fingerprint };
    }
    if (response === 1) {
        return { trusted: true, permanent: false, fingerprint };
    }
    return { trusted: false, permanent: false, fingerprint };
}

// ─────────────────────────────────────────────────────────────────────
// Windows trust store integration
// ─────────────────────────────────────────────────────────────────────

/**
 * Install a CA into the per-user `Root` store via `certutil`. Per-user
 * (not machine) so we never need admin and never affect other accounts.
 *
 * Throws when the platform is not Windows or `certutil` exits non-zero.
 */
export function installToWindowsTrustStore(certPath: string): Promise<void> {
    return new Promise((resolve, reject) => {
        if (process.platform !== 'win32') {
            reject(new Error('installToWindowsTrustStore is Windows-only'));
            return;
        }
        if (!existsSync(certPath)) {
            reject(new Error(`certificate not found: ${certPath}`));
            return;
        }
        // `certutil -addstore -user Root <path>` — per-user trust, no UAC.
        execFile(
            'certutil',
            ['-addstore', '-user', 'Root', certPath],
            { windowsHide: true, timeout: 15_000 },
            (err, stdout, stderr) => {
                if (err) {
                    reject(new Error(
                        `certutil failed: ${(err as Error).message}; stderr=${stderr?.toString().trim()}`,
                    ));
                    return;
                }
                log('info', 'cert installed into user Root store', { certPath });
                resolve();
            },
        );
    });
}

// ─────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────

/** Convenience — fingerprint a `Certificate` exactly like the verify proc. */
export function fingerprintOf(cert: Certificate): string {
    return sha256FingerprintOf(cert);
}

/** Diagnostic — return a structured snapshot of every trust input. */
export function snapshotTrustState(): {
    caLoaded: boolean;
    caPath: string;
    pinsPath: string;
    pinCount: number;
    namePatterns: string[];
} {
    const ca = loadCaIfPresent();
    const pins = getPinnedFingerprints();
    return {
        caLoaded: ca !== null,
        caPath: getCaPath(),
        pinsPath: getPinsPath(),
        pinCount: pins.size,
        namePatterns: HELEN_NAME_PATTERNS.map((r) => r.source),
    };
}

const certTrust = {
    installCertVerifyProc,
    getPinnedFingerprints,
    addPinnedFingerprint,
    removePin,
    showCertTrustDialog,
    installToWindowsTrustStore,
    fingerprintOf,
    snapshotTrustState,
};

export default certTrust;
