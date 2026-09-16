const { execSync, spawn } = require('child_process');
const http = require('http');
const path = require('path');

const arch = process.argv[2] || 'arm64';
const distDir = arch === 'x64' ? 'mac' : 'mac-arm64';
const appVersion = require('../package.json').version;
const resDir = path.join(__dirname, `../dist/${distDir}/SkyArt.app/Contents/Resources`);
const backendBin = path.join(resDir, 'backend-bin', 'skyart-backend');
const soPath = path.join(resDir, 'backend-bin/_internal/python3.12/lib-dynload/pyexpat.cpython-312-darwin.so');

// ── 校验 1：包内后端版本 == package.json（防“壳新芯旧”：PyInstaller 没重跑就打包）──
function checkVersion() {
  return new Promise(resolve => {
    const PORT = 8791;
    // 后端的输出不能丢：它没起来时这段是唯一的线索，早先用 stdio:'ignore'
    // 只能看到「无响应」三个字，查不下去。
    const proc = spawn(backendBin, [], { env: { ...process.env, PORT: String(PORT) },
                                         stdio: ['ignore', 'pipe', 'pipe'] });
    let log = '';
    const grab = d => { log = (log + d).slice(-4000); };
    proc.stdout.on('data', grab);
    proc.stderr.on('data', grab);
    // PyInstaller onedir 冷启动要 import + 建表。CI 的 Intel 机器比本机慢得多，
    // 60s 不够，放到 180s；起得来的话早就返回了，等不满这个数。
    const deadline = Date.now() + Number(process.env.VERIFY_TIMEOUT_MS || 180000);
    let finished = false;
    const done = ver => {
      if (finished) return; finished = true;
      try { proc.kill('SIGKILL'); } catch {}
      const ok = ver === appVersion;
      console.log(`打包验证 [${arch}] · 包内后端版本 = ${ver || '(无响应)'} / package.json = ${appVersion} — ${ok ? '一致 ✓' : '❌ 不一致，包内后端是旧的'}`);
      if (!ok && log.trim()) console.log(`打包验证 [${arch}] · 后端输出：\n${log.trim()}`);
      resolve(ok);
    };
    // 二进制缺失/无执行权限时 spawn 异步 emit 'error'，无监听者会让脚本崩栈；这里收成干净判负
    proc.on('error', err => { console.error(`打包验证 [${arch}] · 无法启动包内后端：${err.message}`); done(null); });
    const tick = () => {
      if (finished) return;
      const req = http.get(`http://127.0.0.1:${PORT}/`, res => {
        let b = ''; res.on('data', d => b += d);
        res.on('end', () => { let ver = null; try { ver = JSON.parse(b).version; } catch {} done(ver); });
      });
      req.on('error', () => { if (Date.now() > deadline) done(null); else setTimeout(tick, 400); });
      req.setTimeout(2000, () => req.destroy());
    };
    tick();
  });
}

// ── 校验 2：包内 ffmpeg/ffprobe 版本（防 bin/ 旧缓存；两个都查——setup 下载中断可能只换了一个，
//    混版本包不许出厂）。x64 走遗留渠道 4.4，只有 arm64 强校验 ──
function checkFfmpeg() {
  if (arch !== 'arm64') return true;
  const expect = '8.1.2'; // 与 setup-ffmpeg.js 的 FFMPEG_VERSION 保持一致
  let allOk = true;
  for (const name of ['ffmpeg', 'ffprobe']) {
    try {
      const out = execSync(`"${path.join(resDir, 'ffmpeg', name)}" -version`).toString().split('\n')[0];
      const ok = out.includes(expect);
      console.log(`打包验证 [${arch}] · 包内 ${name} = ${out.replace(/ Copyright.*/, '')} / 期望 ${expect} — ${ok ? '一致 ✓' : '❌ 不一致，bin/ 缓存是旧的，删掉 bin/ 重新打包'}`);
      allOk = allOk && ok;
    } catch (e) {
      console.error(`打包验证 [${arch}] · 包内 ${name} 无法运行：${e.message}`);
      allOk = false;
    }
  }
  return allOk;
}

// ── 校验 3：拆解管线的 OCR 程序在不在、能不能跑。缺了的话段库分析第一步就失败 ──
function checkOcrbin() {
  const p = path.join(resDir, 'backend-bin/_internal/app/services/decompose/ocrbin');
  const fs = require('fs');
  if (!fs.existsSync(p)) {
    console.error(`打包验证 [${arch}] · ocrbin 不在包里 — ❌ spec 的 datas 漏了 app/services/decompose/ocrbin`);
    return false;
  }
  const mode = fs.statSync(p).mode & 0o777;
  const ok = (mode & 0o111) !== 0;
  console.log(`打包验证 [${arch}] · ocrbin 权限 = ${mode.toString(8)} — ${ok ? '可执行 ✓' : '❌ 没有执行位，afterPack 的 chmod 没生效'}`);
  return ok;
}

// ── 随包的剪辑模板。少了它模板库是空的，按模板剪一个模板都选不到 ──
function checkCutTemplates() {
  const fs = require('fs');
  const d = path.join(resDir, 'backend-bin/_internal/app/data');
  const n = fs.existsSync(d) ? fs.readdirSync(d).filter(f => f.endsWith('.json')).length : 0;
  const ok = n > 0;
  console.log(`打包验证 [${arch}] · 随包剪辑模板 = ${n} 个 — ${ok ? '有 ✓' : '❌ spec 的 datas 漏了 app/data'}`);
  return ok;
}

// ── 校验 4：minos（对外发版须 ≤13，本地 macOS 新系统打的包仅自测）──
function checkMinos() {
  try {
    const out = execSync(`otool -l "${soPath}"`).toString();
    let minos = 'unknown';
    const m = out.match(/^\s+minos\s+([\d.]+)/m);
    const v = out.match(/LC_VERSION_MIN_MACOSX[\s\S]*?version\s+([\d.]+)/m);
    if (m) minos = m[1]; else if (v) minos = v[1];
    const ok = minos !== 'unknown' && parseFloat(minos) <= 13;
    console.log(`打包验证 [${arch}] · pyexpat minos = ${minos} — ${ok ? '兼容 macOS 13+ ✓' : '❌ 新系统编译，对外发版请走 CI (macos-14 runner)'}`);
    return ok;
  } catch (e) {
    console.error('minos 检查失败：', e.message);
    return false;
  }
}

(async () => {
  const verOk = await checkVersion();   // 最关键：版本对不上直接判包报废
  const ffOk = checkFfmpeg();
  const ocrOk = checkOcrbin();
  const tplOk = checkCutTemplates();
  const minosOk = checkMinos();
  console.log('');
  if (!verOk) { console.error('→ 版本校验未过，这个包不能用（包内后端不是当前源码）。'); process.exit(1); }
  if (!ffOk) { console.error('→ ffmpeg 校验未过，这个包不能用（包内 ffmpeg 不是目标版本）。'); process.exit(1); }
  if (!ocrOk) { console.error('→ ocrbin 校验未过，段库分析的认字幕这一步会失败。'); process.exit(1); }
  if (!tplOk) { console.error('→ 剪辑模板校验未过，按模板剪没有模板可选。'); process.exit(1); }
  if (!minosOk) { console.error('→ minos 未过：本地自测可用，对外发版请走 CI。'); process.exit(1); }
  console.log(`✅ 打包验证全部通过 [${arch}]`);
})();
