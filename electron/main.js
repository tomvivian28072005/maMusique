const { app, BrowserWindow, Tray, Menu, nativeImage, dialog } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

// ── Config ──────────────────────────────────────────────────
const PORT = 9000;
const SERVER_URL = `http://localhost:${PORT}`;
const IS_DEV = !app.isPackaged;

let mainWindow = null;
let tray = null;
let pythonProcess = null;
let isQuitting = false;

// ── Single instance ─────────────────────────────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── Python server ───────────────────────────────────────────
function getServerPath() {
  if (IS_DEV) return null; // en dev, le serveur tourne déjà via maMusique.bat
  // En prod, le serveur Python est dans resources/python-server/
  const serverDir = path.join(process.resourcesPath, 'python-server');
  return path.join(serverDir, 'Clom.exe');
}

function startPythonServer() {
  const exePath = getServerPath();
  if (!exePath) {
    console.log('[Electron] Mode dev — serveur Python externe attendu');
    return;
  }

  const serverDir = path.dirname(exePath);
  console.log(`[Electron] Lancement serveur Python: ${exePath}`);

  pythonProcess = spawn(exePath, ['--no-browser'], {
    cwd: serverDir,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  pythonProcess.stdout.on('data', (d) => console.log(`[Python] ${d}`));
  pythonProcess.stderr.on('data', (d) => console.error(`[Python] ${d}`));
  pythonProcess.on('exit', (code) => {
    console.log(`[Python] Processus terminé (code ${code})`);
    pythonProcess = null;
    // Si le serveur crash et qu'on n'est pas en train de quitter, relancer
    if (!isQuitting && code !== 0) {
      console.log('[Electron] Relance du serveur dans 2s...');
      setTimeout(startPythonServer, 2000);
    }
  });
}

function stopPythonServer() {
  if (!pythonProcess) return;
  console.log('[Electron] Arrêt du serveur Python...');
  pythonProcess.kill();
  pythonProcess = null;
}

// ── Wait for server ready ───────────────────────────────────
function waitForServer(maxAttempts = 60) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      attempts++;
      const req = http.get(`${SERVER_URL}/api/version`, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          retry();
        }
      });
      req.on('error', retry);
      req.setTimeout(1000, () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (attempts >= maxAttempts) {
        reject(new Error('Serveur Python non disponible après 30s'));
      } else {
        setTimeout(check, 500);
      }
    };
    check();
  });
}

// ── Window ──────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 400,
    minHeight: 600,
    title: 'Clom',
    icon: path.join(__dirname, '..', 'logo.ico'),
    backgroundColor: '#050508',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Pas de menu bar
  mainWindow.setMenu(null);

  // Afficher quand prêt (évite le flash blanc)
  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Minimize to tray au lieu de fermer
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ── System tray ─────────────────────────────────────────────
function createTray() {
  // Icône par défaut (on utilisera le logo plus tard)
  const iconPath = path.join(__dirname, '..', 'logo.ico');
  let trayIcon;
  try {
    trayIcon = nativeImage.createFromPath(iconPath);
  } catch {
    trayIcon = nativeImage.createEmpty();
  }

  tray = new Tray(trayIcon.isEmpty() ? nativeImage.createEmpty() : trayIcon);
  tray.setToolTip('Clom');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Ouvrir Clom',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    { type: 'separator' },
    {
      label: 'Quitter',
      click: () => {
        isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── Auto-updater ────────────────────────────────────────────
function setupAutoUpdater() {
  if (IS_DEV) return;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info) => {
    console.log(`[Updater] Nouvelle version disponible: ${info.version}`);
    if (mainWindow) {
      mainWindow.webContents.send('update-available', info.version);
    }
  });

  autoUpdater.on('download-progress', (progress) => {
    if (mainWindow) {
      mainWindow.webContents.send('update-progress', progress.percent);
    }
  });

  autoUpdater.on('update-downloaded', () => {
    console.log('[Updater] Mise à jour téléchargée, installation au prochain redémarrage');
    if (mainWindow) {
      mainWindow.webContents.send('update-downloaded');
    }
  });

  autoUpdater.on('error', (err) => {
    console.error('[Updater] Erreur:', err);
  });

  // Vérifier les mises à jour au démarrage (après 5s)
  setTimeout(() => autoUpdater.checkForUpdates(), 5000);
}

// ── IPC handlers ────────────────────────────────────────────
const { ipcMain } = require('electron');

ipcMain.handle('download-update', async () => {
  try {
    await autoUpdater.downloadUpdate();
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('install-update', () => {
  isQuitting = true;
  autoUpdater.quitAndInstall(false, true);
});

ipcMain.handle('app-quit', () => {
  isQuitting = true;
  app.quit();
});

// ── App lifecycle ───────────────────────────────────────────
app.whenReady().then(async () => {
  createWindow();
  createTray();

  // Lancer le serveur Python (sauf en dev)
  startPythonServer();

  // Attendre que le serveur soit prêt
  try {
    await waitForServer();
    console.log('[Electron] Serveur prêt, chargement de la page');
    mainWindow.loadURL(SERVER_URL);
  } catch (err) {
    console.error('[Electron]', err.message);
    dialog.showErrorBox(
      'Erreur de démarrage',
      'Le serveur Clom n\'a pas pu démarrer.\nVérifie que le port 9000 n\'est pas déjà utilisé.'
    );
    isQuitting = true;
    app.quit();
  }

  setupAutoUpdater();
});

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('will-quit', () => {
  stopPythonServer();
});

app.on('window-all-closed', () => {
  // Ne pas quitter — le tray reste actif
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  }
});
