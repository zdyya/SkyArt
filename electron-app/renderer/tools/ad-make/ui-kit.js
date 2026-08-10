// 分段控件（选中态规范里的 A 类）：凹槽 + 浮起滑块，切换时滑过去。
//
// 滑块是一块绝对定位的白底，位置和尺寸由 JS 按选中项量出来——纯 CSS 做不到
// 在宽度各不相同的选项之间平滑滑动。用法：容器加 data-seg，里面放一个
// <span class="seg-thumb"></span>，选中项带 .on / .active，或者 label 里的 radio 被选中。
//
// 点击后用 rAF 重排，是为了让页面自己的 onclick 先把类改完——两边顺序反了滑块会停在旧位置。
(function () {
  const STATES = ['.on', '.active', 'label:has(input:checked)'];

  // 滑块本体的样式放这儿，不放各页面的 <style>：三个页面各抄一份迟早会走样。
  // 页面只管凹槽和选项文字。
  const CSS = `
.seg-thumb{position:absolute;top:0;left:0;z-index:0;border-radius:6px;pointer-events:none;
  background:var(--bg2,#fff);
  box-shadow:0 1px 3px rgba(20,24,30,.2),0 0 0 .5px rgba(20,24,30,.05),inset 0 1px 0 #fff;
  transition:transform .28s cubic-bezier(.22,1,.36,1),
             width .28s cubic-bezier(.22,1,.36,1),
             height .28s cubic-bezier(.22,1,.36,1),
             opacity .18s ease;}`;
  const st = document.createElement('style');
  st.textContent = CSS;
  (document.head || document.documentElement).appendChild(st);

  function selected(seg) {
    for (const s of STATES) {
      const el = seg.querySelector(':scope > ' + s);
      if (el) return el;
    }
    return null;
  }

  function layout(seg) {
    const thumb = seg.querySelector(':scope > .seg-thumb');
    if (!thumb) return;
    const on = selected(seg);
    if (!on || !on.offsetWidth) { thumb.style.opacity = '0'; return; }
    thumb.style.opacity = '1';
    thumb.style.width = on.offsetWidth + 'px';
    thumb.style.height = on.offsetHeight + 'px';
    thumb.style.transform = 'translate(' + on.offsetLeft + 'px,' + on.offsetTop + 'px)';
  }

  function layoutAll(root) {
    (root || document).querySelectorAll('[data-seg]').forEach(layout);
  }

  const ro = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(es => es.forEach(e => layout(e.target)))
    : null;

  // 选中项不是只有点击才会变：页面异步拉回数据后自己 toggle 一遍 .on 是常事。
  // 只靠点击事件重排的话，那一次改动没人管，滑块停在初始位置——初始位置往往是
  // 「一个都没选中」（数据还没回来），于是滑块 opacity 归零，看上去就是整条控件空着。
  const mo = typeof MutationObserver !== 'undefined'
    ? new MutationObserver(ms => {
        const segs = new Set();
        ms.forEach(m => { const s = m.target.closest && m.target.closest('[data-seg]'); if (s) segs.add(s); });
        segs.forEach(layout);
      })
    : null;

  function bind(seg) {
    if (seg._segBound) return;
    seg._segBound = true;
    seg.addEventListener('click', () => requestAnimationFrame(() => layout(seg)));
    if (ro) ro.observe(seg);
    if (mo) mo.observe(seg, { subtree: true, childList: true,
                              attributes: true, attributeFilter: ['class', 'checked'] });
    layout(seg);
  }

  function init(root) {
    (root || document).querySelectorAll('[data-seg]').forEach(bind);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => init());
  else init();
  // 字体加载完字宽会变，滑块要重量一次
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => layoutAll());
  window.addEventListener('resize', () => layoutAll());

  window.SkyUI = { initSegs: init, layoutSegs: layoutAll, layoutSeg: layout };
})();
