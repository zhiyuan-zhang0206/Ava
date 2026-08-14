'use strict';

const { app, BrowserWindow, globalShortcut, Menu, nativeImage, Tray } = require('electron');
const path = require('path');
const { loadSettings } = require('./config');
const { guardWebContents } = require('./external-links');
const { autoLogin } = require('./auto-login');

let mainWindow = null;
let tray = null;
let settings = null;
let quitting = false;

const APP_ICON = path.join(__dirname, '..', 'assets', 'icon.png');
const TRAY_ICON = path.join(__dirname, '..', 'assets', 'trayTemplate.png');

function showMainWindow() {
  if (!mainWindow) createMainWindow();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    title: 'Ava',
    icon: APP_ICON,
    backgroundColor: '#0e0e12',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadURL(settings.entryUrl);

  // Close → hide to tray (the app stays resident); real quit via tray / Cmd+Q.
  mainWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  // External links use a three-level fallback: local ava-browser new tab ->
// the system default browser.
  guardWebContents(mainWindow.webContents, { settings });

  // The gate is briefly unavailable during a rollout — retry instead of a
  // dead window, then give up loudly.
  let failures = 0;
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url) => {
    if (failures++ < 10) {
      setTimeout(() => {
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadURL(settings.entryUrl);
      }, 3000);
    } else {
      console.error(`[ava-desktop] failed to load ${url}: ${code} ${desc}`);
    }
  });
  mainWindow.webContents.on('did-finish-load', () => {
    failures = 0;
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function createTray() {
  const icon = nativeImage.createFromPath(TRAY_ICON);
  if (process.platform === 'darwin') icon.setTemplateImage(true);
  tray = new Tray(icon.resize({ width: 18, height: 18 }));
  tray.setToolTip('Ava');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open Ava', click: showMainWindow },
      { type: 'separator' },
      {
        label: 'Launch at login',
        type: 'checkbox',
        checked: app.getLoginItemSettings().openAtLogin,
        click: (item) =>
          app.setLoginItemSettings({ openAtLogin: item.checked, openAsHidden: true }),
      },
      { type: 'separator' },
      { label: 'Quit Ava', click: quit },
    ]),
  );
  tray.on('click', showMainWindow);
}

function quit() {
  quitting = true;
  app.quit();
}

// Single instance: a second launch focuses the existing window.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', showMainWindow);

  app.whenReady().then(async () => {
    settings = loadSettings();
    // auto-login when a local cluster secret exists (frontend-only machines
    // silently fall back to the login page); --no-auto-login is the escape
    // hatch for debugging/edge cases.
    if (settings.autoLogin !== false && !process.argv.includes('--no-auto-login')) {
      await autoLogin(settings.gatewayUrl);
    }
    createMainWindow();
    createTray();
  });

  app.on('activate', showMainWindow);

  // The shell is tray-resident: closing the window must not quit the app.
  app.on('window-all-closed', () => {
    // Intentionally empty — keep running in the tray.
  });

  app.on('before-quit', () => {
    quitting = true;
  });

  app.on('will-quit', () => {
    globalShortcut.unregisterAll();
  });
}
