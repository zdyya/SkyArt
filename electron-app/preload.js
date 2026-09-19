const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  platform:    process.platform,
  version:     process.versions.electron,
  backendPort: parseInt(process.env.BACKEND_PORT || '8764'),
  showItemInFolder: (p) => ipcRenderer.invoke('show-item-in-folder', p),
  copyToClipboard:  (t) => ipcRenderer.invoke('copy-to-clipboard', t),
  getUploadDir:     ()  => ipcRenderer.invoke('get-upload-dir'),
  selectFolder:     ()  => ipcRenderer.invoke('select-folder'),
  openPath:         (p) => ipcRenderer.invoke('open-path', p),
  selectBackgroundImage: () => ipcRenderer.invoke('select-background-image'),
  clearBackgroundImage:  () => ipcRenderer.invoke('clear-background-image'),
  // 配置管理
  getConfig:       ()    => ipcRenderer.invoke('get-config'),
  saveConfig:      (cfg) => ipcRenderer.invoke('save-config', cfg),
  restartBackend:  ()    => ipcRenderer.invoke('restart-backend'),
  getDiagnostics:  ()    => ipcRenderer.invoke('get-diagnostics'),
});

// 监听主进程发来的"打开设置页"消息，转成 DOM 事件
ipcRenderer.on('open-settings', () => {
  document.dispatchEvent(new Event('open-settings-event'));
});

// 监听后端意外退出
ipcRenderer.on('backend-exit', (_, info) => {
  document.dispatchEvent(new CustomEvent('backend-exit-event', { detail: info }));
});
