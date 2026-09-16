const { exec } = require('child_process');
const { promisify } = require('util');
const fs = require('fs');
const path = require('path');
const execAsync = promisify(exec);

async function signPath(p) {
  await execAsync(`codesign --sign - --force --deep "${p}"`).catch(e => {
    console.warn('[afterPack] codesign warning:', e.message.split('\n')[0]);
  });
}

// 拆解管线的 OCR 程序。PyInstaller 按 datas 收进去的文件不保留执行位，
// 装好之后跑到认字幕那一步才会以「Permission denied」失败，而那是分析的第一步。
// 必须在 codesign 之前改，签完再动文件会让签名失效。
function fixOcrbinPerm(appPath) {
  const p = path.join(appPath, 'Contents', 'Resources', 'backend-bin',
                      '_internal', 'app', 'services', 'decompose', 'ocrbin');
  if (!fs.existsSync(p)) {
    console.warn('[afterPack] 没找到 ocrbin，段库分析装到机器上会跑不了认字幕：', p);
    return;
  }
  fs.chmodSync(p, 0o755);
  console.log('[afterPack] ocrbin 执行位已补上');
}

exports.default = async function (context) {
  if (context.electronPlatformName !== 'darwin') return;
  const appPath = path.join(context.appOutDir, 'SkyArt.app');
  fixOcrbinPerm(appPath);
  console.log('[afterPack] Ad-hoc signing:', appPath);

  // Sign nested .app bundles (Helper apps) before the main bundle
  const frameworksDir = path.join(appPath, 'Contents', 'Frameworks');
  if (fs.existsSync(frameworksDir)) {
    const entries = fs.readdirSync(frameworksDir);
    for (const entry of entries) {
      if (entry.endsWith('.app')) {
        await signPath(path.join(frameworksDir, entry));
      }
    }
  }

  // Sign the main bundle
  await signPath(appPath);
  console.log('[afterPack] Ad-hoc sign complete');
};
