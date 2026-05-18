/**
 * build-panels.js — produce one .exe installer per registered panel.
 *
 * Each panel becomes its own NSIS installer (Helen-Admin-Setup.exe,
 * Helen-iOS-Setup.exe, Helen-Vault-Setup.exe …) so users can install
 * just the panels they need without bundling Helen-Shell as a generic
 * launcher. Each installer hard-codes the panel id at build time via
 * a generated `panel.json`.
 *
 * Build all:    node build-panels.js
 * Build one:    node build-panels.js admin
 */

const { execSync } = require('child_process');
const fs   = require('fs');
const path = require('path');

const REGISTRY = JSON.parse(fs.readFileSync(path.join(__dirname, 'apps.json'), 'utf-8'));
const ARG = process.argv[2];      // optional panel id

const targets = ARG
    ? REGISTRY.apps.filter((a) => a.id === ARG)
    : REGISTRY.apps;

if (!targets.length) {
    console.error(`unknown panel id: ${ARG}. Try one of:`,
        REGISTRY.apps.map((a) => a.id).join(', '));
    process.exit(1);
}

// Per-panel output dir so artifacts don't collide.
const RELEASE_BASE = path.join(__dirname, 'release-panels');
fs.mkdirSync(RELEASE_BASE, { recursive: true });

for (const app of targets) {
    const id    = app.id;
    const title = app.title || id;
    const safeName = `Helen-${id.replace(/[^a-zA-Z0-9]/g, '-')}`;
    console.log(`\n=== ${id} → ${safeName} ===\n`);

    // Pin the panel id in a panel.json that main.js reads on boot.
    fs.writeFileSync(path.join(__dirname, 'panel.json'), JSON.stringify({ id }, null, 2));

    // Generate a one-off electron-builder config for this panel.
    const cfg = `appId: com.helen.${id.replace(/[^a-z0-9]/g, '')}
productName: ${safeName}
directories:
  output: release-panels/${id}
  buildResources: build
files:
  - main.js
  - preload.js
  - apps.json
  - panel.json
  - package.json
asar: true
win:
  target:
    - target: nsis
      arch: [x64]
  artifactName: "${safeName}-Setup-1.0.0.exe"
nsis:
  oneClick: false
  perMachine: false
  allowToChangeInstallationDirectory: true
  createDesktopShortcut: true
  createStartMenuShortcut: true
  shortcutName: "${title}"
  uninstallDisplayName: "${title} 1.0.0"
`;
    const cfgPath = path.join(__dirname, `electron-builder-${id}.yml`);
    fs.writeFileSync(cfgPath, cfg);

    try {
        execSync(
            `npx electron-builder --win --config "${cfgPath}"`,
            { cwd: __dirname, stdio: 'inherit' },
        );
    } catch (err) {
        console.error(`[!] build failed for ${id}:`, err.message);
        process.exit(2);
    } finally {
        fs.unlinkSync(cfgPath);
    }
}

// Cleanup the per-panel config file.
try { fs.unlinkSync(path.join(__dirname, 'panel.json')); } catch {}

console.log('\n=== all panels built ===');
console.log(`output: ${RELEASE_BASE}/<panel-id>/<safeName>-Setup-1.0.0.exe`);
