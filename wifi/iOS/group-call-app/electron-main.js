/**
 * group-call-app/electron-main.js — Electron wrapper for the standalone
 * group call demo. Single binary, no external Node/npm needed: the
 * Express + Socket.IO server is required INTO the Electron main
 * process (Electron embeds Node), so there's no child subprocess and
 * no port discovery dance.
 *
 * The window opens on a deterministic port (3099 by default, env-
 * overridable). If the port is taken, we fall back through 3099→3199.
 */

const { app, BrowserWindow, shell, Menu } = require('electron');
const path = require('path');
const net  = require('net');

let mainWindow = null;
let serverHandle = null;
let serverPort  = null;

// ── Port resolution ──────────────────────────────────────────────────
async function pickFreePort(start = 3099, end = 3199) {
    for (let p = start; p <= end; p++) {
        const free = await new Promise((resolve) => {
            const s = net.createServer().once('error', () => resolve(false))
                                        .once('listening', () => s.close(() => resolve(true)));
            s.listen(p, '127.0.0.1');
        });
        if (free) return p;
    }
    throw new Error(`no free port in ${start}-${end}`);
}

// ── Server boot ──────────────────────────────────────────────────────
async function startServer() {
    serverPort = Number(process.env.PORT) || await pickFreePort();
    process.env.PORT = String(serverPort);

    // Lazy-require so the require() trace shows up after Electron is
    // ready — easier to diagnose dependency errors.
    const http      = require('http');
    const express   = require('express');
    const { Server } = require('socket.io');

    // Inline the relevant bits of server.js so packaging is one file.
    // (We could also `require('./server.js')` but it auto-listens on
    // PORT, which we want to do explicitly here.)
    const serverModule = require('./server-core.js');
    const { io, server, app: expressApp } = serverModule.create({ http, express, Server });
    void expressApp; void io;

    return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(serverPort, '127.0.0.1', () => {
            console.log(`[group-call] server listening on http://127.0.0.1:${serverPort}`);
            serverHandle = server;
            resolve();
        });
    });
}

// ── Window factory ───────────────────────────────────────────────────
function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1100,
        height: 760,
        minWidth: 480,
        minHeight: 360,
        backgroundColor: '#000',
        autoHideMenuBar: true,
        title: 'Group Call',
        webPreferences: {
            contextIsolation: true,
            nodeIntegration:  false,
            sandbox:          true,
        },
    });

    Menu.setApplicationMenu(null);

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (url.startsWith('http://127.0.0.1') || url.startsWith('http://localhost')) {
            return { action: 'allow' };
        }
        shell.openExternal(url);
        return { action: 'deny' };
    });

    mainWindow.loadURL(`http://127.0.0.1:${serverPort}/?room=test`);
    mainWindow.on('closed', () => { mainWindow = null; });
}

// ── Lifecycle ────────────────────────────────────────────────────────
app.whenReady().then(async () => {
    try {
        await startServer();
    } catch (err) {
        console.error('[group-call] server start failed:', err);
        app.exit(1);
        return;
    }
    createWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

app.on('window-all-closed', () => {
    try { serverHandle && serverHandle.close(); } catch {}
    if (process.platform !== 'darwin') app.quit();
});
