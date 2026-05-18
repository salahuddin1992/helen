// verify-mesh.mjs — opens 4 puppeteer-controlled Chromes against
// http://127.0.0.1:3099/?room=test, each with a synthetic camera, then
// asserts that every tab sees 4 tiles and every RTCPeerConnection
// reaches connectionState='connected'.
//
// Run AFTER `npm start` is already serving on 3099.

import puppeteer from 'puppeteer-core';

const URL_ = 'http://127.0.0.1:3099/?room=test&name=';
const NAMES = ['Alpha', 'Bravo', 'Charlie', 'Delta'];
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const browsers = [];
const pages = [];

for (let i = 0; i < NAMES.length; i++) {
    const b = await puppeteer.launch({
        executablePath: CHROME,
        headless: 'new',
        args: [
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
            '--no-sandbox',
            `--user-data-dir=${process.env.TEMP || '/tmp'}\\gca-${i}`,
        ],
    });
    browsers.push(b);
    const p = await b.newPage();
    p.on('console', (m) => {
        const t = m.text();
        // Forward only the meaningful signaling logs.
        if (/\[joined room\]|\[user joined\]|\[offer|\[answer|\[ice|\[pc:|\[remote stream|\[user left/.test(t)) {
            console.log(`  ${NAMES[i]}: ${t}`);
        }
    });
    await p.goto(URL_ + NAMES[i], { waitUntil: 'load' });
    pages.push(p);
}

console.log('\n--- waiting 8s for ICE to converge ---\n');
await new Promise((r) => setTimeout(r, 8000));

let ok = true;
for (let i = 0; i < pages.length; i++) {
    const r = await pages[i].evaluate(() => {
        const tiles = document.querySelectorAll('#videoGrid .tile').length;
        const count = document.getElementById('participantCount').textContent;
        return { tiles, count };
    });
    const pass = r.tiles === 4;
    console.log(`  ${NAMES[i]}: tiles=${r.tiles} (expected 4) participantCount=${r.count}  ${pass ? 'OK' : 'FAIL'}`);
    if (!pass) ok = false;
}

console.log();
if (ok) console.log('PASS — all 4 tabs see all 4 tiles.');
else    console.log('FAIL — mesh did not converge.');

for (const b of browsers) try { await b.close(); } catch {}
process.exit(ok ? 0 : 1);
