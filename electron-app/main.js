const { app, BrowserWindow, Menu, ipcMain, shell, clipboard, dialog, nativeImage } = require('electron');
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

// 数据目录。名字与 app 包名解耦，改产品名不会让已有数据失联。
//
// 历史上叫 SkyArtDemo，「Demo」让人以为是试用版，改成 SkyArt。老用户升级时把
// 老目录原地改名过来——同一个卷上改名只动目录项，116 G 也是瞬间完成，不搬数据。
// 库里存的是相对路径（AdMake/Source/...），成片和导出目录是用户自己选的绝对路径，
// 都在数据目录之外，所以改名不需要动数据库。
const 数据目录名 = 'SkyArt';
const 老数据目录名 = 'SkyArtDemo';

/** 这个目录是不是一个真的数据家：装着业务数据或配置，而不是空壳。 */
function 是数据家(目录) {
  return fs.existsSync(path.join(目录, 'skyart-data')) ||
         fs.existsSync(path.join(目录, 'skyart-config.json'));
}

function 迁移老数据目录(appData) {
  const 新家 = path.join(appData, 数据目录名);
  const 老家 = path.join(appData, 老数据目录名);
  try {
    if (!fs.existsSync(老家)) return 新家;          // 全新安装，或早就迁过了
    // 老名字不是一个实打实的目录就别碰它。两种情况：一种是软链——数据已经迁过了，
    // 这条链是留给老版本的兼容入口，真去改名会得到一条指向自己的死链；另一种是有人
    // 在这儿留了个同名文件，改名过去之后 SkyArt 会变成一个文件，userData 建不出来，
    // 主进程直接崩。
    if (!fs.lstatSync(老家).isDirectory()) return 新家;
    if (fs.existsSync(新家)) {
      // 新名字是软链（有人手工建过反向链）：同理不碰
      if (!fs.lstatSync(新家).isDirectory()) return 新家;
      if (是数据家(新家)) {
        // 两个都是真数据家：该保哪一份要人来判断，程序不该猜。用新的，老的留在原地。
        console.warn(`[数据目录] ${老家} 和 ${新家} 都装着数据，本次用新的，老目录原样留着`);
        return 新家;
      }
      // 走到这里说明 SkyArt 目录存在但里面没有业务数据。这是 1.0 时代 Electron 按
      // app.name 自动建的引擎目录（Cache / Cookies / GPUCache），后来 chromium 被收进
      // 数据目录下，它就成了没人再写的残留。挪开而不是删掉：万一里面还有 Cookies
      // 这类能让人少登录一次的东西，留一份在旁边总比删干净强。
      const 挪到 = `${新家}-旧引擎缓存-${Date.now()}`;
      fs.renameSync(新家, 挪到);
      console.log(`[数据目录] ${新家} 里没有业务数据，是旧引擎缓存，挪到 ${挪到}`);
    }
    fs.renameSync(老家, 新家);
    // 老名字留一条软链指过来。装了这一版、迁移跑完之后，万一又打开旧版本的包
    // （回滚验证、DMG 没删干净），旧版本按老名字照样找得到数据；没有这条链的话
    // 它会当成全新安装，建一个空目录、空库，界面上一个项目都没有。
    try { fs.symlinkSync(数据目录名, 老家); }
    catch (e) { console.warn('[数据目录] 兼容软链没建上（不影响使用）:', e.message); }
    console.log(`[数据目录] 已从 ${老家} 改名为 ${新家}`);
    return 新家;
  } catch (e) {
    // 跨卷、权限不足、目录被别的进程占着——改名失败就接着用老目录。
    // 宁可名字难看，也不能因为改名失败让用户打不开应用、看不到自己的素材。
    console.warn('[数据目录] 改名失败:', e.message);
    // 先看新家。双开时另一个实例可能已经把迁移做完了，这一份的 rename 才会报错；
    // 这时候退回老目录的话，后面 buildBackendEnv 会在老名字下重新建出空的
    // skyart-data 和空库，用户看到零个项目。新家已经装着数据就用新家。
    if (是数据家(新家)) return 新家;
    return fs.existsSync(老家) ? 老家 : 新家;
  }
}

const SKYART_USER_DATA = 迁移老数据目录(app.getPath('appData'));

// Chromium 运行文件(缓存/cookie/localStorage/登录态)默认按 app.name 单独落一个顶层
// 目录，跟业务数据分家。这里把它收进数据目录下的 chromium 子目录：顶层只剩一个数据家，
// 运行文件与业务数据/配置仍按子目录分开，后续导入导出只操作 skyart-data，碰不到引擎
// 数据。必须在 app ready 前调用才生效。
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
    // 阿里云 MaaS 白名单专属通道。区域锁死 us-east-1，
    // 这条通道的 key 打公网 dashscope 或别的区域一律 invalid_api_key，Host 与 key 成对使用。
    HAPPYHORSE_BASE_URL:  _maasUrl(cfg.happyhorseBaseUrl),
    GEMINI_PROXY_URL:     cfg.geminiProxyUrl      || 'https://dscriptr.stg.shoplazza.com/skyart/gemini/analyze',
    GEMINI_PROXY_KEY:     cfg.geminiProxyKey      || '',
    QWEN_API_KEY:         cfg.qwenApiKey          || '',
    QWEN_BASE_URL:        _maasUrl(cfg.qwenBaseUrl),
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

// 装过旧版的机器上，这两个地址字段里存的是公网 dashscope——旧版设置面板在地址框
// 留空时会把公网地址写进配置文件，填过一次 API Key 的人必然踩到。改默认值救不了他们：
// 字段非空，`||` 兜底轮不到。白名单 key 打公网一律 invalid_api_key，用户在设置里
// 看到地址填着、key 也填着，换多少次 key 都过不去。这里把已知的旧地址当空值处理。
const _OLD_DASHSCOPE = /^https:\/\/dashscope(-us|-intl)?\.aliyuncs\.com/i;
function _maasUrl(v) {
  const s = (v || '').trim();
  if (!s || _OLD_DASHSCOPE.test(s)) return 'https://ws-bljb9l73522yj7vp.us-east-1.maas.aliyuncs.com/api/v1';
  return s;
}

let _最近崩溃 = '';
function 最近崩溃信息() { return _最近崩溃; }

function startBackend() {
  backendCrashed = false; // 重置崩溃标志
  _最近崩溃 = '';
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
      _最近崩溃 = backendStderr + backendStdout;
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
function 端口占用者() {
  try {
    return execSync(`lsof -ti tcp:${BACKEND_PORT} -sTCP:LISTEN 2>/dev/null || true`)
      .toString().trim().split('\n').filter(Boolean);
  } catch (e) {
    // 原来这里静默返回空数组，读起来跟「端口没人占」一模一样，于是照常起后端，
    // 起完撞端口退出。查日志时看不出是查不到还是真没人占，写一行出来。
    console.warn('[backend] lsof 查端口占用失败，按「无人占用」处理:', e.message);
    return [];
  }
}

// SIGKILL 是异步的，而且被杀的是上一轮残留的孤儿——它不是 Electron 的子进程，
// Electron 收不了尸，得等系统回收。这中间监听套接字还挂着，实测约 7 毫秒。
// 杀完立刻起后端的话，bind 一定撞 EADDRINUSE，后端 code=1 退出，用户看到崩溃弹窗。
// 所以这里要等端口真的空出来再返回。
function killPortOccupant() {
  const pids = 端口占用者();
  if (!pids.length) return true;
  for (const pid of pids) {
    try { process.kill(parseInt(pid, 10), 'SIGKILL'); console.log('[backend] killed stale occupant pid', pid); }
    catch (e) { console.warn('[backend] kill', pid, 'failed:', e.message); }
  }
  const 截止 = Date.now() + 5000;
  while (Date.now() < 截止) {
    if (!端口占用者().length) {
      console.log('[backend] 端口已空出');
      return true;
    }
    try { execSync('sleep 0.05'); } catch {}
  }
  console.warn('[backend] 等了 5 秒端口还被占着，占用者:', 端口占用者().join(','));
  return false;
}

// packaged：一律清场重起，杜绝强退残留的孤儿（KNOWLEDGE #7）。
// dev：端口上已有健康后端就复用，不 SIGKILL —— 否则会杀掉开发者手起的 --reload 热重载实例。
async function ensureBackend() {
  // 自己起的那个后端还活着、还健康，就接着用。关窗口不再停后端（见下面
  // window-all-closed），从 Dock 点回来时它多半还在跑着合成和分析，
  // 这里再清场重起的话，那些任务会被拦腰打断、启动清扫再把它们全标成失败。
  if (backendProcess && !backendProcess.killed) {
    try { await waitForBackend(1500); console.log('[backend] 复用仍在跑的后端'); return; }
    catch { /* 不健康或版本不对，照常清场重起 */ }
  }
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
  // 渲染进程否决退出时 Electron 默认静默照办，点关闭按钮什么都不发生，
  // 编辑器里写好的那句「导出进行中」提示根本没机会出现。这里接管，问一句。
  mainWindow.webContents.on('will-prevent-unload', (e) => {
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'question',
      buttons: ['继续等待', '仍然退出'],
      defaultId: 0,
      cancelId: 0,
      title: '有任务正在进行',
      message: '导出或 AI 分析还没结束。',
      detail: '现在退出会中断任务，已经转码的片段不会保留。',
    });
    if (choice === 1) e.preventDefault();   // preventDefault 在这里的含义是「不要阻止退出」
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

// 同时开两个 SkyArt，两边都会去清场、都去 bind 同一个端口，后起的那个必然
// 报 EADDRINUSE 并弹崩溃框。第二个实例直接退出，把已经开着的那个窗口叫到前面。
// app.quit() 是异步的：请求退出之后，本次进程照样会跑完 whenReady 里的启动流程，
// 也就照样会拉起一个后端。那个后端绑不上端口、几百毫秒后就退出，但它在绑端口
// 之前已经跑完了启动清扫，把另一个实例正在合成的 Part 全标成 failed
// （2026-09-18 上午 22 个 Part 合成、4 条分析就是这么没的）。
// 所以拿不到锁时不能只喊退出，还要把启动流程整个跳过。
const 是主实例 = app.requestSingleInstanceLock();
if (!是主实例) {
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

if (是主实例) app.whenReady().then(async () => {
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
  // 端口撞车是可以自愈的：孤儿进程被系统回收要几毫秒，这期间 bind 必失败。
  // 原来一次失败就 code=1 弹崩溃框，用户手动重启一次又好了——那一次重启本可以省掉。
  for (let 第几次 = 1; 第几次 <= 3 && !backendOK; 第几次++) {
    try {
      await waitForBackend(第几次 === 1 ? 120000 : 60000);
      console.log('[backend] Ready ✓' + (第几次 > 1 ? ` (第 ${第几次} 次)` : ''));
      backendOK = true;
      break;
    } catch (e) {
      console.error(`[backend] 第 ${第几次} 次启动失败:`, e.message);
      if (第几次 === 3) break;
      // 值得再试的两种：端口被占着起不来、连到了版本不符的残留实例。
      // 其余原因（缺文件、权限）重试也是白试，直接把崩溃信息交给用户。
      const 撞端口 = backendCrashed && /address already in use|Errno 48/i.test(最近崩溃信息());
      const 版本不符 = String(e.message).startsWith('VERSION_MISMATCH');
      if (!撞端口 && !版本不符) break;
      splashEval(`setHint('端口被占用，正在清理后重试…');`);
      killPortOccupant();
      startBackend();
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
    // 后端多半还活着（关窗口不再停它），ensureBackend 会先试着复用
    await ensureBackend();
    try { await waitForBackend(30000); } catch (e) { console.warn('[backend] activate wait:', e.message); }
    createMainWindow();
  });
});

// macOS 上关窗口不等于退出——应用还在 Dock 里。这里以前顺手把后端也停了，
// 于是正在跑的合成和分析全部中断，从 Dock 点回来时启动清扫再把它们标成失败：
// 传了 38 集、关掉窗口去干别的，回来一看全挂了。macOS 上让后端接着跑，
// 真正退出时由 will-quit 停它。其他平台关窗口就是退出，照旧停。
app.on('window-all-closed', () => {
  if (process.platform === 'darwin') return;
  stopBackend();
  app.quit();
});
// 停后端必须挂 will-quit，不能挂 before-quit。before-quit 在关窗口之前就触发，
// 而编辑器页的 beforeunload 在导出/AI 分析进行中会否决退出——后端已经 SIGTERM 掉了，
// 应用却还开着，之后每个请求都失败，用户只能强杀重开。will-quit 只在退出真的成立后触发。
app.on('will-quit', () => stopBackend());

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

// 选一张图当首页背景：复制一份进应用数据目录再返回，原图被用户挪走或删掉也不受影响。
// 顺带把尺寸带回去——比窗口小的图放大后发虚，界面要据此提示一句。
ipcMain.handle('select-background-image', async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    title: '选择背景图片',
    properties: ['openFile'],
    filters: [{ name: '图片', extensions: ['jpg', 'jpeg', 'png', 'webp'] }],
  });
  if (r.canceled || !r.filePaths.length) return null;
  const src = r.filePaths[0];
  const 目录 = path.join(SKYART_USER_DATA, 'skyart-data', 'background');
  fs.mkdirSync(目录, { recursive: true });
  // 固定文件名，换图直接覆盖，不会在目录里越堆越多。扩展名跟着源文件走。
  const 目标 = path.join(目录, 'home-bg' + path.extname(src).toLowerCase());
  // 换了格式时把上一张删掉，否则旧的那张一直占着盘
  for (const ext of ['.jpg', '.jpeg', '.png', '.webp']) {
    const 旧 = path.join(目录, 'home-bg' + ext);
    if (旧 !== 目标 && fs.existsSync(旧)) { try { fs.unlinkSync(旧); } catch (e) {} }
  }
  fs.copyFileSync(src, 目标);
  let width = 0, height = 0;
  try {
    const img = nativeImage.createFromPath(目标);
    const sz = img.getSize();
    width = sz.width; height = sz.height;
  } catch (e) { /* 读不出尺寸就不提示，不拦着用 */ }
  return { path: 目标, name: path.basename(src), width, height };
});

// 移除自定义背景图
ipcMain.handle('clear-background-image', () => {
  const 目录 = path.join(SKYART_USER_DATA, 'skyart-data', 'background');
  for (const ext of ['.jpg', '.jpeg', '.png', '.webp']) {
    const f = path.join(目录, 'home-bg' + ext);
    if (fs.existsSync(f)) { try { fs.unlinkSync(f); } catch (e) {} }
  }
  return { ok: true };
});

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
