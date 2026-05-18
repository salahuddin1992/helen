/**
 * Preload — runs in the renderer with limited Node access.
 *
 * Intentionally tiny. The web panels we host (admin, vault, etc.) were
 * built to run in a regular browser, so they don't expect Node APIs and
 * we keep the surface minimal for security.
 *
 * The `helenShell` API exposes only the things a panel might want:
 *   - which app it is (so a panel can reuse one HTML file across roles)
 *   - the server URL it was launched against
 */

const { contextBridge } = require('electron');

function getCliFlag(name) {
    const arg = process.argv.find((a) => a.startsWith(`--${name}=`));
    return arg ? arg.split('=').slice(1).join('=') : null;
}

contextBridge.exposeInMainWorld('helenShell', {
    isShell:   true,
    appId:     getCliFlag('id') || 'unknown',
    serverUrl: getCliFlag('server') || null,
});
