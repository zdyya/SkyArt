// 打包前置：准备 bin/ffmpeg-<arch>/ 下的 ffmpeg/ffprobe，供 electron-builder extraResources 打进 app。
//
// arm64（正式渠道）：从 martin-riedl.de 下载固定版本的静态构建，SHA-256 写死在本文件里，
//   下载后逐一校验——构建源被篡改或换包会直接失败，不会把来路不明的二进制打进安装包。
//   已下载且版本正确时跳过下载（bin/ 有缓存则离线可打包）。
// x64（遗留渠道）：仍用 @ffmpeg-installer 的 4.4——静态构建源已停止提供 Intel 版，
//   且 CI 只出 arm64 包，x64 仅本地兼容需要时使用。
//
// bin/ 已被 .gitignore 忽略（二进制不进仓库）。`npm run pack` / `pack:x64` 已自动调用本脚本。
// 用法： node scripts/setup-ffmpeg.js [arch]   （arch 默认取 process.arch）
const fs = require('fs');
const path = require('path');
const { execSync, execFileSync } = require('child_process');

// ── arm64 固定版本（升级 ffmpeg = 改这一段 + 重跑打包验证）──
const FFMPEG_VERSION = '8.1.2';
const BASE_URL = 'https://ffmpeg.martin-riedl.de/download/macos/arm64/1783011502_8.1.2';
const SHA256 = {
  'ffmpeg.zip':  'ef1aa60006c7b77ce170c1608c08d8e4ba1c30c5746f2ac986ded932d0ac2c3c',
  'ffprobe.zip': 'c39787f4af7a3932502d2d48db6f6feaaa836b48a73ef78c32cc3285df61dfaf',
};

const arch = process.argv[2] || process.arch; // arm64 / x64
const outDir = path.join(__dirname, '..', 'bin', `ffmpeg-${arch}`);
fs.mkdirSync(outDir, { recursive: true });

function binVersion(binPath) {
  try {
    return execFileSync(binPath, ['-version'], { encoding: 'utf8' }).split('\n')[0];
  } catch { return null; }
}

function setupArm64() {
  // 缓存命中：两个二进制都在且版本号对得上，跳过下载
  const cached = ['ffmpeg', 'ffprobe'].every(n => {
    const v = binVersion(path.join(outDir, n));
    return v && v.includes(FFMPEG_VERSION);
  });
  if (cached) {
    console.log(`✓ ffmpeg/ffprobe ${FFMPEG_VERSION} 已就绪（缓存）→ ${outDir}`);
    return;
  }
  for (const name of ['ffmpeg', 'ffprobe']) {
    const zipName = `${name}.zip`;
    const zipPath = path.join(outDir, zipName);
    console.log(`↓ 下载 ${zipName}（${FFMPEG_VERSION}）…`);
    execSync(`curl -fsSL --retry 3 -A "SkyArt-build" -o "${zipPath}" "${BASE_URL}/${zipName}"`, { stdio: 'inherit' });
    const got = execSync(`shasum -a 256 "${zipPath}"`, { encoding: 'utf8' }).split(/\s+/)[0];
    if (got !== SHA256[zipName]) {
      fs.rmSync(zipPath, { force: true });
      console.error(`✗ ${zipName} SHA-256 不匹配（期望 ${SHA256[zipName]}，实际 ${got}）。构建源可能被篡改，终止打包。`);
      process.exit(1);
    }
    execSync(`unzip -o -q "${zipPath}" -d "${outDir}"`);
    fs.rmSync(zipPath, { force: true });
    fs.chmodSync(path.join(outDir, name), 0o755);
    const v = binVersion(path.join(outDir, name));
    if (!v || !v.includes(FFMPEG_VERSION)) {
      console.error(`✗ ${name} 解包后无法运行或版本不对（${v}）。`);
      process.exit(1);
    }
    console.log(`✓ ${name} ${FFMPEG_VERSION} → ${path.join(outDir, name)}`);
  }
  console.log(`ffmpeg/ffprobe ${FFMPEG_VERSION} (arm64) 已就绪 → ${outDir}`);
}

function setupX64Legacy() {
  console.warn('⚠ x64 走遗留渠道（@ffmpeg-installer，ffmpeg 4.4）：静态构建源无 Intel 版。');
  const sources = [
    [require('@ffmpeg-installer/ffmpeg').path, 'ffmpeg'],
    [require('@ffprobe-installer/ffprobe').path, 'ffprobe'],
  ];
  for (const [src, name] of sources) {
    if (!src || !fs.existsSync(src)) {
      console.error(`✗ 找不到 ${name} 二进制（${src}）。请先 npm install。`);
      process.exit(1);
    }
    const dest = path.join(outDir, name);
    fs.copyFileSync(src, dest);
    fs.chmodSync(dest, 0o755);
    console.log(`✓ ${name} → ${dest}`);
  }
  console.log(`ffmpeg/ffprobe (x64) 已就绪 → ${outDir}`);
}

if (arch === 'arm64') setupArm64();
else setupX64Legacy();
