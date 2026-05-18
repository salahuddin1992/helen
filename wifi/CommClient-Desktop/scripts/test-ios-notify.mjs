// Verify the iOS sim shows a notification + plays a beep + bumps the
// unread badge for messages received outside the active channel.
//
// Strategy: log in as yousef3, stay on the Chats list (don't open any
// chat), then push a message from yousef2 via the API. Assert the
// page called Notification(...) and the channel row got an unread badge.

import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import { io } from 'socket.io-client';

const URL  = 'http://127.0.0.1:3088/mobile/?v=' + Date.now();
const PASS = 'Noor1993!!$';
const RECEIVER = 'yousef3';
const SENDER   = 'yousef2';

const chrome = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].find((p) => { try { fs.statSync(p); return true; } catch { return false; } });
if (!chrome) { console.error('Chrome not found'); process.exit(2); }

async function login(u) {
    const r = await fetch('http://127.0.0.1:3088/api/auth/login', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username:u, password:PASS}),
    });
    return await r.json();
}

const recv = await login(RECEIVER);
const send = await login(SENDER);

const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: 'new',
    args: ['--disable-application-cache'],
});
const page = await browser.newPage();
await page.setViewport({ width: 430, height: 932 });

// Spy on Notification constructor BEFORE the app loads.
await page.evaluateOnNewDocument(() => {
    window.__notifications = [];
    const Original = window.Notification;
    function Spy(title, opts) {
        window.__notifications.push({ title, body: opts && opts.body, tag: opts && opts.tag });
        return Object.assign(Object.create(Original?.prototype || null), { close: () => {} });
    }
    Spy.permission = 'granted';
    Spy.requestPermission = () => Promise.resolve('granted');
    window.Notification = Spy;
});

console.log('1) navigate, seed login (yousef3), stay on Chats list');
await page.goto(URL, { waitUntil: 'networkidle2', timeout: 15_000 });
await page.evaluate((tok, user, base) => {
    localStorage.setItem('helen.token', tok);
    localStorage.setItem('helen.user', JSON.stringify(user));
    localStorage.setItem('helen.serverUrl', base);
}, recv.tokens.access_token, recv.user, 'http://127.0.0.1:3088');
await page.reload({ waitUntil: 'networkidle2' });
await page.waitForSelector('#channelList li:first-child', { timeout: 8_000 });
const channelId = await page.$eval('#channelList li:first-child', (el) => el.dataset.id);
console.log('   first channel id:', channelId.slice(0, 8));

console.log('2) sender (yousef2) connects + sends message via that channel');
const sock = io('http://127.0.0.1:3088', { auth: {token: send.tokens.access_token},
    transports: ['websocket'], reconnection: false });
await new Promise((r, e) => { sock.on('connect', r); sock.on('connect_error', e); });
await new Promise((res) => {
    sock.emit('v2_chat_send_message', {
        channel_id: channelId,
        content: 'Hello from headless test ' + Date.now(),
    }, () => res());
});
sock.close();

await new Promise(r => setTimeout(r, 1500));

console.log('3) check notification + unread badge on receiver');
const result = await page.evaluate(() => {
    const liBadge = document.querySelector('#channelList li:first-child .ch-badge.unread');
    return {
        notifications: window.__notifications || [],
        unreadText: liBadge ? liBadge.textContent : null,
    };
});
console.log('   notifications captured =', result.notifications.length);
result.notifications.forEach((n) => console.log('      title:', JSON.stringify(n.title), 'body:', JSON.stringify(n.body)));
console.log('   unread badge text =', JSON.stringify(result.unreadText));

const pass =
    result.notifications.length >= 1 &&
    result.unreadText && result.unreadText.length > 0;
console.log();
console.log(pass ? '✓ PASS — notification + badge fire on incoming message'
                : '✗ FAIL — missing notification or badge');

await browser.close();
process.exit(pass ? 0 : 1);
