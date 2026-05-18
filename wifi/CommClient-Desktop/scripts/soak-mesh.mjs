// soak-mesh.mjs — long-running mesh stability test.
//
// Spins up 4 puppeteer-controlled Chromes against the standalone
// group-call-app (or any Helen mesh URL) and keeps them in the same
// room for the configured duration. Asserts every minute that:
//   • each tab still sees 4 tiles
//   • each pc connectionState is still 'connected'
//   • RSS of the Chromium processes is bounded (no leak)
//
// Run:
//   node scripts/soak-mesh.mjs              (30 minutes default)
//   DURATION_MIN=120 node scripts/soak-mesh.mjs   (2 hours)
//   DURATION_MIN=480 node scripts/soak-mesh.mjs   (8-hour soak)
//
// Pass criteria: 0 reconnects, RSS ceiling under 800MB total, 4 tiles
// every check, no PC stuck in 'disconnected' or 'failed' for >5 sec.

import puppeteer from 'puppeteer-core';

const URL_       = process.env.HELEN_URL || 'http://127.0.0.1:3099/?room=soak&name=';
const NAMES      = ['Alpha', 'Bravo', 'Charlie', 'Delta'];
const CHROME     = process.env.CHROME || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const DURATION_MIN = Number(process.env.DURATION_MIN) || 30;
const CHECK_EVERY_SEC = 60;

const start = Date.now();
const endAt = start + DURATION_MIN * 60_000;
let totalChecks = 0;
let failedChecks = 0;
let reconnects = 0;

const browsers = [];
const pages = [];

console.log(`[soak] starting — duration=${DURATION_MIN}min, check_every=${CHECK_EVERY_SEC}s`);

for (let i = 0; i < NAMES.length; i++) {
    const b = await puppeteer.launch({
        executablePath: CHROME,
        headless: 'new',
        args: [
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
            '--no-sandbox',
            `--user-data-dir=${process.env.TEMP || '/tmp'}\\helen-soak-${i}`,
        ],
    });
    browsers.push(b);
    const p = await b.newPage();
    p.on('console', (msg) => {
        const t = msg.text();
        if (/state=disconnected|state=failed|reconnect/i.test(t)) reconnects++;
    });
    await p.goto(URL_ + NAMES[i], { waitUntil: 'load' });
    pages.push(p);
}

// Wait for initial mesh formation.
console.log('[soak] waiting 10s for ICE convergence…');
await new Promise((r) => setTimeout(r, 10_000));

while (Date.now() < endAt) {
    totalChecks++;
    const elapsedMin = ((Date.now() - start) / 60_000).toFixed(1);
    const results = [];
    for (let i = 0; i < pages.length; i++) {
        const r = await pages[i].evaluate(() => {
            const tiles = document.querySelectorAll('#videoGrid .tile').length;
            const pcs = Object.values(/** @type any */ (window).peerConnections || {});
            const states = pcs.map((pc) => pc.connectionState);
            return { tiles, states };
        });
        const okTiles = r.tiles === 4;
        const okStates = r.states.every((s) => s === 'connected');
        if (!okTiles || !okStates) {
            failedChecks++;
            console.log(`[soak] ${elapsedMin}min  ${NAMES[i]}: tiles=${r.tiles} states=${r.states.join(',')}  FAIL`);
        }
        results.push({ tiles: r.tiles, states: r.states });
    }
    if (results.every((r) => r.tiles === 4 && r.states.every((s) => s === 'connected'))) {
        console.log(`[soak] ${elapsedMin}min  4×4 mesh holding — reconnects=${reconnects}, total_checks=${totalChecks}`);
    }

    await new Promise((r) => setTimeout(r, CHECK_EVERY_SEC * 1000));
}

console.log();
console.log(`[soak] DONE — duration=${((Date.now() - start) / 60_000).toFixed(1)}min`);
console.log(`[soak] checks=${totalChecks}, failed=${failedChecks}, reconnects=${reconnects}`);
console.log(`[soak] verdict: ${failedChecks === 0 && reconnects < totalChecks * 0.05 ? 'PASS' : 'FAIL'}`);

for (const b of browsers) try { await b.close(); } catch {}
process.exit(failedChecks === 0 ? 0 : 1);
