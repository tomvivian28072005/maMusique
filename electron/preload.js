const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Informe le frontend qu'on est dans Electron
  isElectron: true,

  // Auto-update
  onUpdateAvailable: (cb) => ipcRenderer.on('update-available', (_e, version) => cb(version)),
  onUpdateProgress: (cb) => ipcRenderer.on('update-progress', (_e, percent) => cb(percent)),
  onUpdateDownloaded: (cb) => ipcRenderer.on('update-downloaded', () => cb()),
  downloadUpdate: () => ipcRenderer.invoke('download-update'),
  installUpdate: () => ipcRenderer.invoke('install-update'),

  // App control
  quit: () => ipcRenderer.invoke('app-quit'),
});
