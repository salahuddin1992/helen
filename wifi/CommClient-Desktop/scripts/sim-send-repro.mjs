// Headless reproduction of "I can't send messages from the iOS sim".
//
// Drives the actual served HTML+JS in a real Chromium, logs every
// console message + page error + relevant DOM mutation, then attempts
// the full happy path: sign in → open a DM → type → click Send →
// expect the message to land in the chat-log.
//
// If this runs cleanly, the bug is environmental (extension, cache,
// network). If it fails, the trace tells us exactly where.

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
    args: ['--disable-application-cache', '--disable-features=site-per-process'],
});
const page = await browser.newPage();
await page.setViewport({ width: 430, height: 932 });

// Capture every console + error
page.on('console', (m) => {
    const t = m.type();
    if (t === 'log' || t === 'warn' || t === 'error') {
        console.log(`  [${t}] ${m.text()}`);
    }
});
page.on('pageerror', (e) => console.log('  [pageerror]', e.message));
page.on('requestfailed', (r) =>
    console.log('  [reqfail]', r.method(), r.url(), '-', r.failure()?.errorText));
// Also report any non-2xx fetch the page makes — that's where the 404
// noise was coming from.
page.on('response', (r) => {
    const code = r.status();
    if (code >= 400) console.log('  [http', code + ']', r.url());
});

console.log('1) navigate to', URL);
await page.goto(URL, { waitUntil: 'networkidle2', timeout: 15_000 });

// The sim shows an onboarding screen by default. The fastest path is
// to simulate the same call-flow the real Login screen would: hit
// /api/auth/login, stash token+user in localStorage, reload.
console.log('2) login + seed localStorage');
const loginResp = await fetch('http://127.0.0.1:3088/api/auth/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: USER, password: PASS}),
});
if (!loginResp.ok) { console.error('login failed', loginResp.status); process.exit(2); }
const loginData = await loginResp.json();
await page.evaluate((tok, user, base) => {
    localStorage.setItem('helen.token', tok);
    localStorage.setItem('helen.user', JSON.stringify(user));
    localStorage.setItem('helen.serverUrl', base);
}, loginData.tokens.access_token, loginData.user, 'http://127.0.0.1:3088');
await page.reload({ waitUntil: 'networkidle2' });

// Wait for the chats screen to render at least one row
console.log('3) wait for channel list');
await page.waitForSelector('[data-screen="channels"]:not([hidden]) #channelList li',
    { timeout: 8_000 }).catch(() => {});
const rowCount = await page.$$eval('#channelList li', (els) => els.length);
console.log('   channelList rows =', rowCount);
if (!rowCount) { console.error('no channels — need an existing DM'); await browser.close(); process.exit(3); }

console.log('4) click first channel row');
await page.click('#channelList li:first-child');
await new Promise(r => setTimeout(r, 600));

// Confirm chat screen is up
const chatVisible = await page.$eval('[data-screen="chat"]', (el) => !el.hidden);
console.log('   chat screen visible =', chatVisible);

// Confirm composer is reachable
const composerInfo = await page.evaluate(() => {
    const f  = document.getElementById('composerForm');
    const i  = document.getElementById('composerInput');
    const b  = document.querySelector('.composer-send');
    const fr = f && f.getBoundingClientRect();
    const ir = i && i.getBoundingClientRect();
    const br = b && b.getBoundingClientRect();
    return {
        formExists: !!f,
        inputExists: !!i,
        sendExists: !!b,
        formRect: fr && {x: fr.x, y: fr.y, w: fr.width, h: fr.height},
        inputRect: ir && {x: ir.x, y: ir.y, w: ir.width, h: ir.height},
        sendRect:  br && {x: br.x, y: br.y, w: br.width, h: br.height},
        currentChannel: !!window.currentChannel,
    };
});
console.log('   composer:', JSON.stringify(composerInfo, null, 2));

if (!composerInfo.inputExists) { console.error('NO INPUT'); await browser.close(); process.exit(4); }

console.log('5) focus + type "test123"');
await page.focus('#composerInput');
await page.type('#composerInput', 'test123');

// Probe state right *before* clicking send so we know whether the
// closure variable `currentChannel` was set when the row was clicked.
const stateBeforeSend = await page.evaluate(() => ({
    activeChannel:        localStorage.getItem('helen.activeChannel'),
    chatTitle:            document.getElementById('chatTitle')?.textContent,
    chatLogChildCount:    document.getElementById('chatLog')?.children.length,
    inputDisabled:        document.getElementById('composerInput')?.disabled,
    inputValueLive:       document.getElementById('composerInput')?.value,
}));
console.log('   state before send:', JSON.stringify(stateBeforeSend));

console.log('   element at send-button center:');
const elementHere = await page.evaluate(() => {
    const b = document.querySelector('.composer-send');
    const r = b.getBoundingClientRect();
    const cx = r.x + r.width/2, cy = r.y + r.height/2;
    const top = document.elementFromPoint(cx, cy);
    const path = [];
    let n = top;
    while (n && path.length < 6) {
        path.push(n.tagName + (n.id ? '#'+n.id : '') + (n.className ? '.'+String(n.className).replace(/\s+/g,'.') : ''));
        n = n.parentElement;
    }
    return { topElement: path[0], chain: path.join(' < ') };
});
console.log('     ', JSON.stringify(elementHere));

console.log('6) click Send button');
const before = await page.$$eval('#chatLog li', (els) => els.length);
await page.click('.composer-send');
await new Promise(r => setTimeout(r, 1500));
const after = await page.$$eval('#chatLog li', (els) => els.length);
console.log('   chatLog rows  before=', before, ' after=', after,
    after > before ? '✓ MESSAGE APPEARED' : '✗ NO NEW MESSAGE');

console.log('7) capture remaining input value (should be cleared if send fired):',
    JSON.stringify(await page.$eval('#composerInput', (el) => el.value)));

console.log('8) try Enter-key path');
await page.focus('#composerInput');
await page.keyboard.press('Enter');
await new Promise(r => setTimeout(r, 1500));
const after2 = await page.$$eval('#chatLog li', (els) => els.length);
console.log('   chatLog rows after Enter =', after2,
    after2 > after ? '✓ MESSAGE APPEARED' : '✗ STILL NO NEW MESSAGE');

// Listener probe: submit the form directly via evaluate.
console.log('9) directly fire form.requestSubmit() from evaluate');
await page.evaluate(() => {
    const f = document.getElementById('composerForm');
    if (f && typeof f.requestSubmit === 'function') {
        try { f.requestSubmit(); console.log('[probe] requestSubmit returned'); }
        catch (e) { console.log('[probe] requestSubmit threw:', e.message); }
    } else {
        const ev = new Event('submit', { cancelable: true, bubbles: true });
        const result = f.dispatchEvent(ev);
        console.log('[probe] dispatchEvent submit returned', result, 'defaultPrevented=', ev.defaultPrevented);
    }
});
await new Promise(r => setTimeout(r, 1500));
const after3 = await page.$$eval('#chatLog li', (els) => els.length);
console.log('   chatLog rows after probe =', after3,
    after3 > after2 ? '✓ MESSAGE APPEARED' : '✗ STILL NO NEW MESSAGE');

console.log('10) attempt manual emit via socket from evaluate');
const emitResult = await page.evaluate(async (channelId) => {
    return await new Promise((res) => {
        const sock = window.__sock || null;          // not exposed yet
        // Attempt to find the socket by inspecting all globals for a
        // socket.io client.
        let found = null;
        for (const k of Object.getOwnPropertyNames(window)) {
            const v = window[k];
            if (v && v.io && typeof v.emit === 'function' && typeof v.on === 'function') {
                found = v; break;
            }
        }
        if (!found) { res({ found: false }); return; }
        found.emit('v2_chat_send_message',
            { channel_id: channelId, content: 'probe-from-evaluate', client_id: 'probe-1' },
            (ack) => res({ found: true, ack }));
        setTimeout(() => res({ found: true, ack: 'no-ack-timeout' }), 2000);
    });
}, stateBeforeSend.activeChannel);
console.log('   socket emit result:', JSON.stringify(emitResult));

await browser.close();
process.exit(0);
