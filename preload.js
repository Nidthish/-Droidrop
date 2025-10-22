// preload.js

const { contextBridge, ipcRenderer } = require('electron');

// Expose a safe, limited API to the renderer process (your UI)
contextBridge.exposeInMainWorld('electronAPI', {
  openFolderDialog: () => ipcRenderer.invoke('open-folder-dialog')
});