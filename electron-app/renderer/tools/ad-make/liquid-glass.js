// 液态玻璃：backdrop-filter 上叠一层 SVG 位移图，做出边缘折射。
//
// 三个要点，改参数前先读：
// 1. 位移图按元素自己的像素尺寸现算。一张图拉伸复用会糊——这是这类实现最常见的毛病。
// 2. 位移图中心是平坦的，只有边缘一圈有梯度，所以玻璃中间保持通透。iOS 26 的玻璃中心
//    并不糊，模糊值越大越不像。厚度感靠 CSS 里上缘亮、下缘暗那两道 inset 高光，不靠模糊。
// 3. 玻璃背后必须有内容。背后是纯色时，像素挪来挪去还是同一个色，折射看不出任何东西。
//
// 兜底：调用方在 CSS 里先写一条不带 url() 的 backdrop-filter。这里设的行内样式如果
// 因为运行时不认 url() 而被丢弃，CSS 那条还在，元素退化成普通毛玻璃，不会塌成透明。
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const DEFAULTS = { radius: 14, band: 12, scale: 30, blur: 3, sat: 135, bright: 108 };

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

  // 圆角矩形的有符号距离场 + smoothstep：边缘一圈是 1，往中心平滑降到 0。
  // 两个方向各自归一化——按 max(w,h) 统一归一化的话，工具条那种宽扁元素纵向位移会被压没，
  // 上下边缘看不出折射，而横条最该有折射的恰恰是下边缘。
  function buildMap(w, h, radius, band) {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d'), im = g.createImageData(w, h);
    const ss = (a, b, t) => { t = Math.max(0, Math.min(1, (t - a) / (b - a))); return t * t * (3 - 2 * t); };
    const sdf = (x, y, bw, bh, rr) => {
      const qx = Math.abs(x) - bw + rr, qy = Math.abs(y) - bh + rr;
      return Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - rr;
    };
    const cl = v => Math.round(Math.max(0, Math.min(255, v)));
    const bn = Math.min(band / w, band / h);
    const rr = radius / Math.min(w, h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const ix = x / w - .5, iy = y / h - .5;
        const d = sdf(ix, iy, .5 - bn * .5, .5 - bn * .5, rr);
        const sc = ss(0, 1, ss(bn, 0, d));
        const i = (y * w + x) * 4;
        im.data[i]     = cl((Math.max(-1, Math.min(1, -ix * 2 * sc)) * .5 + .5) * 255);
        im.data[i + 1] = cl((Math.max(-1, Math.min(1, -iy * 2 * sc)) * .5 + .5) * 255);
        im.data[i + 2] = 128;
        im.data[i + 3] = 255;
      }
    }
    g.putImageData(im, 0, 0);
    return c.toDataURL();
  }

  function ensureFilter(rec) {
    if (rec.id) return;
    rec.id = 'lg-lens-' + (++seq);
    const f = document.createElementNS(NS, 'filter');
    f.setAttribute('id', rec.id);
    f.setAttribute('x', '0%'); f.setAttribute('y', '0%');
    f.setAttribute('width', '100%'); f.setAttribute('height', '100%');
    f.setAttribute('color-interpolation-filters', 'sRGB');
    rec.img = document.createElementNS(NS, 'feImage');
    rec.img.setAttribute('result', 'm');
    rec.img.setAttribute('preserveAspectRatio', 'none');
    rec.dm = document.createElementNS(NS, 'feDisplacementMap');
    rec.dm.setAttribute('in', 'SourceGraphic');
    rec.dm.setAttribute('in2', 'm');
    rec.dm.setAttribute('xChannelSelector', 'R');
    rec.dm.setAttribute('yChannelSelector', 'G');
    f.appendChild(rec.img); f.appendChild(rec.dm);
    ensureDefs().appendChild(f);
  }

  function paint(rec) {
    const el = rec.el, o = rec.o;
    const w = Math.round(el.offsetWidth), h = Math.round(el.offsetHeight);
    if (!w || !h) return;                 // 隐藏时不画，等下次 refresh
    // 边缘环最多吃掉短边的三分之一。再宽整块都在变形，上面的字会糊。
    const band = Math.min(o.band, Math.min(w, h) / 3);
    const key = w + 'x' + h + 'x' + band;
    ensureFilter(rec);
    if (rec.key !== key) {                // 只在尺寸变化时重算位移图，滚动和切换都不重算
      rec.key = key;
      rec.img.setAttribute('href', buildMap(w, h, o.radius, band));
      rec.img.setAttribute('width', w);
      rec.img.setAttribute('height', h);
    }
    rec.dm.setAttribute('scale', o.scale);
    el.style.backdropFilter =
      'url(#' + rec.id + ') blur(' + o.blur + 'px) saturate(' + o.sat + '%) brightness(' + o.bright + '%)';
  }

  function apply(el, opts) {
    if (!el) return null;
    let rec = items.find(r => r.el === el);
    if (rec) { Object.assign(rec.o, opts || {}); rec.key = null; }
    else { rec = { el: el, o: Object.assign({}, DEFAULTS, opts || {}) }; items.push(rec); }
    paint(rec);
    return rec;
  }

  function refresh() {
    for (let i = items.length - 1; i >= 0; i--) {
      if (!items[i].el.isConnected) { items.splice(i, 1); continue; }
      paint(items[i]);
    }
  }

  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(refresh, 120);
  });

  window.LiquidGlass = { apply: apply, refresh: refresh, DEFAULTS: DEFAULTS };
})();
