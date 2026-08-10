// 液态镜片：6D 钢化膜那种玻璃 —— 边缘一圈窄弧面，中间是平的高透区。
//
// 和 tools/ad-make/liquid-glass.js 的关系：那个是上一代，只做一次位移、无色散、
// 无镜边反光、无交互形变，而且它的位移图是「整块背景都在被拉伸、最外一圈急刹车收回 0」，
// 所以弧面一宽，中间就会浮出一个方框（那道急刹车的界线）。这里重做了三件事：
//
// 1. 位移只发生在边缘 band 那一圈，中间精确为零。距离在像素空间算，band 就是真的像素宽度
//    （上一代按宽高各自归一化，同一个数字在横竖两个方向对应的实际宽度并不相等）。
//    方向取距离场的梯度，也就是边界法线，圆角处的折射方向才是对的。
// 2. 色散：同一张位移图跑三遍，R/G/B 各用不同的位移量再拼回去。位移量在中心是 0，
//    所以色散只出现在弧面上——跟真透镜一样，正对着看的那块是无色的。
// 3. 液态：点击时一条阻尼弹簧同时驱动形变、折射强度和圆角，过冲两次后收敛。
//
// 玻璃背后必须有内容。背后是纯色时，像素挪来挪去还是同一个色，什么都看不见。
// 调用方在 CSS 里先写一条不带 url() 的 backdrop-filter 作兜底。
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  // 默认值是 2026-08-09 在预览里调定的那一组
  // push = 弧面上最大把背景拽走多少像素。它必须和 band 同一个量级：
  // 弧面只有 6px 宽，却从 55px 外抓东西进来，一条 1px 细线就会被拉成一道长钩——
  // 那就是「边角有污渍」的来源。默认 push = band，即最远拽到弧面自己那么宽。
  // （feDisplacementMap 的 scale 是最大位移的两倍，所以内部 scale = push * 2。）
  //
  // disp 默认 0：色散把 R/G/B 用三个不同的位移量采样，背后是 1px 细线时，
  // 一条线会被拆成三条各自带色的线，圆角处三条挤在一起堆成彩色楔形。
  // 我们的背景恰恰是细线网格，所以这两样不能同时开。要用得先把背景换成连续色调。
  // 2026-08-10 定稿的一组：调参面板拖出来的。想让玻璃更明显，最大的一档是背景线条浓度
  // （--ink，玻璃折射的是背后的边，背后没东西可折就什么都看不见），其次是底色调淡。
  // 折射(push)不能比弧面宽(band)大太多：6px 的弧面从 40px 外抓东西进来，细网格线会被
  // 拉成一道长钩，读起来就是"边角有污渍"。所以 push 涨的同时 band 也从 6 加到 8。
  const DEFAULTS = { band: 8, push: 17, disp: .12, blur: 1.5, sat: 165, bright: 106, radius: 14 };

  const items = [];
  let defs = null, seq = 0, resizeTimer = null;

  function ensureDefs() {
    if (defs) return defs;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    svg.style.cssText = 'position:absolute;width:0;height:0;pointer-events:none;';
    defs = document.createElementNS(NS, 'defs');
    svg.appendChild(defs);
    document.body.appendChild(svg);
    return defs;
  }

  function buildMap(w, h, radius, band) {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d'), im = g.createImageData(w, h);
    const ss = t => { t = Math.max(0, Math.min(1, t)); return t * t * (3 - 2 * t); };
    const cl = v => Math.round(Math.max(0, Math.min(255, v)));
    const rr = Math.min(radius, Math.min(w, h) / 2);
    // 圆角矩形有符号距离场（像素空间），内部为负
    const D = (px, py) => {
      const qx = Math.abs(px) - (w / 2 - rr), qy = Math.abs(py) - (h / 2 - rr);
      return Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - rr;
    };
    const B = Math.max(1, band);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const px = x - w / 2 + .5, py = y - h / 2 + .5;
        const amp = 1 - ss(-D(px, py) / B);      // 边上 1，进到 band 以内衰减到 0
        let dx = 0, dy = 0;
        if (amp > 0.0008) {                       // 内部留精确的零，避免亚像素噪声
          let gx = D(px + .5, py) - D(px - .5, py);
          let gy = D(px, py + .5) - D(px, py - .5);
          const gl = Math.hypot(gx, gy) || 1;
          dx = (gx / gl) * amp; dy = (gy / gl) * amp;
        }
        const i = (y * w + x) * 4;
        im.data[i]     = cl((dx * .5 + .5) * 255);
        im.data[i + 1] = cl((dy * .5 + .5) * 255);
        im.data[i + 2] = 128;
        im.data[i + 3] = 255;
      }
    }
    g.putImageData(im, 0, 0);
    return c.toDataURL();
  }

  const mk = (n, a) => { const e = document.createElementNS(NS, n);
    for (const k in a) e.setAttribute(k, a[k]); return e; };

  function ensureFilter(rec) {
    if (rec.id) return;
    rec.id = 'll-lens-' + (++seq);
    const f = mk('filter', { id: rec.id, x: '-6%', y: '-6%', width: '112%', height: '112%',
                             'color-interpolation-filters': 'sRGB' });
    rec.img = mk('feImage', { result: 'm0', preserveAspectRatio: 'none' });
    f.appendChild(rec.img);
    // 中性值校正：不偏移只能编码成 128，而 128/255 = 0.50196，比真正的中性 0.5 多 0.00196。
    // 三条通路 scale 不同，这点残差会被放大成三个不同的亚像素位移，中间那块高透区的
    // 1px 细线就会无端起彩边。减掉 0.5/255，中心就是精确的零位移。
    f.appendChild(mk('feColorMatrix', { in: 'm0', type: 'matrix', result: 'm',
      values: '1 0 0 0 -0.0019608  0 1 0 0 -0.0019608  0 0 1 0 0  0 0 0 1 0' }));
    rec.dm = {};
    ['R', 'G', 'B'].forEach(ch => {
      rec.dm[ch] = mk('feDisplacementMap', { in: 'SourceGraphic', in2: 'm',
        xChannelSelector: 'R', yChannelSelector: 'G', result: 'd' + ch });
      f.appendChild(rec.dm[ch]);
      const v = ch === 'R' ? '1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0'
              : ch === 'G' ? '0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0'
              :              '0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0';
      f.appendChild(mk('feColorMatrix', { in: 'd' + ch, type: 'matrix', values: v, result: 'c' + ch }));
    });
    f.appendChild(mk('feComposite', { in: 'cR', in2: 'cG', operator: 'arithmetic',
                                      k1: 0, k2: 1, k3: 1, k4: 0, result: 'rg' }));
    f.appendChild(mk('feComposite', { in: 'rg', in2: 'cB', operator: 'arithmetic',
                                      k1: 0, k2: 1, k3: 1, k4: 0 }));
    ensureDefs().appendChild(f);
  }

  function paint(rec) {
    const el = rec.el, o = rec.o;
    const w = Math.round(el.offsetWidth), h = Math.round(el.offsetHeight);
    if (!w || !h) return;                       // 隐藏时不画，等下次 refresh
    const band = Math.min(o.band, Math.min(w, h) / 3);
    const radius = parseFloat(getComputedStyle(el).borderTopLeftRadius) || o.radius;
    const key = w + 'x' + h + 'x' + band + 'x' + Math.round(radius);
    ensureFilter(rec);
    if (rec.key !== key) {                      // 只在尺寸变化时重算位移图
      rec.key = key;
      rec.img.setAttribute('href', buildMap(w, h, radius, band));
      rec.img.setAttribute('width', w);
      rec.img.setAttribute('height', h);
    }
    rec.band = band;
    rec.setScale(rec.pushScale());
    el.style.setProperty('--band', band + 'px');   // 镜边反光和棱镜扇共用这个宽度
    el.style.backdropFilter = 'url(#' + rec.id + ')'
      + (o.blur > 0 ? ' blur(' + o.blur + 'px)' : '')
      + ' saturate(' + o.sat + '%) brightness(' + o.bright + '%)';
  }

  function apply(el, opts) {
    if (!el) return null;
    let rec = items.find(r => r.el === el);
    if (rec) { Object.assign(rec.o, opts || {}); rec.key = null; }
    else {
      rec = { el: el, o: Object.assign({}, DEFAULTS, opts || {}) };
      // 没给 push 就按弧面宽走；给了 scale 就当原始值直接用（留个后门）
      rec.pushScale = () => rec.o.scale != null ? rec.o.scale
                          : (rec.o.push != null ? rec.o.push : (rec.band || rec.o.band)) * 2;
      rec.setScale = s => {                      // 三条通路一起改，色散比例不变
        const d = rec.o.disp;
        rec.dm.R.setAttribute('scale', s * (1 + d));
        rec.dm.G.setAttribute('scale', s);
        rec.dm.B.setAttribute('scale', s * (1 - d));
      };
      items.push(rec);
    }
    paint(rec);
    return rec;
  }

  function refresh() {
    for (let i = items.length - 1; i >= 0; i--) {
      const rec = items[i];
      if (!rec.el.isConnected) {
        // 元素已经被重绘掉了，连它的滤镜节点一起回收。
        // 列表页翻一次页就重建十几张卡，不回收的话 defs 里会一直堆滤镜。
        if (rec.id) { const f = document.getElementById(rec.id); if (f) f.remove(); }
        items.splice(i, 1);
        continue;
      }
      paint(rec);
    }
  }
  const recOf = el => items.find(r => r.el === el) || null;

  // ── 液态 ────────────────────────────────────────────────────────────────
  // s(t) = e^(-λt)·cos(ωt)，从 1 衰减到 0，中途过冲到负值再回来——过冲那一下就是手感。
  // 同一条曲线喂给三处：transform 的 scaleX/scaleY 反相（压扁再回弹，纯等比缩放只是变大变小）、
  // feDisplacementMap 的 scale（折射跟着抖，边缘的背景在晃）、border-radius（圆角一起呼吸，
  // 少了这条会像一块硬板在弹）。不碰 filter/opacity，那两个会让整块重新合成、掉帧。
  const springs = new Map();
  let gain = 2;                                  // 全局 Q 弹倍率
  function jelly(el, opts) {
    if (gain === 0) return;
    const cfg = Object.assign({ amp: 1, dur: 620, freq: 3.1, damp: 6.4 }, opts || {});
    const A = cfg.amp * gain, rec = recOf(el);
    const base = rec ? rec.pushScale() : 0;
    const baseR = parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0;
    const prev = springs.get(el); if (prev) cancelAnimationFrame(prev);
    const t0 = performance.now();
    const step = now => {
      const t = (now - t0) / 1000;
      if ((now - t0) / cfg.dur >= 1) {
        el.style.transform = ''; el.style.borderRadius = '';
        if (rec) rec.setScale(base);
        springs.delete(el); return;
      }
      const s = Math.exp(-cfg.damp * t) * Math.cos(2 * Math.PI * cfg.freq * t);
      el.style.transform = `scale(${1 + .030 * A * s}, ${1 - .026 * A * s})`;
      if (baseR > .5) el.style.borderRadius = `${baseR * (1 + .30 * A * s)}px`;
      if (rec) rec.setScale(Math.max(0, base * (1 + 1.5 * A * s)));
      springs.set(el, requestAnimationFrame(step));
    };
    springs.set(el, requestAnimationFrame(step));
  }

  function splash(el, x, y) {
    const b = el.getBoundingClientRect(), d = Math.max(b.width, b.height) * 1.5;
    const s = document.createElement('span');
    s.className = 'll-splash';
    s.style.cssText = `position:absolute;border-radius:50%;pointer-events:none;z-index:1;
      width:${d}px;height:${d}px;left:${x - b.left}px;top:${y - b.top}px;
      background:radial-gradient(circle,rgba(255,255,255,.75) 0%,rgba(255,255,255,.26) 42%,transparent 70%);
      transform:translate(-50%,-50%) scale(0);`;
    const clip = el.style.overflow; el.style.overflow = 'hidden';
    el.appendChild(s);
    s.animate([{ transform: 'translate(-50%,-50%) scale(0)', opacity: .8 },
               { transform: 'translate(-50%,-50%) scale(1)', opacity: 0 }],
              { duration: 620, easing: 'cubic-bezier(.22,1,.36,1)' })
     .onfinish = () => { s.remove(); el.style.overflow = clip; };
  }

  // 悬停时轻弹一下：比点击那一下小得多（幅度 .45 对 1.2、时长 420 对 520）。
  // 鼠标扫过一排卡片时每张都按点击的力度弹，整页会像在抖。
  // 用 pointerenter 不用 pointerover——后者从子元素移进移出会反复触发，一张卡能弹十几次。
  function bindHoverJelly(el, opts) {
    if (!el || el.dataset.llHover) return;
    el.dataset.llHover = '1';
    el.style.transformOrigin = 'center center';
    el.addEventListener('pointerenter', e => {
      if (e.pointerType === 'touch') return;      // 触摸设备上 enter 和 click 会连着来，弹两次
      jelly(el, Object.assign({ amp: .45, freq: 3.2, dur: 420 }, opts || {}));
    });
  }

  function bindJelly(el, opts) {
    if (!el || el.dataset.llJelly) return;
    el.dataset.llJelly = '1';
    el.style.transformOrigin = 'center center';
    el.style.willChange = 'transform';
    el.addEventListener('pointerdown', () => {
      el.style.transition = 'transform .09s cubic-bezier(.4,0,.6,1)';
      el.style.transform = 'scale(.982)';
    });
    const up = () => { el.style.transition = ''; };
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(e => el.addEventListener(e, up));
    el.addEventListener('click', e => { up(); splash(el, e.clientX, e.clientY); jelly(el, opts); });
  }

  // ── 工作台定位线 ────────────────────────────────────────────────────────
  // 只在内容稀疏的页面铺（首页、列表页）。编辑器那种整屏被面板盖住的页面铺了
  // 也看不见，白费一层渲染，还会跟导轨刻度这类精密控件抢注意力。
  function buildDraft(d) {
    if (!d) return;
    const W = d.offsetWidth, H = d.offsetHeight, MAJ = 120;
    // 网格和交点标包在 .ll-paper 里：开了漂移时整块一起走，位移正好一个粗格 120px，
    // 线和线、交点和交点都对得上，循环处看不出接缝。交点标必须跟网格同层——
    // 网格动它不动的话，十字会一点点滑离交点。
    let h = '<div class="ll-paper"><span class="g-minor"></span><span class="g-major"></span>';
    for (let x = 0; x <= W + MAJ; x += MAJ)
      for (let y = MAJ; y < H; y += MAJ)
        h += `<span class="cross" style="left:${x - 7}px;top:${y - 7}px"></span>`;
    h += '</div>';
    let t = '';
    for (let x = 0; x < W; x += 8) {
      t += `<span class="tk${x % 40 === 0 ? ' lg' : ''}" style="left:${x}px"></span>`;
      if (x % 120 === 0 && x > 0) t += `<span class="num" style="left:${x}px">${x}</span>`;
    }
    h += `<div class="ruler">${t}</div>`;
    let v = '';
    for (let y = 90; y < H; y += 8) v += `<span class="tk${y % 40 === 0 ? ' lg' : ''}" style="top:${y}px"></span>`;
    h += `<div class="vruler">${v}</div>`;
    // 圆弧、尺寸标注、角标都搬去 .ll-drift 了：它们不是 120px 周期的，留在纸上会在
    // 每 9 秒的循环处整块跳一下。母版层循环距离 960px，大圆整个装得下，跟着一起走。
    // 这里只留尺规——工作台上的尺子本来就是固定的，动的是纸；尺规跟着动，刻度数字
    // 滑来滑去反而没法读。
    d.innerHTML = h;
  }

  // ── 漂移层 ──────────────────────────────────────────────────────────────
  // 一组制图母版横向平铺，匀速右移。母版整块画在 TILE 宽的容器里、左右各留白，
  // 铺够覆盖视口再多一块，位移走满一个 TILE 就复位——接缝处两块内容一样，看不出跳。
  // 只给首页这种过路页面：动的背景会让每一块玻璃的 backdrop-filter 逐帧重跑，
  // 实测停在首页 19.9% GPU，切走 0.9%，最小化 0%。编辑器那种十几块玻璃的页面不能开。
  const DRIFT_TILE = 960;
  function buildDrift(host) {
    const W = host.offsetWidth, H = host.offsetHeight;
    const n = Math.ceil(W / DRIFT_TILE) + 2;
    const motif = x => `
      <span class="arc" style="width:460px;height:460px;left:${x + 40}px;top:${Math.round(H * .30)}px"></span>
      <span class="cross" style="left:${x + 263}px;top:${Math.round(H * .30) + 223}px"></span>
      <span class="dim" style="left:${x + 270}px;width:230px;top:${Math.round(H * .30) + 230}px"><span class="lb">R230</span></span>
      <span class="arc" style="width:300px;height:300px;left:${x + 600}px;top:${Math.round(H * .06)}px"></span>
      <span class="cross" style="left:${x + 743}px;top:${Math.round(H * .06) + 143}px"></span>
      <span class="arc" style="width:150px;height:150px;left:${x + 675}px;top:${Math.round(H * .06) + 75}px;opacity:.55"></span>
      <span class="arc" style="width:170px;height:170px;left:${x + 640}px;top:${Math.round(H * .68)}px;opacity:.7"></span>
      <span class="cross" style="left:${x + 718}px;top:${Math.round(H * .68) + 78}px"></span>
      <span class="dim" style="left:${x + 180}px;width:300px;top:${Math.round(H * .86)}px"><span class="lb">300</span></span>
      <span class="dim" style="left:${x + 560}px;width:180px;top:${Math.round(H * .20)}px"><span class="lb">180</span></span>
      <span class="corner tr" style="left:${x + 880}px;top:${Math.round(H * .40)}px"></span>
      <span class="corner bl" style="left:${x + 520}px;top:${Math.round(H * .50)}px"></span>
      <span class="hatch" style="left:${x + 300}px;top:${Math.round(H * .08)}px"></span>
      <span class="cross" style="left:${x + 60}px;top:${Math.round(H * .80)}px"></span>
      <span class="cross" style="left:${x + 900}px;top:${Math.round(H * .74)}px"></span>`;
    let h = '';
    for (let i = 0; i < n; i++) h += motif(i * DRIFT_TILE);
    host.style.width = (n * DRIFT_TILE) + 'px';
    host.innerHTML = h;
  }

  // 页面被切走/窗口最小化就停。只认 document.hidden，不认失焦——
  // 用户点一下旁边的应用，窗口还好端端露在桌面上，动画却停住，读起来像界面卡死。
  function bindDriftPause(wrap) {
    const sync = () => wrap.classList.toggle('paused', document.hidden);
    document.addEventListener('visibilitychange', sync);
    sync();
  }

  let draftEl = null;
  let driftEl = null;
  // opts.ink：线条浓度。首页和列表页用默认的 .16；编辑器这类被面板占满的页面
  // 给一半左右，它在那里的职责是「让玻璃有边可折」，不是让人去读那些线。
  function mountBackdrop(opts) {
    if (draftEl) return draftEl;
    const wrap = document.createElement('div');
    wrap.className = 'll-bgfx';
    wrap.setAttribute('aria-hidden', 'true');
    if (opts && opts.ink != null) wrap.style.setProperty('--ink', opts.ink);
    // 两团暖色只给浏览页。工作页要中性底——一片暖色晕在面板上，
    // 容易让人以为界面进了某种状态
    const tint = !opts || opts.tint !== false
      ? '<span class="ll-tint t1"></span><span class="ll-tint t2"></span>' : '';
    // opts.drift：加一层横向漂移的制图母版。只给首页开，理由见 buildDrift 的注释
    const drift = opts && opts.drift ? '<div class="ll-drift"></div>' : '';
    wrap.innerHTML = tint + '<div class="ll-draft"></div>' + drift;
    document.body.insertBefore(wrap, document.body.firstChild);
    draftEl = wrap.querySelector('.ll-draft');
    buildDraft(draftEl);
    driftEl = wrap.querySelector('.ll-drift');
    if (driftEl) { wrap.classList.add('drift'); buildDrift(driftEl); bindDriftPause(wrap); }
    return draftEl;
  }

  // 一步到位：补 .g6d 和棱镜层、上镜片、按需绑液态
  function glass(target, opts) {
    const o = opts || {};
    const list = typeof target === 'string' ? document.querySelectorAll(target)
               : (target instanceof Element ? [target] : target || []);
    refresh();                       // 顺手回收上一批已被重绘掉的元素和它们的滤镜
    [].forEach.call(list, el => {
      if (!el) return;
      if (o.rim !== false) el.classList.add('g6d');
      // lens:false —— 只要凸面镜边，不上折射滤镜。
      // 工作面底色 88% 不透明，折射本来就被盖住看不见；但圆角那一小段圆弧上法线要转满 90°，
      // 背后的网格线在这里汇聚被径向拽开，打成一个结，反而是唯一露出来的东西。
      // 有成本没收益，所以工作面只留镜边和阴影。
      if (o.lens === false) { el.style.setProperty('--band', (o.band || 6) + 'px'); return; }
      // 棱镜扇要显式开。默认不插这个元素——见 liquid-lens.css 里 --prism 的注释
      if (o.prism === true) {
        el.style.setProperty('--prism', o.prismStrength != null ? o.prismStrength : .48);
        if (!el.querySelector(':scope > .prism')) {
          const p = document.createElement('i');
          p.className = 'prism';
          el.insertBefore(p, el.firstChild);
        }
      }
      apply(el, o);
      if (o.jelly) bindJelly(el, typeof o.jelly === 'object' ? o.jelly : undefined);
    });
  }

  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (draftEl) buildDraft(draftEl);
      if (driftEl) buildDrift(driftEl);
      refresh();
    }, 140);
  });

  window.LiquidLens = { apply, refresh, jelly, bindJelly, bindHoverJelly, glass, mountBackdrop, DEFAULTS,
                        setJellyGain: g => { gain = g; } };
})();
