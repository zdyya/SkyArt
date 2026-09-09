// 任务中心边缘悬浮件（AdMake 三个页面共用）：
// 服务端 /admake/tasks 是唯一事实源——该用户所有进行中的任务（导出/AI 分析/Part 合成），
// 完成即从列表消失，全部完成悬浮件隐藏。不再在 localStorage 存任务快照（旧方案的
// 僵尸轮询/误清/单任务覆盖一类问题随之消灭），只记折叠偏好。
// 形态：停靠右缘。折叠 = 小舌头(进度环+任务数)；展开 = 任务列表卡片，点行跳对应项目。
(function () {
  const COLLAPSE_KEY = 'skyart_export_task_collapsed';
  const PORT = (window.electronAPI && window.electronAPI.backendPort) || 8764;
  const API = `http://127.0.0.1:${PORT}/api/v1`;
  const LABEL = { export: '导出', ai: 'AI 分析', parts: 'Part 合成' };

  let tasks = [];       // [{type, project_id, title, count}]
  let pollTimer = null;
  let failStreak = 0;   // 连续查询失败次数：单次失败不算数，连续几次才认为状态不可信
  let root = null;
  let lastRowCount = -1;

  function collapsed() { return localStorage.getItem(COLLAPSE_KEY) === '1'; }

  function ensureDom() {
    if (root) return;
    root = document.createElement('div');
    root.id = 'export-task-float';
    root.innerHTML = `
      <style>
        #export-task-float{position:fixed;right:0;top:38%;z-index:9500;font-family:inherit;pointer-events:none;}
        /* 项目列表页的弹窗遮罩是 z-index:1000，浮窗 9500 压在它上面，而且能点。
           新建项目填到一半误点一下，表单连同已选的素材一起没了。弹窗开着就把浮窗收起来。
           编辑器自己的弹窗是 9999，本来就盖在浮窗上，不受这条影响。 */
        body:has(.modal-overlay:not(.hidden)) #export-task-float,
        body:has(#leave-modal:not(.hidden)) #export-task-float{display:none!important;}
        #export-task-float .etf{pointer-events:auto;}
        /* 折叠态：贴边玻璃圆球，hover 微微滑出。整个界面是浅色的，原来这颗球是深灰底，
           贴在右边像一块外来的东西；底色改成和展开卡片同一档的白玻璃。
           尺寸 44→56，且只推出去 30%（原来 40%），露出来的部分从 26px 变成 39px。
           环不再是画满一圈的静态橙圈——那既不表示进度也不表示在跑；换成转着的橙色缺口环，
           一眼看出后台还有任务在动。
           backdrop-filter 这条是兜底——liquid-glass.js 会用带 url() 的行内样式盖掉它做折射；
           运行时不认 url() 时行内那条被丢弃，这条还在，退化成原来的毛玻璃。 */
        #etf-tab{position:absolute;right:0;top:0;display:flex;align-items:center;justify-content:center;
          width:56px;height:56px;border-radius:50%;transform:translateX(20%);cursor:pointer;
          background:rgba(255,255,255,.55);backdrop-filter:blur(3px) saturate(135%) brightness(108%);
          box-shadow:inset 0 1.4px 0 rgba(255,255,255,1),
                     inset 0 -1px 0 rgba(255,255,255,.3),
                     inset 0 0 14px rgba(255,255,255,.35),
                     0 6px 20px rgba(24,28,36,.16);
          border:1px solid rgba(255,255,255,.5);
          transition:transform .35s cubic-bezier(.22,1,.36,1),opacity .3s;opacity:0;pointer-events:none;}
        #export-task-float.collapsed #etf-tab{opacity:1;pointer-events:auto;}
        #export-task-float.collapsed #etf-tab:hover{transform:translateX(6%);}
        /* 缺口环：conic 画 3/4 圈橙色，mask 掏空中间只留 2.5px 的圈，整体匀速转 */
        #etf-ring{width:34px;height:34px;border-radius:50%;position:relative;
          display:flex;align-items:center;justify-content:center;}
        #etf-ring::before{content:'';position:absolute;inset:0;border-radius:50%;
          background:conic-gradient(var(--accent,#f97316) 0deg 270deg,rgba(249,115,22,.16) 270deg 360deg);
          -webkit-mask:radial-gradient(circle,transparent 13.5px,#000 13.5px);
          mask:radial-gradient(circle,transparent 13.5px,#000 13.5px);
          animation:etfSpin 1.4s linear infinite;}
        #etf-ring-in{position:relative;display:flex;align-items:center;justify-content:center;
          font-size:13px;font-weight:700;color:var(--text,#191b1f);font-variant-numeric:tabular-nums;}
        /* 展开态：玻璃任务列表卡片，滑入。底色 48% 比工具条的 42% 厚一点——
           这里的字只有 11-12px，压在最亮的画面上也要读得清。 */
        #etf-card{position:absolute;right:0;top:-8px;width:248px;padding:10px 12px;
          background:rgba(255,255,255,.48);backdrop-filter:blur(3px) saturate(135%) brightness(108%);
          border-radius:14px 0 0 14px;
          box-shadow:inset 0 1.4px 0 rgba(255,255,255,1),
                     inset 0 -1px 0 rgba(255,255,255,.3),
                     inset 0 0 16px rgba(255,255,255,.35),
                     0 6px 20px rgba(24,28,36,.15);
          font-size:12px;color:var(--text,#222);
          transform:translateX(105%);opacity:0;pointer-events:none;
          transition:transform .35s cubic-bezier(.22,1,.36,1),opacity .3s;}
        #export-task-float:not(.collapsed) #etf-card{transform:none;opacity:1;pointer-events:auto;}
        #etf-head{display:flex;align-items:center;gap:7px;margin-bottom:4px;}
        #etf-dot{width:7px;height:7px;border-radius:50%;background:var(--accent,#f97316);flex-shrink:0;animation:etfPulse 1.6s ease infinite;}
        @keyframes etfPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.8)}}
        #etf-title{font-weight:700;flex:1;}
        #etf-collapse{cursor:pointer;padding:6px 10px;margin:-4px 0 -4px -8px;font-size:15px;color:var(--text3,#999);border-radius:6px;line-height:1;}
        #etf-collapse:hover{background:var(--bg3,#f2f2f2);color:var(--text,#222);}
        .etf-row{display:flex;align-items:center;gap:8px;padding:7px 6px;border-radius:6px;cursor:pointer;}
        .etf-row:hover{background:color-mix(in srgb,var(--bg3,#f2f2f2) 70%,transparent);}
        .etf-row .n{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;}
        .etf-row .t{color:var(--text3,#999);font-size:11px;white-space:nowrap;}
        .etf-row .sp{width:10px;height:10px;border:2px solid var(--accent,#f97316);border-top-color:transparent;
          border-radius:50%;flex-shrink:0;animation:etfSpin 1s linear infinite;}
        @keyframes etfSpin{to{transform:rotate(360deg)}}
      </style>
      <div id="etf-tab" class="etf" title="展开任务中心">
        <div id="etf-ring"><div id="etf-ring-in">0</div></div>
      </div>
      <div id="etf-card" class="etf">
        <div id="etf-head">
          <span id="etf-collapse" title="收到边缘">»</span>
          <span id="etf-dot"></span>
          <div id="etf-title">进行中的任务</div>
        </div>
        <div id="etf-rows"></div>
      </div>`;
    document.body.appendChild(root);
    root.querySelector('#etf-tab').onclick = () => { localStorage.setItem(COLLAPSE_KEY, '0'); render(); };
    root.querySelector('#etf-collapse').onclick = () => { localStorage.setItem(COLLAPSE_KEY, '1'); render(); };
    // 优先用新的液态镜片；老页面还没换过来时退回 liquid-glass.js
    const L = window.LiquidLens || window.LiquidGlass;
    if (L) {
      const card = root.querySelector('#etf-card'), tab = root.querySelector('#etf-tab');
      if (window.LiquidLens) {
        LiquidLens.glass(card, { band: 6, scale: 90, disp: .24 });
        LiquidLens.glass(tab,  { band: 6, scale: 90, disp: .24, jelly: { amp: 1.2, freq: 3.6 } });
      } else {
        L.apply(card, { radius: 14, band: 12, scale: 26 });
        L.apply(tab,  { radius: 28, band: 11, scale: 24 });
      }
    }
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  function go(pid, type) {
    // 导出任务：跳过去必须能看到进度和成败，落地即开导出历史弹窗（history=1）；
    // AI 分析 / Part 合成：页面本身逐 Part 挂状态徽章，落地即可见
    const page = location.pathname.split('/').pop();
    if (page === 'editor.html'
        && new URLSearchParams(location.search).get('projectId') === String(pid)) {
      if (type === 'export') {
        // 本页导出还活着：开实时进度弹窗（带百分比）；否则开导出历史（服务端数据，永远是活的）
        if (window.__exportUiLive && window.__exportUiLive() && window.expandExportProgress) window.expandExportProgress();
        else if (window.openHistoryModal) window.openHistoryModal();
      } else if (type === 'ai') {
        // AI 分析被收起过：重新展开进度弹窗；本页没在跑就什么都不做，Part 列表自带状态点
        if (window.__aiUiLive && window.__aiUiLive() && window.expandAiProgress) window.expandAiProgress();
      }
      return;
    }
    const target = type === 'export'
      ? `editor.html?projectId=${pid}&history=1`
      : `editor.html?projectId=${pid}`;
    // 模板编辑器的未保存改动只活在内存里，它的确认弹窗只挂在自己的返回按钮上。
    // 从这里直接跳走会绕过去，改了一半的版式全丢。有钩子就把跳转交给页面自己。
    if (window.__guardLeave && window.__guardLeave(target)) return;
    // 编辑器页的 beforeunload 会拦没放行的跳转（导出/分析中防误关应用），先放行再跳
    if (window.__allowInAppNav) window.__allowInAppNav();
    location.href = target;
  }

  function render() {
    if (!tasks.length) { if (root) root.style.display = 'none'; return; }
    ensureDom();
    root.style.display = 'block';
    root.classList.toggle('collapsed', collapsed());
    root.querySelector('#etf-ring-in').textContent = tasks.length;
    root.querySelector('#etf-rows').innerHTML = tasks.map(t => `
      <div class="etf-row" data-pid="${t.project_id}" data-type="${t.type}" title="进入该项目查看进度">
        <span class="sp"></span>
        <span class="n">《${esc(t.title)}》</span>
        <span class="t">${LABEL[t.type] || t.type}${t.count > 1 ? ' ' + t.count : ''}</span>
      </div>`).join('');
    root.querySelectorAll('.etf-row').forEach(r => r.onclick = () => go(+r.dataset.pid, r.dataset.type));
    // 任务条数变了卡片就变高，位移图要跟着重算。条数没变不碰——render 每 4 秒跑一次，
    // 每次都重设 backdrop-filter 会白白触发样式重算。
    if (lastRowCount !== tasks.length) {
      lastRowCount = tasks.length;
      if (window.LiquidLens) LiquidLens.refresh();
      else if (window.LiquidGlass) LiquidGlass.refresh();
    }
  }

  // 拿不到新数据时必须把上一份丢掉。屏幕上这张卡片说的是"现在有什么在跑"，
  // 一旦查不到，我们就不知道现在有什么在跑了——继续挂着上一次的快照，
  // 用户看到的就是一个早就导完的任务永远显示"导出中"，点它还能跳转。
  // 宁可什么都不显示，也不显示一个不知道对不对的东西。
  function dropStale(why) {
    if (!tasks.length) return;
    tasks = [];
    lastRowCount = -1;
    if (root) root.style.display = 'none';
    console.warn('[任务中心] 状态已过期，先隐藏：' + why);
  }

  async function poll() {
    try {
      const token = localStorage.getItem('skyart_token') || '';
      if (!token) { dropStale('本地没有登录凭证'); return; }
      const res = await fetch(`${API}/admake/tasks`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      // 登录失效：不再永久停掉轮询——原来停掉之后这一页剩下的时间里卡片就冻在
      // 最后一次的结果上，重新登录也不会自己恢复，非得刷新页面。改成先清空，
      // 然后继续按原节奏轮询，token 一换回来下一轮就自己接上了。
      if (res.status === 401) { dropStale('登录已失效'); return; }
      if (!res.ok) { failStreak++; if (failStreak >= 3) dropStale(`接口连续 ${failStreak} 次返回 ${res.status}`); return; }
      failStreak = 0;
      tasks = await res.json();
      render();
    } catch (e) {
      // 单次失败可能只是后端正被 ffmpeg 压满，连续 3 次（约 12 秒）才认为状态不可信
      failStreak++;
      if (failStreak >= 3) dropStale(`连续 ${failStreak} 次请求失败`);
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, 4000);
    poll();
  }

  // 对页面暴露：发起导出等任务后调用可立即刷新（不调也会在 4 秒内轮到）
  window.ExportTaskFloat = {
    start() { poll(); },
    clear() { poll(); },
  };

  startPolling();
})();
