/* 在浏览器里跑 SkyArt 时，补一个 window.electronAPI 的替身。
 *
 * 页面里几十处调用 window.electronAPI.xxx()，装在 Electron 里由 preload.js 提供。
 * 直接用浏览器打开这些页面，那些调用会在第一处就抛 undefined，页面停在半路。
 * 这个文件补齐同名方法，让调用点一行都不用改。
 *
 * 装在 Electron 里时 preload.js 先跑，这里检测到已经有了就什么都不做。
 *
 * 能力差异照实降级，不假装成功：
 * - 选文件夹、在访达里显示：浏览器给不了本机路径，改成把路径显示出来让人自己去找。
 * - 改 API Key：key 由启动脚本的环境变量给，浏览器里改不了后端进程的环境。
 */
(function () {
  if (window.electronAPI) return;               // Electron 里跑，用真的那个

  const PORT = Number(new URLSearchParams(location.search).get('backend')) || 8764;
  const API = `http://127.0.0.1:${PORT}/api/v1`;

  const 提示 = (msg) => {
    if (typeof window.toast === 'function') window.toast(msg, 'info', 5000);
    else console.log('[web]', msg);
  };

  async function 取(path, 兜底) {
    try {
      const r = await fetch(API + path);
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch (e) { return 兜底; }
  }

  window.electronAPI = {
    backendPort: PORT,
    是网页版: true,

    async getUploadDir() {
      const d = await 取('/system/upload-dir', { dir: '' });
      return d.dir || '';
    },

    // 浏览器打不开访达。把绝对路径摆出来并塞进剪贴板，自己去粘。
    showItemInFolder(p) {
      提示(`网页版打不开访达。路径已复制：${p}`);
      this.copyToClipboard(p);
    },

    async copyToClipboard(text) {
      const s = Array.isArray(text) ? text.join('\n') : String(text ?? '');
      try {
        await navigator.clipboard.writeText(s);
        return true;
      } catch (e) {
        // 没有 https 或没授权时 clipboard API 不可用，退回老办法
        const ta = document.createElement('textarea');
        ta.value = s;
        ta.style.cssText = 'position:fixed;left:-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (err) { /* 复制不了就算了 */ }
        ta.remove();
        return true;
      }
    },

    // 浏览器没有目录选择器（File System Access API 在 file:// 和非 https 下不稳）。
    // 返回空＝用户取消，调用方本来就按这个分支走，不会把空串当成选了根目录。
    async selectFolder() {
      提示('网页版选不了文件夹。导出目录留空会用应用默认目录，要改目录请在装好的 app 里改。');
      return null;
    },

    // key 在启动脚本的环境变量里，浏览器改不了后端进程的环境。
    async getConfig() { return {}; },
    async saveConfig() {
      提示('网页版改不了 API Key。key 由启动脚本传给后端，改 key 请编辑 scripts/dev-web.sh 旁边的 .env 再重启脚本。');
      return false;
    },

    // 首页按 localStorage 里的 skyart_api_configured 决定弹不弹配置面板。
    // 浏览器 profile 是新的，这个标记必然为空；而 key 由启动脚本从装好的 app
    // 那份配置里取好了，再让人填一遍是多此一举。问后端要真实状态来定。
    async 同步配置状态() {
      const d = await 取('/system/config-status', null);
      if (!d) return;
      const 配好了 = ['ai_gemini', 'ai_qwen', 'drama_video'].some(k => d[k] && d[k].ok);
      if (配好了) localStorage.setItem('skyart_api_configured', '1');
    },

    async getDiagnostics() {
      const [h, c] = await Promise.all([
        取('/system/health', {}), 取('/system/config-status', {}),
      ]);
      return { ...h, ...c, mode: 'web' };
    },
  };

  // 在页面自己的脚本跑之前把标记补上。首页是在 DOMContentLoaded 里读它的，
  // 这个 fetch 抢在那之前发出去，慢一步也只是多弹一次，刷新就好。
  window.electronAPI.同步配置状态();
})();
