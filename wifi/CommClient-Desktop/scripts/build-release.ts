/**
 * build-release.ts — Complete release build pipeline for CommClient.
 *
 * Orchestrates the full build sequence from source to distributable
 * installer. Run from the project root on a Windows machine with
 * all prerequisites installed.
 *
 * ════════════════════════════════════════════════════════════════
 *  PREREQUISITES (install once)
 * ════════════════════════════════════════════════════════════════
 *
 *  1. Node.js 18+ LTS             https://nodejs.org
 *  2. Python 3.10+                https://python.org
 *  3. PyInstaller                 pip install pyinstaller
 *  4. Git                         https://git-scm.com
 *  5. Visual Studio Build Tools   (for native Node modules if any)
 *     → workload: "Desktop development with C++"
 *
 * ════════════════════════════════════════════════════════════════
 *  BUILD STEPS (exact commands)
 * ════════════════════════════════════════════════════════════════
 *
 *  Step 1: Build Backend Server (PyInstaller)
 *  ─────────────────────────────────────────────
 *    cd CommClient-Server
 *    pip install -r requirements.txt
 *    pyinstaller CommClient.spec --clean --noconfirm
 *
 *    Output: CommClient-Server/dist/CommClient-Server/
 *    ├── CommClient-Server.exe     (main executable)
 *    └── _internal/                (Python runtime + dependencies)
 *
 *    NOTE: CommClient.spec must define:
 *      - name='CommClient-Server'
 *      - console=False (no console window)
 *      - icon='icon.ico'
 *      - hiddenimports for FastAPI, uvicorn, socket.io, etc.
 *
 *  Step 2: Build Frontend (Vite + TypeScript)
 *  ─────────────────────────────────────────────
 *    cd CommClient-Desktop
 *    npm ci                  # Clean install (deterministic)
 *    npm run build:renderer  # Vite build → dist-electron/renderer/
 *
 *    This also triggers vite-plugin-electron which builds:
 *      - dist-electron/main/index.js       (Electron main process)
 *      - dist-electron/preload/index.js    (preload script)
 *      - dist-electron/renderer/           (React app assets)
 *
 *  Step 3: Package Installer (electron-builder + NSIS)
 *  ─────────────────────────────────────────────────────
 *    npx electron-builder --win --config electron-builder.yml
 *
 *    Output: release/
 *    ├── CommClient Setup 1.0.0.exe    (NSIS installer ~80-120MB)
 *    ├── CommClient Setup 1.0.0.exe.blockmap
 *    └── builder-effective-config.yaml  (debug: resolved config)
 *
 *  Step 4: Verify Build
 *  ─────────────────────────────────────────────────────
 *    - Check release/ contains the .exe
 *    - Check file size is reasonable (80-150MB)
 *    - Run the installer on a clean test machine
 *    - Verify: app starts, server starts, LAN discovery works
 *
 * ════════════════════════════════════════════════════════════════
 *  ONE-LINE BUILD COMMAND
 * ════════════════════════════════════════════════════════════════
 *
 *    cd CommClient-Server && pyinstaller CommClient.spec --clean --noconfirm && cd ../CommClient-Desktop && npm ci && npm run build
 *
 *  Or using this script:
 *    cd CommClient-Desktop && npx ts-node scripts/build-release.ts
 *
 * ════════════════════════════════════════════════════════════════
 *  SILENT INSTALL / LAN DEPLOYMENT
 * ════════════════════════════════════════════════════════════════
 *
 *  Install:
 *    "CommClient Setup 1.0.0.exe" /S
 *
 *  Install to custom path:
 *    "CommClient Setup 1.0.0.exe" /S /D=C:\Apps\CommClient
 *
 *  Uninstall:
 *    "%LOCALAPPDATA%\Programs\CommClient\Uninstall CommClient.exe" /S
 *
 *  GPO / SCCM / batch deployment:
 *    \\fileserver\deploy\CommClient\install.bat:
 *      @echo off
 *      "\\fileserver\deploy\CommClient\CommClient Setup 1.0.0.exe" /S
 *
 * ════════════════════════════════════════════════════════════════
 *  VERSION BUMPING
 * ════════════════════════════════════════════════════════════════
 *
 *  Before building a new release:
 *    1. Update version in package.json
 *    2. Update buildVersion in electron-builder.yml
 *    3. Update CHANGELOG.md (if maintained)
 *    4. Commit and tag: git tag v1.1.0
 *    5. Run the build
 *
 * ════════════════════════════════════════════════════════════════
 */

import { execSync } from 'child_process';
import { existsSync, statSync, mkdirSync } from 'fs';
import { join, resolve } from 'path';

// ── Configuration ───────────────────────────────────────────

const PROJECT_ROOT = resolve(__dirname, '..');
const SERVER_ROOT = resolve(PROJECT_ROOT, '../CommClient-Server');
const RELEASE_DIR = join(PROJECT_ROOT, 'release');

interface BuildConfig {
  skipServer: boolean;
  skipFrontend: boolean;
  skipInstaller: boolean;
  verbose: boolean;
  clean: boolean;
}

function parseArgs(): BuildConfig {
  const args = process.argv.slice(2);
  return {
    skipServer: args.includes('--skip-server'),
    skipFrontend: args.includes('--skip-frontend'),
    skipInstaller: args.includes('--skip-installer'),
    verbose: args.includes('--verbose') || args.includes('-v'),
    clean: args.includes('--clean'),
  };
}

// ── Build Steps ─────────────────────────────────────────────

function run(cmd: string, cwd: string, label: string): void {
  console.log(`\n${'═'.repeat(60)}`);
  console.log(`  ${label}`);
  console.log(`${'═'.repeat(60)}`);
  console.log(`  cwd: ${cwd}`);
  console.log(`  cmd: ${cmd}\n`);

  try {
    execSync(cmd, {
      cwd,
      stdio: 'inherit',
      env: { ...process.env },
      windowsHide: false,
    });
    console.log(`  ✓ ${label} — SUCCESS`);
  } catch (err) {
    console.error(`  ✗ ${label} — FAILED`);
    process.exit(1);
  }
}

function checkPrerequisites(): void {
  console.log('\nChecking prerequisites...');

  // Node.js
  try {
    const nodeVer = execSync('node --version', { encoding: 'utf-8' }).trim();
    console.log(`  Node.js: ${nodeVer}`);
    const major = parseInt(nodeVer.replace('v', '').split('.')[0]);
    if (major < 18) {
      console.error('  ✗ Node.js 18+ required');
      process.exit(1);
    }
  } catch {
    console.error('  ✗ Node.js not found');
    process.exit(1);
  }

  // Python
  try {
    const pyVer = execSync('python --version', { encoding: 'utf-8' }).trim();
    console.log(`  Python: ${pyVer}`);
  } catch {
    console.error('  ✗ Python not found');
    process.exit(1);
  }

  // PyInstaller
  try {
    const piVer = execSync('pyinstaller --version', { encoding: 'utf-8' }).trim();
    console.log(`  PyInstaller: ${piVer}`);
  } catch {
    console.error('  ✗ PyInstaller not found. Run: pip install pyinstaller');
    process.exit(1);
  }

  // Check server source
  if (!existsSync(SERVER_ROOT)) {
    console.error(`  ✗ Server source not found at: ${SERVER_ROOT}`);
    console.error('    Expected: ../CommClient-Server/ relative to CommClient-Desktop/');
    process.exit(1);
  }

  // Check CommClient.spec
  const specFile = join(SERVER_ROOT, 'CommClient.spec');
  if (!existsSync(specFile)) {
    console.error(`  ✗ CommClient.spec not found at: ${specFile}`);
    process.exit(1);
  }

  console.log('  ✓ All prerequisites satisfied\n');
}

function buildServer(): void {
  const specFile = join(SERVER_ROOT, 'CommClient.spec');

  // Install Python dependencies
  const reqFile = join(SERVER_ROOT, 'requirements.txt');
  if (existsSync(reqFile)) {
    run('pip install -r requirements.txt', SERVER_ROOT, 'Step 1a: Install Python dependencies');
  }

  // Build with PyInstaller
  run(
    'pyinstaller CommClient.spec --clean --noconfirm',
    SERVER_ROOT,
    'Step 1b: Build backend server (PyInstaller)',
  );

  // Verify output
  const serverExe = join(SERVER_ROOT, 'dist', 'CommClient-Server', 'CommClient-Server.exe');
  if (!existsSync(serverExe)) {
    console.error(`  ✗ Server executable not found at: ${serverExe}`);
    process.exit(1);
  }
  const size = statSync(serverExe).size;
  console.log(`  Server exe size: ${(size / 1024 / 1024).toFixed(1)} MB`);
}

function buildFrontend(): void {
  // Clean install dependencies
  run('npm ci', PROJECT_ROOT, 'Step 2a: Install Node.js dependencies');

  // Type check
  run('npx tsc --noEmit', PROJECT_ROOT, 'Step 2b: TypeScript type check');

  // Build renderer + main + preload
  run('npm run build:renderer', PROJECT_ROOT, 'Step 2c: Build frontend (Vite)');

  // Verify output
  const mainJs = join(PROJECT_ROOT, 'dist-electron', 'main', 'index.js');
  const preloadJs = join(PROJECT_ROOT, 'dist-electron', 'preload', 'index.js');
  const rendererIndex = join(PROJECT_ROOT, 'dist-electron', 'renderer', 'index.html');

  if (!existsSync(mainJs)) {
    console.error('  ✗ Main process bundle not found');
    process.exit(1);
  }
  if (!existsSync(preloadJs)) {
    console.error('  ✗ Preload script not found');
    process.exit(1);
  }
  if (!existsSync(rendererIndex)) {
    console.error('  ✗ Renderer build not found');
    process.exit(1);
  }
  console.log('  ✓ All build outputs verified');
}

function buildInstaller(): void {
  run(
    'npx electron-builder --win --config electron-builder.yml',
    PROJECT_ROOT,
    'Step 3: Package installer (electron-builder + NSIS)',
  );

  // Verify output
  if (!existsSync(RELEASE_DIR)) {
    console.error('  ✗ Release directory not created');
    process.exit(1);
  }

  // Find the installer .exe
  const files = require('fs').readdirSync(RELEASE_DIR);
  const installer = files.find((f: string) => f.endsWith('.exe') && f.includes('Setup'));
  if (!installer) {
    console.error('  ✗ Installer .exe not found in release/');
    process.exit(1);
  }

  const installerPath = join(RELEASE_DIR, installer);
  const size = statSync(installerPath).size;
  console.log(`\n  ✓ Installer: ${installer}`);
  console.log(`  ✓ Size: ${(size / 1024 / 1024).toFixed(1)} MB`);
  console.log(`  ✓ Path: ${installerPath}`);
}

function cleanBuild(): void {
  run('npm run clean', PROJECT_ROOT, 'Clean previous build artifacts');
}

// ── Main ────────────────────────────────────────────────────

async function main(): Promise<void> {
  const config = parseArgs();
  const startTime = Date.now();

  console.log('╔══════════════════════════════════════════════════════════╗');
  console.log('║         CommClient — Release Build Pipeline             ║');
  console.log('╚══════════════════════════════════════════════════════════╝');

  checkPrerequisites();

  if (config.clean) {
    cleanBuild();
  }

  if (!config.skipServer) {
    buildServer();
  } else {
    console.log('\n  ⊘ Skipping server build (--skip-server)');
  }

  if (!config.skipFrontend) {
    buildFrontend();
  } else {
    console.log('\n  ⊘ Skipping frontend build (--skip-frontend)');
  }

  if (!config.skipInstaller) {
    buildInstaller();
  } else {
    console.log('\n  ⊘ Skipping installer build (--skip-installer)');
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log(`\n${'═'.repeat(60)}`);
  console.log(`  BUILD COMPLETE — ${elapsed}s total`);
  console.log(`${'═'.repeat(60)}\n`);
  console.log('  Next steps:');
  console.log('    1. Test: run the installer on a clean Windows machine');
  console.log('    2. Verify: app starts, server starts, LAN discovery works');
  console.log('    3. Deploy: copy installer to shared network folder');
  console.log('    4. Silent: "CommClient Setup x.y.z.exe" /S');
  console.log('');
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
