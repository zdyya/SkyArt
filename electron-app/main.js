const { app, BrowserWindow, Menu, ipcMain, shell, clipboard, dialog } = require('electron');
const path = require('path');
const { spawn, execSync } = require('child_process');
const http = require('http');
const fs = require('fs');

// ── Config ────────────────────────────────────────────────────────────────────
// Dev (npm start): 8764  |  Packaged .app: 8765  — 两者不同，互不干扰
const BACKEND_PORT = app.isPackaged ? 8765 : 8764;
process.env.BACKEND_PORT = String(BACKEND_PORT); // preload.js 读取后注入到 renderer
const APP_VERSION = app.getVersion(); // 启动握手校验：确保连的后端版本 == 本 App 版本
const BACKEND_DIR  = path.join(__dirname, '..', 'backend');
const VENV_UVICORN = path.join(BACKEND_DIR, '.venv', 'bin', 'uvicorn');

const BUNDLED_BACKEND = path.join(
  app.isPackaged ? path.join(process.resourcesPath, 'backend-bin') : path.join(__dirname, 'backend-bin'),
  'skyart-backend'
);

// 数据目录固定用 SkyArtDemo，与 app 包名解耦，避免改名后已有数据丢失
const SKYART_USER_DATA = path.join(app.getPath('appData'), 'SkyArtDemo');

// Chromium 运行文件(缓存/cookie/localStorage/登录态)默认按 app.name 落在单独的
// Application Support/SkyArt 顶层目录，与业务数据的 SkyArtDemo 分家。这里把它收进
// SkyArtDemo/chromium 子目录：顶层只剩一个数据家，运行文件与业务数据/配置仍按子目录分开，
// 后续导入导出只操作 skyart-data，碰不到引擎数据。必须在 app ready 前调用才生效。
app.setPath('userData', path.join(SKYART_USER_DATA, 'chromium'));

/** 用户配置文件路径 */
function configFilePath() {
  return path.join(SKYART_USER_DATA, 'skyart-config.json');
}

/** 读配置（不存在时返回空对象） */
function readConfig() {
  try {
    const raw = fs.readFileSync(configFilePath(), 'utf-8');
    return JSON.parse(raw);
  } catch { return {}; }
}

/** 写配置 */
function writeConfig(cfg) {
  fs.writeFileSync(configFilePath(), JSON.stringify(cfg, null, 2), 'utf-8');
}

let mainWindow     = null;
let splashWindow   = null;
let backendProcess = null;
let backendCrashed = false; // 后端进程启动后立刻退出（非零）时设为 true

// ── Backend process ───────────────────────────────────────────────────────────

function buildBackendEnv() {
  const cfg = readConfig();
  const dataDir = path.join(SKYART_USER_DATA, 'skyart-data');
  fs.mkdirSync(dataDir, { recursive: true });

  // 打包进 app 的 ffmpeg/ffprobe（原生 arm64），放到 PATH 最前 —— 业务机器无需自行安装。
  const bundledFfmpegDir = app.isPackaged
    ? path.join(process.resourcesPath, 'ffmpeg')
    : path.join(__dirname, 'bin', `ffmpeg-${process.arch}`);
  // 启动时确保可执行权限（electron-builder 偶尔会丢 +x）；目录不存在则忽略，回落系统 ffmpeg
  try {
    fs.chmodSync(path.join(bundledFfmpegDir, 'ffmpeg'), 0o755);
    fs.chmodSync(path.join(bundledFfmpegDir, 'ffprobe'), 0o755);
  } catch (e) { /* ignore — fall back to system ffmpeg on PATH */ }

  // GUI 启动的 Electron 不继承 shell 的完整 PATH，Homebrew 目录不在其中。
  // 顺序：包内 ffmpeg 优先 → Homebrew/usr-local 兜底 → 继承的 PATH。
  const extraPaths = '/opt/homebrew/bin:/usr/local/bin';
  const currentPath = process.env.PATH || '';
  const sysPaths = currentPath.includes('/opt/homebrew/bin')
    ? currentPath
    : `${extraPaths}:${currentPath}`;
  const fullPath = `${bundledFfmpegDir}:${sysPaths}`;

  return {
    ...process.env,
    PATH:                 fullPath,
    PORT:                 String(BACKEND_PORT),
    UPLOAD_DIR:           dataDir,
    DATABASE_URL:         `sqlite+aiosqlite:///${path.join(dataDir, 'skyart.db')}`,
    SECRET_KEY:           cfg.secretKey           || 'skyart-default-secret-2024',
    HAPPYHORSE_API_KEY:   cfg.happyhorseApiKey    || '',
    HAPPYHORSE_BASE_URL:  cfg.happyhorseBaseUrl   || 'https://dashscope-us.aliyuncs.com/api/v1',
    GEMINI_PROXY_URL:     cfg.geminiProxyUrl      || 'https://dscriptr.stg.shoplazza.com/skyart/gemini/analyze',
    GEMINI_PROXY_KEY:     cfg.geminiProxyKey      || '',
    QWEN_API_KEY:         cfg.qwenApiKey          || '',
    QWEN_BASE_URL:        cfg.qwenBaseUrl         || 'https://dashscope.aliyuncs.com/api/v1',
    OSS_ACCESS_KEY_ID:      cfg.ossAccessKeyId      || '',
    OSS_ACCESS_KEY_SECRET:  cfg.ossAccessKeySecret  || '',
    OSS_BUCKET_NAME:        cfg.ossBucketName       || '',
    OSS_ENDPOINT:           cfg.ossEndpoint         || 'https://oss-cn-hangzhou.aliyuncs.com',
    OSS_CUSTOM_DOMAIN:      cfg.ossCustomDomain     || '',
    // 美区 OSS — Gemini 代理视频上传用，从配置文件读取
    OSS_KEY_SkyArt_US:      cfg.ossKeyUsWest      || '',
    OSS_SECRET_SkyArt_US:   cfg.ossSecretUsWest   || '',
    OSS_BUCKET_SkyArt_US:   cfg.ossBucketUsWest   || 'saas-dscriptr-dscriptr-develop',
    OSS_ENDPOINT_SkyArt_US: cfg.ossEndpointUsWest || 'https://oss-us-west-1.aliyuncs.com',
  };
}

function startBackend() {
  backendCrashed = false; // 重置崩溃标志
  const env = buildBackendEnv();
  const dataDir = path.join(SKYART_USER_DATA, 'skyart-data');

  if (app.isPackaged) {
    console.log('[backend] Starting bundled backend:', BUNDLED_BACKEND);

    // 确保可执行权限（electron-builder 有时会丢失）
    try { fs.chmodSync(BUNDLED_BACKEND, 0o755); } catch(e) { console.warn('[backend] chmod failed:', e.message); }

    backendProcess = spawn(BUNDLED_BACKEND, [], {
      cwd: dataDir,
      stdio: ['pipe', 'pipe', 'pipe'],  // 不用 ignore，保证 stdin/stdout/stderr 都连接
      env,
      detached: false,  // 不分离进程组，让 Electron 直接管理子进程
    });
  } else {
    if (!fs.existsSync(VENV_UVICORN)) {
      console.error('[backend] uvicorn not found:', VENV_UVICORN);
      return;
    }
    console.log('[backend] Starting uvicorn (dev mode)');
    backendProcess = spawn(VENV_UVICORN, ['main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)], {
      cwd: BACKEND_DIR, stdio: ['pipe', 'pipe', 'pipe'], env, detached: false,  // stdin 管道:供孤儿守卫检测父进程死亡
    });
  }

  let backendStderr = '';
  let backendStdout = '';
  backendProcess.stdout.on('data', d => { process.stdout.write('[backend] ' + d); backendStdout += d.toString(); });
  backendProcess.stderr.on('data', d => {
    process.stderr.write('[backend] ' + d);
    backendStderr += d.toString();
  });
  backendProcess.on('exit', (code, signal) => {
    console.log('[backend] exited', code, signal);
    if (code !== 0 && code !== null) {
      backendCrashed = true;
      const errInfo = {
        code, signal,
        stderr: backendStderr.slice(-1000),
        stdout: backendStdout.slice(-500),
        path: BUNDLED_BACKEND,
      };
      // 写日志文件方便排查
      try {
        const logPath = path.join(SKYART_USER_DATA, 'backend-crash.log');
        fs.writeFileSync(logPath, JSON.stringify({ time: new Date().toISOString(), ...errInfo }, null, 2));
        errInfo.logPath = logPath;
      } catch {}
      if (mainWindow) mainWindow.webContents.send('backend-exit', errInfo);
    }
    backendProcess = null;
  });
}

function stopBackend() {
  if (!backendProcess) return;
  console.log('[backend] Stopping…');
  try { backendProcess.kill('SIGTERM'); } catch {}
  backendProcess = null;
}

// 清场：杀掉占用后端端口的任何进程（上次强退残留的孤儿、没退干净的旧实例）。
// 每次启动前先清，保证接下来 startBackend() 起的、前端连的，一定是本次安装目录里的后端。
function killPortOccupant() {
  let pids = '';
  try {
    pids = execSync(`lsof -ti tcp:${BACKEND_PORT} -sTCP:LISTEN 2>/dev/null || true`).toString().trim();
  } catch { return; }
  if (!pids) return;
  for (const pid of pids.split('\n').filter(Boolean)) {
    try { process.kill(parseInt(pid, 10), 'SIGKILL'); console.log('[backend] killed stale occupant pid', pid); }
    catch (e) { console.warn('[backend] kill', pid, 'failed:', e.message); }
  }
}

// packaged：一律清场重起，杜绝强退残留的孤儿（KNOWLEDGE #7）。
// dev：端口上已有健康后端就复用，不 SIGKILL —— 否则会杀掉开发者手起的 --reload 热重载实例。
async function ensureBackend() {
  if (!app.isPackaged) {
    try { await waitForBackend(1500); console.log('[backend] dev: 复用端口上已有的后端'); return; }
    catch { /* 没有或不健康，照常清场重起 */ }
  }
  killPortOccupant();
  startBackend();
}

function waitForBackend(ms = 30000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + ms;
    function attempt() {
      const req = http.get(`http://127.0.0.1:${BACKEND_PORT}/`, res => {
        if (res.statusCode !== 200) { res.resume(); return retry(); }
        let body = '';
        res.on('data', d => body += d);
        res.on('end', () => {
          let v = null;
          try { v = JSON.parse(body).version; } catch {}
          // 连到的后端版本与 App 不一致 —— 多半是旧孤儿占着端口，交给上层清场重起。
          // 仅 packaged 强制：dev 下后端版本(源码常量)与 app.getVersion()(package.json)分头维护，
          // 漂移不代表连错实例，若在此 reject 会把开发态自锁。
          if (app.isPackaged && v && v !== APP_VERSION) return reject(new Error(`VERSION_MISMATCH:${v}`));
          resolve();
        });
      });
      req.on('error', retry);
      req.end();
    }
    function retry() {
      // 后端进程已崩溃，无需继续等待
      if (backendCrashed) return reject(new Error('Backend crashed'));
      Date.now() > deadline ? reject(new Error('Backend timeout')) : setTimeout(attempt, 400);
    }
    attempt();
  });
}

// ── Windows ───────────────────────────────────────────────────────────────────

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 420, height: 280, frame: false, resizable: false, center: true,
    backgroundColor: '#0f1117', webPreferences: { nodeIntegration: false },
  });
  splashWindow.loadURL(`data:text/html;charset=utf-8,<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{margin:0;background:%230f1117;color:%23e2e8f0;font-family:system-ui,sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh}
.logo{font-size:28px;font-weight:700;color:%236366f1}.sub{font-size:13px;color:%2394a3b8;margin-top:6px}
.spinner{margin-top:28px;width:36px;height:36px;border:3px solid %231e293b;
         border-top-color:%236366f1;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.status{margin-top:14px;font-size:12px;color:%2364748b;text-align:center;max-width:320px;line-height:1.5}
.hint{margin-top:8px;font-size:11px;color:%23334155;text-align:center;max-width:320px;line-height:1.4;min-height:32px}
</style></head><body>
<div class="logo">&#x1F3AC; 天工短剧</div>
<div class="sub">SkyArt &mdash; AI 短剧创作平台</div>
<div class="spinner"></div>
<div class="status" id="s">正在初始化…</div>
<div class="hint" id="h">首次启动需要初始化，请耐心等待（最多 2 分钟）</div>
<script>
var _t=null,_s=0;
function setStatus(msg){document.getElementById('s').textContent=msg;}
function setHint(msg){document.getElementById('h').textContent=msg;}
function startTimer(){
  _s=0;_t=setInterval(function(){
    _s++;
    setStatus('正在启动后端服务… '+_s+'s');
    if(_s===60)setHint('macOS 正在对文件进行安全验证，请耐心等待…');
    if(_s===90)setHint('仍在等待中，首次安装可能需要较长时间，这是正常现象。');
  },1000);
}
</script>
</body></html>`);
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1200, minHeight: 700,
    titleBarStyle: 'hiddenInset', title: 'SkyArt — 天工短剧',
    // 各页 topbar 高 52~56px，默认红绿灯偏上、与标题/路径不在一条水平线上。手动下移
    // 垂直居中(y≈(topbar-灯高)/2)，x 与各页 topbar 的 padding-left:78 留出安全间距。
    trafficLightPosition: { x: 18, y: 20 },  // 12px 按钮中心 26 = 52px 顶栏中线
    backgroundColor: '#eef0f8',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false, contextIsolation: true, webSecurity: false,
    },
    show: false,
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'home.html'));
  mainWindow.once('ready-to-show', () => {
    if (splashWindow) { splashWindow.close(); splashWindow = null; }
    mainWindow.show(); mainWindow.focus();
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

// ── App lifecycle ─────────────────────────────────────────────────────────────

/** 向 splash 注入 JS（splash 加载 data: URL 时使用） */
function splashEval(js) {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.executeJavaScript(js).catch(() => {});
  }
}

app.whenReady().then(async () => {
  buildMenu();
  createSplash();

  // 给 splash 约 300ms 完成首次渲染
  await new Promise(resolve => setTimeout(resolve, 300));

  // ── 阶段 1：清除 macOS quarantine ──────────────────────────────────────────
  if (app.isPackaged) {
    splashEval(`setStatus('正在清除 macOS 安全限制…'); setHint('首次安装后需要清除安全标记，请稍候…');`);

    // 清除整个 .app bundle 的 quarantine（之前只清了 backend-bin，可能遗漏 _internal/Python.framework 等）
    const appBundlePath = path.join(process.resourcesPath, '..', '..');
    await new Promise(resolve => {
      const { exec } = require('child_process');
      exec(`xattr -dr com.apple.quarantine "${appBundlePath}"`, (err) => {
        if (err) console.warn('[backend] xattr warning:', err.message);
        else console.log('[backend] Cleared quarantine from entire app bundle:', appBundlePath);
        resolve();
      });
    });
  }

  // ── 阶段 2：启动后端 ──────────────────────────────────────────────────────
  splashEval(`startTimer(); setHint('首次启动需要初始化，请耐心等待（最多 2 分钟）');`);

  // packaged 清场重起、dev 复用健康实例（见 ensureBackend）
  await ensureBackend();

  let backendOK = false;
  try {
    await waitForBackend(120000);
    console.log('[backend] Ready ✓');
    backendOK = true;
  } catch (e) {
    console.error('[backend] startup failed:', e.message);
    // 版本不一致（连到了残留旧实例）：强制清场再起一次
    if (String(e.message).startsWith('VERSION_MISMATCH')) {
      killPortOccupant();
      startBackend();
      try { await waitForBackend(60000); backendOK = true; console.log('[backend] Ready ✓ (after reclaim)'); }
      catch (e2) { console.error('[backend] retry failed:', e2.message); }
    }
  }

  createMainWindow();

  if (!backendOK) {
    // 读取崩溃日志以显示真实错误原因
    let crashDetail = '';
    try {
      const raw = fs.readFileSync(path.join(SKYART_USER_DATA, 'backend-crash.log'), 'utf-8');
      const parsed = JSON.parse(raw);
      const stderr = (parsed.stderr || '').trim().slice(-400);
      const code = parsed.code;
      crashDetail = `\n\n── 崩溃信息（供技术支持使用）──\n退出码: ${code}\n${stderr || '（无 stderr 输出）'}`;
    } catch { /* 没有崩溃日志，可能是超时而非崩溃 */ }

    setTimeout(() => {
      dialog.showErrorBox(
        '⚠️  SkyArt 后端无法启动',
        [
          '后端服务在 2 分钟内未就绪，可能被 macOS 安全策略阻止。',
          '',
          '请在「终端」中运行以下命令后重启应用：',
          '',
          '  xattr -dr com.apple.quarantine /Applications/SkyArt.app',
          '',
          '如仍无法启动，请前往：',
          '  系统设置 → 隐私与安全性 → 找到 SkyArt → 点击「仍然允许」',
          crashDetail,
        ].join('\n')
      );
    }, 1500);
  }
  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length) return;
    // 窗口全关时后端已被 stopBackend 停掉；重开前清场再起（dev 复用健康实例）
    await ensureBackend();
    try { await waitForBackend(30000); } catch (e) { console.warn('[backend] activate wait:', e.message); }
    createMainWindow();
  });
});

app.on('window-all-closed', () => { stopBackend(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => stopBackend());

// ── IPC ───────────────────────────────────────────────────────────────────────

ipcMain.handle('show-item-in-folder', (_, p) => shell.showItemInFolder(p));
ipcMain.handle('copy-to-clipboard',   (_, t) => clipboard.writeText(t));
ipcMain.handle('get-upload-dir',      () => path.join(SKYART_USER_DATA, 'skyart-data'));

// 选本地文件夹（项目导出目录）：返回绝对路径，取消返回 null
ipcMain.handle('select-folder', async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    title: '选择导出文件夹',
    properties: ['openDirectory', 'createDirectory'],
  });
  return (r.canceled || !r.filePaths.length) ? null : r.filePaths[0];
});

// 打开某个文件夹（导出目录“打开文件夹”按钮）
ipcMain.handle('open-path', (_, p) => shell.openPath(p));

ipcMain.handle('get-config', () => readConfig());

ipcMain.handle('save-config', (_, cfg) => {
  writeConfig(cfg);
  return { ok: true };
});

// 重启后端（保存配置后调用，让新 API Key 生效）
// 诊断信息：供用户排查后端启动失败
ipcMain.handle('get-diagnostics', async () => {
  const { execSync } = require('child_process');
  const safeExec = (cmd) => { try { return execSync(cmd, { encoding: 'utf-8', timeout: 3000 }).trim(); } catch(e) { return e.message; } };
  return {
    platform:      process.platform,
    arch:          process.arch,
    nodeVersion:   process.version,
    electronVersion: process.versions.electron,
    osVersion:     safeExec('sw_vers -productVersion'),
    backendPath:   BUNDLED_BACKEND,
    backendExists: fs.existsSync(BUNDLED_BACKEND),
    backendPerms:  safeExec(`ls -la "${BUNDLED_BACKEND}" 2>&1`),
    quarantine:    safeExec(`xattr -l "${BUNDLED_BACKEND}" 2>&1`),
    backendPort:   BACKEND_PORT,
    portInUse:     safeExec(`lsof -i :${BACKEND_PORT} 2>&1 | head -5`),
    logPath:       path.join(SKYART_USER_DATA, 'backend-crash.log'),
    logContent:    (() => { try { return fs.readFileSync(path.join(SKYART_USER_DATA, 'backend-crash.log'), 'utf-8'); } catch { return '无崩溃日志'; } })(),
    userData:      SKYART_USER_DATA,
  };
});

ipcMain.handle('restart-backend', async () => {
  stopBackend();
  await new Promise(r => setTimeout(r, 1000));
  killPortOccupant();
  startBackend();
  try { await waitForBackend(20000); return { ok: true }; }
  catch (e) { return { ok: false, error: e.message }; }
});

// ── Menu ──────────────────────────────────────────────────────────────────────

function buildMenu() {
  const template = [
    {
      label: app.name,
      submenu: [
        { role: 'about', label: '关于 SkyArt' },
        { type: 'separator' },
        {
          label: '⚙️ API 配置…',
          click: () => { if (mainWindow) mainWindow.webContents.send('open-settings'); },
        },
        { type: 'separator' },
        { role: 'services', label: '服务' },
        { type: 'separator' },
        { role: 'hide', label: '隐藏' },
        { role: 'hideOthers', label: '隐藏其他' },
        { role: 'unhide', label: '全部显示' },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '刷新' }, { role: 'forceReload', label: '强制刷新' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' }, { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        {
          label: '📂 打开日志目录',
          click: () => {
            const logsDir = path.join(SKYART_USER_DATA, 'logs');
            try { fs.mkdirSync(logsDir, { recursive: true }); } catch {}
            shell.openPath(logsDir);
          },
        },
      ],
    },
    {
      label: '窗口',
      submenu: [
        { role: 'minimize', label: '最小化' }, { role: 'zoom', label: '缩放' },
        { type: 'separator' }, { role: 'front', label: '前置所有窗口' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}
