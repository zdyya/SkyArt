'use strict';

/**
 * 吸附到最近的片段边界（snapPx 像素内精确对齐）
 * @param {number} pct - 当前位置 0–100
 * @param {number} trackWidth - 轨道像素宽度（不含 padding）
 * @param {Array<{start:number, end:number}>} clips
 * @param {number} [snapPx=5]
 */
function snapToBoundary(pct, trackWidth, clips, snapPx = 5) {
  if (!clips || !clips.length || trackWidth <= 0) return pct;
  const thresh = snapPx / trackWidth * 100;
  let best = null, bestDist = thresh;
  for (const c of clips) {
    const ds = Math.abs(pct - c.start), de = Math.abs(pct - c.end);
    if (ds < bestDist) { bestDist = ds; best = c.start; }
    if (de < bestDist) { bestDist = de; best = c.end;   }
  }
  return best !== null ? best : pct;
}

/**
 * 创建视频播放器实例（单文件模式 / 多文件模式通用）
 *
 * DOM IDs（必填）
 *   videoId, playBtnId, progressFillId, currentTimeId, totalTimeId
 *   playheadId, trackClipsId, timelineClipsId, rulerId
 *   placeholderId  (可选)
 *
 * 数据 / 渲染
 *   fmtTime(sec)             将秒数格式化为字符串（用于刻度和时间显示）
 *   getClips()               返回 [{id, start, end, label?, color?}]（百分比坐标）
 *   getSelectedIds()         返回 Set<id>，用于高亮选中片段（可选）
 *   clipIdPrefix             片段元素 ID 前缀，默认 'tc'
 *   onClipClickFn            片段 onclick/oncontextmenu 的函数名字符串
 *   renderClipLabel(c, i)    自定义片段文字（可选）
 *   renderClipTitle(c, i)    自定义 tooltip（可选）
 *
 * 时间轴行为
 *   getSnapBoundaries()      吸附边界来源，默认 getClips()
 *   seekVideo(pct)           覆盖默认 seek 逻辑（多文件模式必填）
 *   onSeek(pct)              seek 完成后的回调
 *
 * 键盘微调
 *   activateSelector         点击该区域后激活键盘（CSS 选择器）
 *   keepActiveSelector       点击该区域时保持激活（不失活），可选
 *   onKeyboardSeek(key)      ← / → 键处理（'ArrowLeft' | 'ArrowRight'）
 *
 * video 事件
 *   onMetaLoaded(dur)        loadedmetadata 回调（工厂已完成刻度 + 总时长）
 *   onTimeUpdate()           完全覆盖 timeupdate 逻辑（多文件模式用）
 *   onEnded()                完全覆盖 ended 逻辑
 *
 * @returns {{ loadVideo, renderRuler, renderClips, togglePlay, updateProgress,
 *             seekFromBar, seekFromTimeline, setActive }}
 */
function createVideoPlayer(cfg) {
  const $ = id => document.getElementById(id);
  const PREFIX = cfg.clipIdPrefix ?? 'tc';

  let _mockInterval = null;
  let _tlActive = false;

  const _v = () => $(cfg.videoId);

  function _playBtn(playing) {
    const btn = $(cfg.playBtnId);
    if (!btn) return;
    btn.style.background = playing ? 'rgba(239,68,68,0.8)' : 'rgba(249,115,22,0.85)';
    btn.innerHTML = playing
      ? '<div style="width:14px;height:14px;border-left:4px solid #fff;border-right:4px solid #fff;margin:0 2px"></div>'
      : '<div class="tri"></div>';
  }

  // ── 公开方法 ──────────────────────────────────────────────────────────────────

  function loadVideo(url) {
    const v = _v(), ph = $(cfg.placeholderId), btn = $(cfg.playBtnId);
    if (!v) return;
    if (url) {
      v.src = url; v.style.display = 'block';
      if (ph)  ph.style.display  = 'none';
      if (btn) btn.style.display = 'flex';
    } else {
      v.src = ''; v.style.display = 'none';
      if (ph)  ph.style.display  = 'flex';
      if (btn) btn.style.display = 'none';
    }
  }

  function renderRuler(durationSec) {
    const ruler = $(cfg.rulerId);
    if (!ruler || !(durationSec > 0)) return;
    let step = durationSec <= 30 ? 5 : durationSec <= 90 ? 10
             : durationSec <= 300 ? 30 : durationSec <= 900 ? 60 : 120;
    const labels = [];
    for (let t = 0; t <= durationSec; t += step)
      labels.push(`${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`);
    const tail = `${Math.floor(durationSec / 60)}:${String(Math.floor(durationSec % 60)).padStart(2, '0')}`;
    if (labels[labels.length - 1] !== tail) labels.push(tail);
    ruler.innerHTML = labels.map(l => `<span>${l}</span>`).join('');
  }

  function renderClips() {
    const container = $(cfg.timelineClipsId);
    if (!container) return;
    const clips = cfg.getClips?.() ?? [];
    const sel   = cfg.getSelectedIds?.() ?? new Set();
    container.innerHTML = clips.map((c, i) => {
      const w   = c.end - c.start;
      const col = c.color ?? ((i % 6) + 1);
      const label = cfg.renderClipLabel?.(c, i) ?? `分镜 ${i + 1}`;
      const title = cfg.renderClipTitle?.(c, i) ?? label;
      const fn    = cfg.onClipClickFn ?? '';
      return `<div class="timeline-clip timeline-clip-${col}${sel.has(c.id) ? ' selected' : ''}"
        id="${PREFIX}-${c.id}" style="width:${Math.max(w, 0.5)}%;flex-shrink:0"
        ${fn ? `onclick="${fn}(${c.id},event)" oncontextmenu="${fn}(${c.id},event)"` : ''}
        title="${title}">${label}</div>`;
    }).join('');
  }

  function updateProgress(pct) {
    const fill  = $(cfg.progressFillId);
    const ph    = $(cfg.playheadId);
    const track = $(cfg.trackClipsId);
    if (fill) fill.style.width = pct + '%';
    if (ph && track) ph.style.left = (68 + pct / 100 * (track.offsetWidth - 16)) + 'px';
  }

  function togglePlay() {
    const v = _v();
    if (v?.src) {
      if (v.paused) { v.play();  _playBtn(true);  }
      else          { v.pause(); _playBtn(false); }
    } else {
      // 无视频时的 mock 播放（进度条动画）
      if (_mockInterval) {
        clearInterval(_mockInterval); _mockInterval = null; _playBtn(false);
      } else {
        let pct = 0;
        _playBtn(true);
        _mockInterval = setInterval(() => {
          pct = Math.min(pct + 0.4, 100);
          updateProgress(pct);
          if (pct >= 100) { clearInterval(_mockInterval); _mockInterval = null; _playBtn(false); }
        }, 50);
      }
    }
  }

  function seekFromBar(e) {
    const bar = e.currentTarget ?? e.target.closest?.('.video-progress-bar');
    if (!bar) return;
    const rect = bar.getBoundingClientRect();
    const pct  = Math.max(0, Math.min(100, (e.clientX - rect.left) / rect.width * 100));
    const v = _v();
    if (v?.duration) v.currentTime = v.duration * pct / 100;
    else updateProgress(pct);
    cfg.onSeek?.(pct);
  }

  function seekFromTimeline(e) {
    if (e.button !== undefined && e.button !== 0) return;
    if (e.ctrlKey || e.metaKey) return;
    const track = e.currentTarget ?? $(cfg.trackClipsId);
    if (!track) return;
    const rect   = track.getBoundingClientRect();
    const PAD    = 8, inner = rect.width - PAD * 2;
    const rawPct = Math.max(0, Math.min(100, (e.clientX - rect.left - PAD) / inner * 100));
    const snap   = cfg.getSnapBoundaries?.() ?? cfg.getClips?.() ?? [];
    const pct    = snapToBoundary(rawPct, inner, snap);
    const ph = $(cfg.playheadId);
    if (ph) ph.style.left = (68 + pct / 100 * inner) + 'px';
    if (cfg.seekVideo) {
      cfg.seekVideo(pct);
    } else {
      const v = _v();
      if (v?.duration) v.currentTime = v.duration * pct / 100;
      else updateProgress(pct);
    }
    cfg.onSeek?.(pct);
    _tlActive = true;
  }

  function setActive(val) { _tlActive = !!val; }

  // ── 拖拽播放头 ────────────────────────────────────────────────────────────────
  let _dragging = false;

  document.addEventListener('mousedown', e => {
    if (e.target?.id === cfg.playheadId) { _dragging = true; e.preventDefault(); }
  });

  document.addEventListener('mousemove', e => {
    if (!_dragging) return;
    const track = $(cfg.trackClipsId);
    if (!track) return;
    const rect   = track.getBoundingClientRect();
    const PAD    = 8, inner = rect.width - PAD * 2;
    const rawPct = Math.max(0, Math.min(100, (e.clientX - rect.left - PAD) / inner * 100));
    const snap   = cfg.getSnapBoundaries?.() ?? cfg.getClips?.() ?? [];
    const pct    = snapToBoundary(rawPct, inner, snap);
    const ph = $(cfg.playheadId);
    if (ph) ph.style.left = (68 + pct / 100 * inner) + 'px';
    if (cfg.seekVideo) {
      cfg.seekVideo(pct);
    } else {
      updateProgress(pct);
      const v = _v();
      if (v?.duration) v.currentTime = v.duration * pct / 100;
    }
    cfg.onSeek?.(pct);
  });

  document.addEventListener('mouseup', () => { _dragging = false; });

  // ── 键盘微调 ──────────────────────────────────────────────────────────────────
  if (cfg.activateSelector || cfg.onKeyboardSeek) {
    document.addEventListener('mousedown', e => {
      if (cfg.activateSelector && e.target.closest?.(cfg.activateSelector)) {
        _tlActive = true;
      } else if (!cfg.keepActiveSelector || !e.target.closest?.(cfg.keepActiveSelector)) {
        _tlActive = false;
      }
    });

    document.addEventListener('keydown', e => {
      if (!_tlActive) return;
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const tag = document.activeElement?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      e.preventDefault();
      cfg.onKeyboardSeek?.(e.key);
    });
  }

  // ── video 事件（直接绑定到元素）────────────────────────────────────────────────
  const v = _v();
  if (v) {
    v.addEventListener('loadedmetadata', () => {
      const dur = v.duration || 0;
      const tt = $(cfg.totalTimeId);
      if (tt && cfg.fmtTime) tt.textContent = cfg.fmtTime(dur);
      renderRuler(dur);
      cfg.onMetaLoaded?.(dur);
    });

    v.addEventListener('timeupdate', () => {
      if (cfg.onTimeUpdate) { cfg.onTimeUpdate(); return; }
      if (!v.duration) return;
      const pct = (v.currentTime / v.duration) * 100;
      updateProgress(pct);
      const ct = $(cfg.currentTimeId);
      if (ct && cfg.fmtTime) ct.textContent = cfg.fmtTime(v.currentTime);
    });

    v.addEventListener('ended', () => {
      if (cfg.onEnded) { cfg.onEnded(); return; }
      _playBtn(false);
    });
  }

  return { loadVideo, renderRuler, renderClips, togglePlay, updateProgress, seekFromBar, seekFromTimeline, setActive };
}
