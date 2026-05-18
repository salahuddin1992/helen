/**
 * Helen-Shell — generic Electron wrapper for any Helen web panel.
 *
 * One binary, one main process. Pick the panel at launch via:
 *
 *   electron . --id=admin
 *   electron . --id=vault
 *   electron . --id=ios
 *   electron . --url=http://127.0.0.1:3088/admin-secret/   (free-form)
 *
 * The registry in apps.json is the source of truth for built-in panels.
 * Pass --url to point at anything else (smoke-testing a remote server,
 * etc.).
 */

const { app, BrowserWindow, shell, Menu } = require('electron');
const path = require('path');
const fs   = require('fs');

// ── CLI parsing ────────────────────────────────────────────────────────

function parseArgs(argv) {
    // In dev (`electron . --id=...`) argv is [electron, ., --id=...]; in
    // prod (`Helen-Shell.exe --id=...`) it's [Helen-Shell.exe, --id=...].
    // `app.isPackaged` distinguishes; fall back to scanning every token
    // since Chromium can inject helpers (--enable-features=...) we want
    // to ignore — `--id`/`--url`/`--title`/`--width`/`--height` are the
    // only keys we care about.
    const accept = new Set(['id', 'url', 'title', 'width', 'height']);
    const out = {};
    for (const a of argv) {
        const m = a.match(/^--([^=]+)(?:=(.*))?$/);
        if (m && accept.has(m[1])) out[m[1]] = m[2] ?? true;
    }
    return out;
}

const args = parseArgs(process.argv);

// If this binary was packaged for a single specific panel (build-panels.js
// drops a panel.json next to main.js), the pinned id wins over CLI args
// unless the user explicitly overrides with --id. Lets us ship one .exe
// per panel where the user just double-clicks Helen-Admin-Setup.exe etc.
try {
    const pinned = JSON.parse(require('fs').readFileSync(path.join(__dirname, 'panel.json'), 'utf-8'));
    if (pinned && pinned.id && !args.id) args.id = pinned.id;
} catch { /* not a pinned-panel build, ignore */ }

// ── Resolve the target app ─────────────────────────────────────────────

function loadRegistry() {
    try {
        return JSON.parse(fs.readFileSync(path.join(__dirname, 'apps.json'), 'utf-8'));
    } catch (e) {
        console.error('[Helen-Shell] failed to read apps.json:', e.message);
        return { server: 'http://127.0.0.1:3088', apps: [] };
    }
}

function resolveTarget(args, registry) {
    const id = args.id || 'hub';
    if (args.url) {
        return {
            id:     args.id || 'custom',
            title:  args.title || 'Helen',
            url:    String(args.url),
            width:  Number(args.width)  || 1100,
            height: Number(args.height) || 800,
        };
    }
    const entry = registry.apps.find((a) => a.id === id);
    if (!entry) {
        console.error(`[Helen-Shell] unknown --id="${id}". Try one of:`,
            registry.apps.map((a) => a.id).join(', '));
        return null;
    }
    // An app can either declare a fully-qualified `url` (e.g. group-call
    // running on its own port 3099) or just a `path` to be appended to the
    // global `registry.server`. The fully-qualified form wins.
    const fullUrl = entry.url
        ? String(entry.url)
        : (registry.server || 'http://127.0.0.1:3088') + entry.path;
    return {
        id:     entry.id,
        title:  args.title  ? String(args.title)  : entry.title,
        url:    fullUrl,
        width:  Number(args.width)  || entry.width  || 1100,
        height: Number(args.height) || entry.height || 800,
    };
}

// ── Window factory ─────────────────────────────────────────────────────

function createWindow(target) {
    const win = new BrowserWindow({
        title:        target.title,
        width:        target.width,
        height:       target.height,
        minWidth:     360,
        minHeight:    400,
        backgroundColor: '#0d1018',
        autoHideMenuBar: true,
        webPreferences: {
            contextIsolation:    true,
            nodeIntegration:     false,
            sandbox:             true,
            preload:             path.join(__dirname, 'preload.js'),
            // Allow getUserMedia / microphone / camera prompts inside the
            // shell so iOS-sim and Desktop calling work when wrapped.
            webSecurity:         true,
            spellcheck:          false,
        },
    });

    // Strip the application menu so the shell stays minimal.
    Menu.setApplicationMenu(null);

    // Open external links (mailto:, https://help...) in the OS browser
    // rather than inside the shell.
    win.webContents.setWindowOpenHandler(({ url }) => {
        // Allow same-origin (the panel may pop a child window pointing back
        // at itself, e.g. a settings overlay). For everything else — most
        // commonly help links, GitHub repos, mailto: — hand off to the
        // OS browser/handler instead of silently denying. The previous
        // policy blocked all https:// links, which made operator-facing
        // support links dead-clickable.
        if (url.startsWith('http://127.0.0.1') || url.startsWith('http://localhost')) {
            return { action: 'allow' };
        }
        // Reject obviously dangerous schemes; let the rest go to the OS.
        const blocked = /^(file|javascript|data|chrome|chrome-extension):/i;
        if (blocked.test(url)) {
            console.warn('[Helen-Shell] blocked dangerous scheme:', url);
            return { action: 'deny' };
        }
        shell.openExternal(url).catch((err) => {
            console.warn('[Helen-Shell] openExternal failed:', err && err.message);
        });
        return { action: 'deny' };
    });

    win.loadURL(target.url).catch((err) => {
        console.error(`[Helen-Shell] loadURL("${target.url}") failed:`, err);
    });

    if (process.env.HELEN_SHELL_DEVTOOLS === '1') {
        win.webContents.openDevTools({ mode: 'detach' });
    }

    return win;
}

// ── Lifecycle ──────────────────────────────────────────────────────────

app.whenReady().then(() => {
    const registry = loadRegistry();
    const target   = resolveTarget(args, registry);
    if (!target) {
        app.exit(1);
        return;
    }
    console.log(`[Helen-Shell] opening ${target.id} → ${target.url}`);
    createWindow(target);

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow(target);
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});
