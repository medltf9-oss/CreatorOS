// Minimal, explicit bridge. Nothing else from Node reaches the renderer.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('creatoros', {
  bootstrap: () => ipcRenderer.invoke('creatoros:bootstrap'),
  backendLog: () => ipcRenderer.invoke('creatoros:backendLog'),
  openPath: (target) => ipcRenderer.invoke('creatoros:openPath', target),
  pickMedia: () => ipcRenderer.invoke('creatoros:pickMedia'),
  importMedia: (paths) => ipcRenderer.invoke('creatoros:importMedia', paths),
  // Phase 6 - credential vault. The renderer can hand a secret to the vault and read back
  // metadata only. There is deliberately no decrypt handle: nothing in the renderer can read
  // a stored secret back out.
  vaultHealth: () => ipcRenderer.invoke('creatoros:vaultHealth'),
  vaultStore: (request) => ipcRenderer.invoke('creatoros:vaultStore', request),
  vaultDelete: (id) => ipcRenderer.invoke('creatoros:vaultDelete', id),
  openAuthUrl: (url) => ipcRenderer.invoke('creatoros:openAuthUrl', url),
});
