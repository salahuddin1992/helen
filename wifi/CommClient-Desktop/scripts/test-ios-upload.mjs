// Verify the iOS web sim's file-attach flow:
// 1. + button opens attach menu (Photo / File)
// 2. Selecting opens an <input type=file>
// 3. File is uploaded via /api/files/upload
// 4. Chat message with file_id is sent
// 5. Recipient sees the message
//
// We bypass the OS file picker by injecting a File directly onto the
// hidden <input>.

import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const URL  = 'http://127.0.0.1:3088/mobile/?v=' + Date.now();
const PASS = 'Noor1993!!$';
const USER = 'yousef2';

const chrome = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].find((p) => { try { fs.statSync(p); return true; } catch { return false; } });
if (!chrome) { console.error('Chrome not found'); process.exit(2); }

const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: 'new',
    args: ['--disable-application-cache'],
});
const page = await browser.newPage();
await page.setViewport({ width: 430, height: 932 });
page.on('console', (m) => {
    const t = m.type();
    if (t === 'log' || t === 'warn' || t === 'error') console.log(`  [${t}] ${m.text()}`);
});
page.on('pageerror', (e) => console.log('  [pageerror]', e.message));
page.on('response', (r) => {
    if (r.status() >= 400) console.log('  [http', r.status() + ']', r.url());
    // Also log all uploads
    if (r.url().includes('/api/files/upload')) {
        console.log('  [upload', r.status() + ']', r.url());
    }
});
page.on('request', (r) => {
    if (r.url().includes('/api/files/upload')) {
        console.log('  [→ upload]', r.method(), r.url());
    }
});

console.log('1) navigate + login');
await page.goto(URL, { waitUntil: 'networkidle2', timeout: 15_000 });
const lr = await fetch('http://127.0.0.1:3088/api/auth/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: USER, password: PASS}),
});
const ld = await lr.json();
await page.evaluate((tok, user, base) => {
    localStorage.setItem('helen.token', tok);
    localStorage.setItem('helen.user', JSON.stringify(user));
    localStorage.setItem('helen.serverUrl', base);
}, ld.tokens.access_token, ld.user, 'http://127.0.0.1:3088');
await page.reload({ waitUntil: 'networkidle2' });

console.log('2) open first chat');
await page.waitForSelector('#channelList li:first-child', { timeout: 8_000 });
await page.click('#channelList li:first-child');
await new Promise(r => setTimeout(r, 600));

console.log('3) check element under composer-plus button');
const plusInfo = await page.evaluate(() => {
    const b = document.querySelector('.composer-plus');
    if (!b) return { found: false };
    const r = b.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
    return {
        found: true,
        rect: {x: r.x, y: r.y, w: r.width, h: r.height},
        topElement: top ? top.tagName + (top.className ? '.' + String(top.className).split(' ').join('.') : '') : null,
        topMatches: top === b,
    };
});
console.log('  ', JSON.stringify(plusInfo));

console.log('4) click + button → attach menu should appear');
await page.click('.composer-plus');
await new Promise(r => setTimeout(r, 300));
const menuVisible = await page.$('#chatCtxMenu') !== null;
console.log('   menu visible:', menuVisible);

if (!menuVisible) { console.log('  ✗ FAIL — menu did not open'); await browser.close(); process.exit(3); }

console.log('5) patch HTMLInputElement.click + click Photo menu item');
// Override .click() globally for file inputs so the OS picker is
// bypassed. The original site's pickAndUpload() builds an
// <input type=file>, calls .click(), and waits on `change` — we
// inject a synthetic File and fire change instead.
await page.evaluateOnNewDocument(() => {
    const origClick = HTMLInputElement.prototype.click;
    HTMLInputElement.prototype.click = function() {
        if (this.type === 'file' && !this._helenTestFired) {
            this._helenTestFired = true;
            const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='), c => c.charCodeAt(0));
            const file = new File([png], 'sim-test.png', { type: 'image/png' });
            const dt = new DataTransfer();
            dt.items.add(file);
            this.files = dt.files;
            setTimeout(() => this.dispatchEvent(new Event('change', { bubbles: true })), 30);
            return;
        }
        return origClick.call(this);
    };
});
await page.reload({ waitUntil: 'networkidle2' });
await new Promise(r => setTimeout(r, 600));
await page.click('#channelList li:first-child');
await new Promise(r => setTimeout(r, 600));

const before = await page.$$eval('#chatLog li', (els) => els.length);
console.log('   chatLog rows before:', before);
await page.click('.composer-plus');
await new Promise(r => setTimeout(r, 200));
await page.click('#chatCtxMenu button[data-act="photo"]');
console.log('   waiting for upload + send …');
await new Promise(r => setTimeout(r, 3500));

const after = await page.$$eval('#chatLog li', (els) => els.length);
console.log('   chatLog rows  before=', before, ' after=', after,
    after > before ? '✓ FILE MESSAGE APPEARED' : '✗ NO FILE MESSAGE');

// Last message text
const lastText = await page.$$eval('#chatLog li', (els) =>
    els.length ? els[els.length-1].textContent.slice(0, 80) : '');
console.log('   last bubble text:', JSON.stringify(lastText));

await browser.close();
process.exit(after > before ? 0 : 4);
