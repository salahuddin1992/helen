#!/usr/bin/env node
/**
 * sync-renderer.mjs — copy the desktop renderer build into ./www so
 * Capacitor can package it into the APK.
 *
 * The desktop project's `vite build` output goes to:
 *   ../CommClient-Desktop/dist-electron/renderer/
 *
 * This script:
 *   1. Verifies the desktop build exists (fails fast with help text).
 *   2. Mirrors it into ./www, replacing any prior copy.
 *   3. Patches index.html so it works under android_asset:// schema:
 *      - rewrites any absolute paths to relative
 *      - injects the mobile bridge shim that translates window.electronAPI
 *        calls to Capacitor plugin calls
 *
 * Run via `npm run build:web`.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync, rmSync, cpSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');
const DESKTOP_RENDERER = resolve(
  ROOT, '..', 'CommClient-Desktop', 'dist-electron', 'renderer'
);
const WWW = join(ROOT, 'www');

function fail(msg) {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
}

if (!existsSync(DESKTOP_RENDERER)) {
  fail(
    `Desktop renderer build not found at:\n  ${DESKTOP_RENDERER}\n\n` +
    `Build it first:\n` +
    `  cd ../CommClient-Desktop\n  npm run build:renderer\n`
  );
}

console.log(`→ syncing renderer from ${DESKTOP_RENDERER}`);
if (existsSync(WWW)) {
  rmSync(WWW, { recursive: true, force: true });
}
mkdirSync(WWW, { recursive: true });
cpSync(DESKTOP_RENDERER, WWW, { recursive: true });
console.log(`✓ copied to ${WWW}`);

// Inject the Capacitor → electronAPI shim so the existing renderer
// (which calls window.electronAPI.X) works unmodified inside a WebView.
const indexHtml = join(WWW, 'index.html');
if (!existsSync(indexHtml)) {
  fail(`Expected ${indexHtml} after copy — desktop build looks incomplete.`);
}
let html = readFileSync(indexHtml, 'utf-8');
const SHIM_MARKER = '<!-- HELEN-MOBILE-SHIM -->';
if (!html.includes(SHIM_MARKER)) {
  const shim =
    `${SHIM_MARKER}\n` +
    `  <script type="module" src="./mobile-bridge.js"></script>\n`;
  // Prepend so the shim runs before any module that touches window.electronAPI.
  html = html.replace('</head>', `  ${shim}</head>`);
  writeFileSync(indexHtml, html);
  console.log(`✓ injected mobile-bridge shim into index.html`);
}

// Drop the shim itself next to index.html so the relative <script src>
// resolves under android_asset://.
const SHIM_SRC = resolve(__dirname, 'mobile-bridge.js');
if (existsSync(SHIM_SRC)) {
  cpSync(SHIM_SRC, join(WWW, 'mobile-bridge.js'));
  console.log(`✓ shipped mobile-bridge.js into www`);
} else {
  console.warn(`⚠ ${SHIM_SRC} missing — shim was not copied.`);
}

console.log(`\n✓ sync complete — run \`npm run cap:sync\` next.`);
