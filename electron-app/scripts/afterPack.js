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

exports.default = async function (context) {
  if (context.electronPlatformName !== 'darwin') return;
  const appPath = path.join(context.appOutDir, 'SkyArt.app');
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
