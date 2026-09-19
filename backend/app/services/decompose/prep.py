#!/usr/bin/env python3
"""短剧广告素材拆解：本地预处理。

VLM 只回答看画面能回答的问题，时间边界和节奏指标一律本地算。

用法：
  prep.py sub     <in.mp4> [裁剪区]             OCR 硬字幕，出字幕 TSV
  prep.py detect  <in.mp4>                      检出黑场和镜头切换
  prep.py round1  <in.mp4> <sub.tsv>            出第一轮输入（切段）
  prep.py round2  <in.mp4> <sub.tsv> <segs.json>  出第二轮输入（按段切片）
  prep.py round3  <in.mp4>                      出第三轮输入（整条素材）
  prep.py post1   <work_dir>                    第一步跑完，补第一步撑得起的字段
  prep.py stage2  <work_dir>                    组装第二步（文本推理）的输入
  prep.py post2   <work_dir>                    第二步跑完，补跨段派生字段
"""
import json, os, re, subprocess, sys

# 本机 ffmpeg 总数闸门。这几个模块也支持当脚本单跑（文件末尾有 __main__），
# 那时候拿不到 app 包，就退回一个什么都不做的闸门，单跑本来也不存在抢资源的问题。
try:
    from app.services import ffmpeg_gate as _闸门
except Exception:                                    # pragma: no cover
    class _闸门:                                      # type: ignore
        class 排队:
            def __enter__(self): return self
            def __exit__(self, *e): return False
from .环境 import 拆解失败, ocr程序

# 输出改成行缓冲。管道里不是 tty 时 Python 默认块缓冲，攒到 8KB 才写盘，
# 长任务的进度要等几分钟才看得到，只能拿 lsof 猜它在干什么。
sys.stdout.reconfigure(line_buffering=True)

BLACK_YAVG = 12.0      # 亮度均值低于这个数算黑场
SCENE_THRESH = 0.3     # ffmpeg scene 分数阈值
LONG_SHOT = 6.0        # 超过这个秒数算长镜头
SEG_MAX = 120.0        # 单段上限，超了在第一轮再切


def sh(cmd):
    if "ffmpeg" in cmd or "ffprobe" in cmd:
        # 本机 ffmpeg 总数有上限，忙的时候在这里等
        with _闸门.排队():
            return _sh_真跑(cmd)
    return _sh_真跑(cmd)


def _sh_真跑(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def err(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stderr


def 全长(mp4):
    return float(sh(f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{mp4}"').strip())


def 片尾板(mp4):
    """这一集结尾那段定格或缓放的片尾板有多长，检不到是 0。

    短剧每集结尾都接一段片尾板。线上在上传时就检出来存进 tail_trim，合 Part
    的时候逐集截掉；拆解这一侧原来没有这一步，末段一路切到文件结尾，片尾板
    被当成正片描述，按模板剪出来的成片也会在段与段之间夹一张定格画面。

    判据用的是线上那份 transition_detect.py，原样搬过来，阈值没改。
    检测结果按集缓存，重跑不重算；想手改就编 _片尾板.json 里那个数。
    """
    wd = workdir(mp4)
    f = os.path.join(wd, '_片尾板.json')
    if os.path.exists(f):
        try:
            return float(json.load(open(f, encoding='utf-8'))['片尾板'])
        except (ValueError, KeyError, json.JSONDecodeError):
            pass
    d = 全长(mp4)
    try:
        from .transition_detect import detect_transitions
        tail = float(detect_transitions(mp4, d)[1])
    except Exception as e:
        # 检不出来就按不截算。少截一段片尾板只是多描述两秒定格，
        # 多截会吃掉正片结尾，所以出错一律偏向不截。
        print(f'    片尾板没检出来，按整条算（{type(e).__name__}）')
        tail = 0.0
    json.dump({'全长': round(d, 3), '片尾板': round(tail, 2)},
              open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if tail:
        print(f'    片尾板 {tail:.1f}s，这一集分析到第 {d - tail:.1f} 秒为止')
    return tail


def duration(mp4):
    """有效时长：全长减掉片尾板。切段、抽帧、算密度一律按这个数。"""
    return 全长(mp4) - 片尾板(mp4)


def detect_black(mp4, fps=2):
    """检出段边界候选，返回（强黑场, 暗帧候选）。

    两个信号：
    - blackdetect 找真黑场。持续 0.2 秒以上的算强候选，直接当段边界。
      更短的是场景内的暗帧闪动，不算。
    - 亮度均值的局部极小值。有些转场是渐变穿过暗场，不到全黑，
      blackdetect 抓不到，这类只作候选，由第一轮的模型判断是不是边界。

    metadata=print:file=- 写的是标准输出，不是标准错误；pts_time 的单位
    已经是秒，不要再除帧率。
    """
    out = sh(f'ffmpeg -i "{mp4}" -vf "blackdetect=d=0.05:pix_th=0.10:pic_th=0.90" '
             f'-an -f null - 2>&1')
    strong = []
    for m in re.finditer(r'black_start:([\d.]+) black_end:([\d.]+)', out):
        a, b = float(m.group(1)), float(m.group(2))
        if b - a >= 0.2:
            strong.append(round((a + b) / 2, 1))

    out2 = sh(f'ffmpeg -i "{mp4}" -vf "fps={fps},scale=64:114,signalstats,'
              f'metadata=print:key=lavfi.signalstats.YAVG:file=-" -f null - 2>/dev/null')
    frames, cur = [], None
    for line in out2.splitlines():
        m = re.search(r'pts_time:([\d.]+)', line)
        if m:
            cur = float(m.group(1))
        elif 'YAVG=' in line and cur is not None:
            frames.append((cur, float(line.split('YAVG=')[1].strip())))
            cur = None
    ys = sorted(y for _, y in frames)
    med = ys[len(ys) // 2] if ys else 0
    th = med * 0.35
    dim, run = [], []
    for t, y in frames:
        if y < th:
            run.append((t, y))
        elif run:
            dim.append(round(min(run, key=lambda r: r[1])[0], 1)); run = []
    if run:
        dim.append(round(min(run, key=lambda r: r[1])[0], 1))
    dim = [t for t in dim if all(abs(t - s) > 2.0 for s in strong)]
    return strong, dim


def detect_shots(mp4):
    """返回镜头切换秒数列表。"""
    out = sh(f'ffmpeg -i "{mp4}" -vf "select=\'gt(scene,{SCENE_THRESH})\','
             f'metadata=print:file=-" -f null - 2>/dev/null')
    return [round(float(m), 1) for m in re.findall(r'pts_time:([\d.]+)', out)]


def load_sub(path):
    """字幕 TSV：起<TAB>止<TAB>台词，单位秒。"""
    rows = []
    for line in open(path, encoding='utf-8', errors='replace'):
        p = line.rstrip('\n').split('\t')
        if len(p) >= 3 and p[0].strip().isdigit():
            rows.append((float(p[0]), float(p[1]), p[2].strip()))
    return rows


def fmt_sub(rows, offset=0.0):
    return '\n'.join(f'{r[0]-offset:.0f}-{r[1]-offset:.0f}\t{r[2]}' for r in rows)


def grab(mp4, secs, outdir, w=360):
    """按秒数抽帧，文件名就是秒数。返回 [(秒, 文件名)]。"""
    os.makedirs(outdir, exist_ok=True)
    got = []
    for t in secs:
        name = f't{t:07.1f}.jpg'
        sh(f'ffmpeg -y -ss {t} -i "{mp4}" -frames:v 1 -vf scale={w}:-2 '
           f'"{os.path.join(outdir, name)}" 2>/dev/null')
        if os.path.exists(os.path.join(outdir, name)):
            got.append((t, name))
    return got


def clip(mp4, a, b, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # crf 20 而不是 28：给模型看的画面先压一遍，硬字幕和表情细节都会掉。
    sh(f'ffmpeg -y -ss {a} -to {b} -i "{mp4}" -c:v libx264 -crf 20 '
       f'-preset veryfast -c:a aac -b:a 96k "{out}" 2>/dev/null')


def workdir(mp4):
    d = os.path.join(os.path.dirname(os.path.abspath(mp4)),
                     os.path.splitext(os.path.basename(mp4))[0] + '_拆解')
    os.makedirs(d, exist_ok=True)
    return d


# ---------- sub ----------

def cmd_sub(mp4, crop=None):
    """OCR 烧在画面上的硬字幕，出字幕 TSV。

    素材没有字幕文件，字全烧在画面里。用 macOS 的 Vision 框架逐帧认，
    再把连续相同的合并成条目。

    裁剪区不写死。实测 30 条素材的字幕带从画面高度的 60% 到 88% 不等，
    还有两条是 1080×1920，固定像素裁剪会把剧名浮层认成字幕反复记下来。
    所以先用 probe.py 找出这条素材的字幕带——剧名、角标、结尾引导语在
    整条素材里不变，台词一直在换，靠这个区分。
    """
    d, wd = duration(mp4), workdir(mp4)
    if crop is None:
        from . import probe as _probe
        crop = _probe.crop(mp4)
        if crop is None:
            print('没找到台词字幕带，出一个空字幕文件')
            open(os.path.join(wd, 'sub.tsv'), 'w').close()
            return
        print(f'字幕带裁剪区 {crop}')
    binp = ocr程序()
    if not os.path.exists(binp):
        src = os.path.join(os.path.dirname(binp), 'ocr.swift')
        print('先编译 OCR 工具…')
        sh(f'swiftc -O -o "{binp}" "{src}"')
        if not os.path.exists(binp):
            print('编译失败。需要 Xcode 命令行工具：xcode-select --install')
            return
    fd = os.path.join(wd, 'sub_frames')
    os.makedirs(fd, exist_ok=True)
    print(f'抽帧（每秒一帧，裁 {crop}）…')
    sh(f'ffmpeg -y -i "{mp4}" -vf "fps=1,crop={crop}" "{fd}/%05d.jpg" 2>/dev/null')
    n = len(os.listdir(fd))
    print(f'{n} 帧，开始识别…')
    raw = sh(f'"{binp}" "{fd}"/*.jpg')
    # ocr.swift 一行是「文件名 上边界 下边界 文本」四列，文本在最后一列。
    # 一帧里的两行字幕会输出两行，按帧号并起来，否则同一帧出现两次，
    # 下面合并连续相同的那一步会算出「起大于止」。
    per_frame = {}
    for ln in raw.splitlines():
        pt = ln.split('\t')
        m = re.search(r'(\d+)\.jpg$', pt[0]) if pt else None
        if not m:
            continue
        t = int(m.group(1)) - 1
        txt = pt[3].strip() if len(pt) > 3 else ''
        per_frame.setdefault(t, [])
        if txt:
            per_frame[t].append(txt)
    lines = [(t, ' '.join(per_frame[t])) for t in sorted(per_frame)]
    rows, cur, start = [], None, 0
    for t, txt in lines + [(len(lines), '\x00')]:
        if txt != cur:
            if cur:
                rows.append((start, t - 1, cur))
            cur, start = txt, t
    out = os.path.join(wd, 'sub.tsv')
    with open(out, 'w', encoding='utf-8') as f:
        for a, b, t in rows:
            f.write(f'{a}\t{b}\t{t}\n')
    print(f'{len(rows)} 条字幕 → {out}')
    print('抽查几条对不对得上，裁剪区不对会认出一堆碎字：')
    for a, b, t in rows[:5]:
        print(f'  {a}-{b}\t{t}')


# ---------- detect ----------

def cmd_detect(mp4):
    d, wd = duration(mp4), workdir(mp4)
    strong, dim = detect_black(mp4)
    shots = detect_shots(mp4)
    bounds = [0.0] + shots + [d]
    lens = [bounds[i+1] - bounds[i] for i in range(len(bounds)-1)]
    longs = [l for l in lens if l >= LONG_SHOT]
    tail = [l for a, l in zip(bounds, lens) if a >= d - 30]
    info = {
        '素材时长': round(d, 1),
        '强黑场': strong,
        '强黑场处数': len(strong),
        '暗帧候选': dim,
        '镜头切换': shots,
        '镜头数': len(lens),
        '平均镜头长度': round(d / max(len(lens), 1), 2),
        '长镜头数': len(longs),
        '长镜头占比': round(sum(longs) / d, 3),
        '结尾节奏比': round((sum(tail)/len(tail)) / (d/max(len(lens),1)), 2) if tail else None,
    }
    json.dump(info, open(os.path.join(wd, 'detect.json'), 'w'),
              ensure_ascii=False, indent=2)
    brief = {k: v for k, v in info.items() if k != '镜头切换'}
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    print(f'\n→ {wd}/detect.json')
    return info


# ---------- round 1 ----------

def cmd_round1(mp4, sub):
    d, wd = duration(mp4), workdir(mp4)
    info = json.load(open(os.path.join(wd, 'detect.json'))) if \
        os.path.exists(os.path.join(wd, 'detect.json')) else cmd_detect(mp4)
    fades = info['强黑场']
    dim = info.get('暗帧候选', [])
    # 抽帧上限 85 张：黑场前后帧优先保留，均匀扫的间隔按时长反算。
    # 写死 15/20 秒两档在 30 分钟以上会破上限（40 分钟出 120 张）。
    step = max(15.0, d / max(85 - 2 * len(fades), 10))
    secs = sorted(set([round(t, 1) for t in frange(0, d, step)] +
                      [round(max(0, f - 1.5), 1) for f in fades] +
                      [round(min(d - 0.1, f + 1.5), 1) for f in fades] +
                      [round(t, 1) for t in dim]))
    got = grab(mp4, secs, os.path.join(wd, 'round1_frames'))
    if len(got) > 85:
        print(f'注意：抽出 {len(got)} 张，超过 85 张上限。黑场太多，'
              f'均匀扫的部分已经压到 {step:.0f} 秒一帧。')
    manifest = '\n'.join(f'第 {t:.1f} 秒：{n}' for t, n in got)
    txt = f"""【素材时长】{d:.1f} 秒

【本地检出的黑场】{'、'.join(f'{f:.1f}' for f in fades) if fades else '无，这条素材没有黑场'}

【暗帧候选】{'、'.join(f'{t:.1f}' for t in dim) if dim else '无'}
这些时刻画面明显变暗但没有全黑，可能是渐变转场，也可能只是剧情里的暗光镜头。
逐个判断它是不是段边界，是就切，不是就跳过，两种都要在边界依据里说明。

【截图清单】共 {len(got)} 张，每张前面的文字标明它是第几秒。按顺序对应下面上传的图片。
{manifest}

【字幕转写】格式为「起-止（秒）＋台词」
{fmt_sub(load_sub(sub))}
"""
    p = os.path.join(wd, 'round1_input.txt')
    open(p, 'w', encoding='utf-8').write(txt)
    print(f'第一轮：{len(got)} 张帧（均匀扫 {step:.0f} 秒一帧）'
          f' + 字幕 {len(load_sub(sub))} 条')
    print(f'→ {p}\n→ {wd}/round1_frames/')


def frange(a, b, s):
    t = a
    while t < b:
        yield t
        t += s


# ---------- round 2 ----------

MIN_SEG = 20.0


def clean_starts(raw, dur):
    """模型报的起点不能直接用，先清一遍。

    实测过 top2：规则里「补场景断裂」和「超 120 秒再切」会对同一处边界各报
    一次，撞在同一秒就出 0 秒段，ffmpeg 切不出片子，第二轮静默跳过。另外
    模型会切得过碎，18 段里 10 段短于 15 秒、最短 2 秒——那种段填不出人物
    和剧情叙述，只是噪声。所以这里去重加并短段，不指望提示词一定管住。

    并短段之后段可能反而超上限（top2 并掉 10.5 秒那个边界后首段变 126 秒），
    所以最后再把丢掉的边界挑回来切开。宁可留一个略短的段，不要留超长段。

    MIN_SEG 和 SEG_MAX 这两个常数是拿 top1、top2 标的，两条都在 500 秒
    左右。第二个返回值记下它们实际起了多少作用，24 条跑完看分布再定要不
    要改，现在不猜。
    """
    cand = sorted(t for t in {round(x, 1) for x in raw} if 0 < t < dur)
    keep, dropped = [0.0], []
    for t in cand:
        if t - keep[-1] < MIN_SEG:
            dropped.append(t)
        else:
            keep.append(t)
    if len(keep) > 1 and dur - keep[-1] < MIN_SEG:
        dropped.append(keep.pop())

    picked = 0
    changed = True
    while changed:
        changed = False
        bounds = keep + [dur]
        for i in range(len(bounds) - 1):
            if bounds[i + 1] - bounds[i] <= SEG_MAX:
                continue
            mid = (bounds[i] + bounds[i + 1]) / 2
            inner = [t for t in dropped if bounds[i] < t < bounds[i + 1]]
            if not inner:
                break        # 没有可用的边界，只能留超长段
            pick = min(inner, key=lambda t: abs(t - mid))
            keep = sorted(keep + [pick])
            dropped.remove(pick)
            picked += 1
            changed = True
            break

    bounds = keep + [dur]
    hit = {
        '模型报的起点数': len(raw),
        '去重掉的': len(raw) - len(cand),
        '因为短于 MIN_SEG 被并掉的': len(dropped),
        '因为超过 SEG_MAX 被挑回来的': picked,
        'MIN_SEG': MIN_SEG,
        'SEG_MAX': SEG_MAX,
        '最终段数': len(bounds) - 1,
        '仍然超过 SEG_MAX 的段': [
            [round(bounds[i], 1), round(bounds[i + 1], 1)]
            for i in range(len(bounds) - 1)
            if bounds[i + 1] - bounds[i] > SEG_MAX],
        '最短段': round(min(bounds[i + 1] - bounds[i]
                            for i in range(len(bounds) - 1)), 1),
        '最长段': round(max(bounds[i + 1] - bounds[i]
                            for i in range(len(bounds) - 1)), 1),
    }
    if len(keep) != len(cand) + 1:
        print(f'起点清理：模型报 {len(raw)} 个，去重并短段后留 {len(keep)} 个')
    if hit['仍然超过 SEG_MAX 的段']:
        print(f"  注意：{len(hit['仍然超过 SEG_MAX 的段'])} 段超过 {SEG_MAX} 秒，"
              '没有可用的内部边界')
    return bounds, hit


def cmd_local_cut(mp4, sub):
    """不问模型，用本机数据切段（默认走这里，LOCAL_CUT=0 改回问模型）。

    模型切段每 15 秒看一帧，三遍之间只有黑场那几处一致，其余边界每遍都不同。
    这里的规则：强黑场一律是边界；黑场之间的块超过上限时，在 25 到 70 秒的范围里
    挑一个落在字幕空档的镜头切点，离 45 秒最近的优先；没有落在空档的就挑最近的切点。
    起点列表交给 cmd_round2 的 clean_starts 做同样的去重并短段。
    """
    d, wd = duration(mp4), workdir(mp4)
    det = json.load(open(os.path.join(wd, 'detect.json')))
    cuts = sorted(float(c) for c in det.get('镜头切换') or [])
    fades = sorted(float(c) for c in det.get('强黑场') or [])
    rows = load_sub(sub)
    目标, 下限, 上限 = 45.0, 25.0, 70.0

    def 在空档(t):
        return not any(r[0] - 0.3 <= t <= r[1] + 0.3 for r in rows)

    段 = [{'起': 0.0, '边界依据': '素材开头'}]
    hard = [f for f in fades if 下限 / 2 < f < d - 下限 / 2]
    块 = [0.0] + hard + [d]
    for i in range(len(块) - 1):
        a, b = 块[i], 块[i + 1]
        if i > 0:
            段.append({'起': round(a, 1), '边界依据': f'黑场 {a:.1f} 秒'})
        cur = a
        while b - cur > 上限:
            候 = [c for c in cuts if cur + 下限 <= c <= cur + 上限]
            if not 候:
                候 = [c for c in cuts if cur + 下限 <= c <= b - 下限 / 2] or [cur + 目标]
            空 = [c for c in 候 if 在空档(c)]
            pick = min(空 or 候, key=lambda c: abs(c - (cur + 目标)))
            段.append({'起': round(pick, 1),
                       '边界依据': '镜头切点，落在字幕空档' if pick in 空 else '镜头切点'})
            cur = pick
    out = {'段': 段, '人物': [], '_合并': {'遍数': 0, '本地切段': True}}
    q = os.path.join(wd, 'segs.json')
    json.dump(out, open(q, 'w'), ensure_ascii=False, indent=1)
    print(f'本地切段：黑场 {len(hard)} 处，共 {len(段)} 个起点 → {q}')
    return out


def cmd_round2(mp4, sub, segs_json):
    d, wd = duration(mp4), workdir(mp4)
    segs = json.load(open(segs_json))
    segs = segs['段'] if isinstance(segs, dict) else segs
    starts, hit = clean_starts([float(s['起']) for s in segs], d)
    rows = load_sub(sub)
    # 按清理后的边界循环，不按模型报的段数——清理会并掉段，两者对不上
    for i in range(len(starts) - 1):
        a, b = starts[i], starts[i + 1]
        n = i + 1
        sd = b - a
        out = os.path.join(wd, 'round2', f'seg{n:02d}.mp4')
        clip(mp4, a, b, out)
        seg_rows = [(r[0], r[1], r[2]) for r in rows if r[1] > a and r[0] < b]
        # 镜头切点当锚点一起发下去（默认开，ROUND2_ANCHORS=0 关）。三遍并集实测只是把同一批
        # 时刻各说三遍（273 条事件落在 33 个时刻上），覆盖率该靠确定的锚点核对，
        # 不该靠多跑几遍碰运气。切点本地 ffmpeg 数出来的，不花钱。
        锚 = ''
        if os.environ.get('ROUND2_ANCHORS', '1') == '1':
            try:
                det = json.load(open(os.path.join(wd, 'detect.json')))
                cuts = [round(c - a, 1) for c in (det.get('镜头切换') or []) if a < c < b]
                if cuts:
                    锚 = ('\n【镜头切点】ffmpeg 数出来的，秒数已从 0 起算，共 %d 处：\n%s\n'
                          % (len(cuts), '、'.join(f'{c:.1f}' for c in cuts)))
            except Exception:
                锚 = ''
        txt = f"""【段序号】{n}
【这一段的时长】{sd:.1f} 秒
【视频】seg{n:02d}.mp4，时间码已经从 0 起算，直接按视频里的时间报

【字幕转写】格式为「起-止（秒）＋台词」，时间码已重算成从 0 起算
{fmt_sub(seg_rows, offset=a)}
{锚}"""
        open(os.path.join(wd, 'round2', f'seg{n:02d}_input.txt'), 'w',
             encoding='utf-8').write(txt)
        print(f'段{n:02d}  {a:7.1f}-{b:7.1f}  {sd:6.1f}s  台词 {len(seg_rows):3d} 条  → seg{n:02d}.mp4')
    json.dump({'段边界': starts, '素材时长': d, '切段门槛': hit},
              open(os.path.join(wd, 'round2', 'bounds.json'), 'w'),
              ensure_ascii=False, indent=2)
    print(f'\n→ {wd}/round2/')


# ---------- round 3 ----------

def cmd_round3(mp4):
    d, wd = duration(mp4), workdir(mp4)
    secs = [round(d * i / 10 + d / 20, 1) for i in range(10)]
    secs += [0.5, 1.5, 2.5]
    got = grab(mp4, sorted(set(secs)), os.path.join(wd, 'round3_frames'))
    tail_a = max(0, d - 30)
    clip(mp4, tail_a, d, os.path.join(wd, 'round3', 'tail30.mp4'))
    manifest = '\n'.join(f'第 {t:.1f} 秒：{n}' for t, n in got)
    txt = f"""【素材时长】{d:.1f} 秒

【截图清单】共 {len(got)} 张，判断字幕和语音形式用。每张前面的文字标明它是第几秒。
{manifest}

【片尾视频】tail30.mp4，是全片最后 30 秒（第 {tail_a:.1f} 秒到第 {d:.1f} 秒）。
结尾字卡就在这一段里。视频里的时间码从 0 起算，你报出的时间也从 0 起算，
本地会加回 {tail_a:.1f} 秒的偏移。
"""
    p = os.path.join(wd, 'round3_input.txt')
    open(p, 'w', encoding='utf-8').write(txt)
    print(f'第三轮：{len(got)} 张帧 + 片尾 30 秒切片')
    print(f'→ {p}\n→ {wd}/round3_frames/\n→ {wd}/round3/tail30.mp4')


# ---------- post ----------

def snap(t, shots, win=1.0):
    """把模型报的时间对齐到 ±win 秒内最近的真实镜头切换点。"""
    c = [s for s in shots if abs(s - t) <= win]
    return min(c, key=lambda s: abs(s - t)) if c else t


MOOD = {
    ('被压制','占上风'):'憋屈转爆发', ('被压制','平等'):'憋屈转缓和', ('被压制','被压制'):'一路虐',
    ('占上风','被压制'):'甜转虐', ('占上风','平等'):'爽转缓和', ('占上风','占上风'):'一路爽',
    ('平等','占上风'):'平转爽', ('平等','被压制'):'平转虐', ('平等','平等'):'平',
}


def read_segs(wd):
    """读第一步的逐段产出，附上起止和绝对时间。"""
    bounds = json.load(open(os.path.join(wd, 'round2', 'bounds.json')))
    starts, d = bounds['段边界'], bounds['素材时长']
    det = json.load(open(os.path.join(wd, 'detect.json')))
    segs = []
    for i in range(len(starts) - 1):
        f = os.path.join(wd, 'round2', f'seg{i+1:02d}_out.json')
        if not os.path.exists(f):
            print(f'缺 {f}，跳过'); continue
        sg = json.load(open(f))
        a, b = starts[i], starts[i + 1]
        sg['_序号'], sg['_起'], sg['_止'] = i + 1, a, round(b, 1)
        sg['_段时长'] = round(b - a, 1)
        # 模型偶尔漏写某一项的「时间」，或写成非数字。少一条记录不该让整个 Part 失败，
        # 丢掉那一条继续；事件的时间 check_schema 已经卡过，这里主要防屏幕文字和人物。
        def _秒(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        sg['事件'] = [e for e in sg.get('事件') or [] if _秒(e.get('时间')) is not None]
        for e in sg['事件']:
            e['绝对时间'] = round(snap(_秒(e['时间']) + a, det['镜头切换']), 1)
        sg['屏幕上的文字'] = [e for e in sg.get('屏幕上的文字') or [] if _秒(e.get('时间')) is not None]
        for e in sg['屏幕上的文字']:
            e['绝对时间'] = round(_秒(e['时间']) + a, 1)
            if _秒(e.get('持续到第几秒')) is not None:
                e['绝对消失时间'] = round(_秒(e['持续到第几秒']) + a, 1)
                e['挂了多久'] = round(_秒(e['持续到第几秒']) - _秒(e['时间']), 1)
        for pr in sg.get('人物') or []:
            if _秒(pr.get('首次出现')) is not None:
                pr['绝对时间'] = round(_秒(pr['首次出现']) + a, 1)
        segs.append(sg)
    return segs, d, det


def cmd_post1(wd):
    """第一步跑完。只算第一步产出撑得起的东西。

    情绪走向、反转次数、爽点密度这类要等第二步，不在这里算——第一步现在
    只出描述，不出分类。
    """
    segs, d, det = read_segs(wd)
    sub = None
    sp = os.path.join(wd, 'sub.tsv')
    if os.path.exists(sp):
        sub = load_sub(sp)
    for sg in segs:
        sd = sg['_段时长']
        sg['事件数'] = len(sg.get('事件') or [])
        sg['事件密度'] = round(sg['事件数'] / sd * 60, 2)
        pub = sum(1 for e in sg.get('事件') or [] if e.get('有旁人在看吗') == '有')
        sg['有旁人在场的事件占比'] = round(pub / sg['事件数'], 2) if sg['事件数'] else None
        if sub is not None:
            n = sum(1 for a, b, t in sub if b > sg['_起'] and a < sg['_止'])
            sg['台词句数'] = n
            sg['台词密度'] = round(n / sd * 10, 2)
        # 下面三个是段作为可复用片段的可观察属性，用来替代模型自评分。
        # 全部本地算，不问模型。
        cnt = [e.get('在场人数') for e in sg.get('事件') or []
               if isinstance(e.get('在场人数'), (int, float))]
        sg['在场人数峰值'] = max(cnt) if cnt else None
        shots = [x for x in det['镜头切换'] if sg['_起'] <= x < sg['_止']]
        sg['段内镜头数'] = len(shots)
        sg['段内镜头密度'] = round(len(shots) / sd * 60, 2)
        sg['段内平均镜头长度'] = round(sd / len(shots), 2) if shots else None

    scenes = [(sg.get('场景') or {}).get('地点') for sg in segs]
    r3p = os.path.join(wd, 'round3_out.json')
    r3 = json.load(open(r3p)) if os.path.exists(r3p) else {}
    tail_a = max(0, d - 30)

    def abst(v):
        return round(float(v) + tail_a, 1) if v is not None else None

    card, last = abst(r3.get('结尾字卡时间')), abst(r3.get('剧情停下的时间'))
    whole = {
        '素材时长': round(d, 1),
        '段数': len(segs),
        '场景数': len(set(x for x in scenes if x)),
        '场景轮换次数': sum(1 for a, b in zip(scenes, scenes[1:]) if a != b),
        '全片事件数': sum(sg['事件数'] for sg in segs),
        '强黑场处数': det['强黑场处数'],
        '镜头数': det['镜头数'],
        '平均镜头长度': det['平均镜头长度'],
        '长镜头占比': det['长镜头占比'],
        '结尾节奏比': det['结尾节奏比'],
        '字幕形式': r3.get('字幕形式'),
        '字幕语言': r3.get('字幕语言'),
        '语音形式': r3.get('语音形式'),
        '结尾字卡时间': card,
        '结尾字卡形式': r3.get('结尾字卡形式'),
        '剧情停下的时间': last,
        '剧情占比': round(last / d, 4) if last else None,
    }
    out = {'整条': whole, '段': segs}
    q = os.path.join(wd, 'stage1.json')
    json.dump(out, open(q, 'w'), ensure_ascii=False, indent=2)
    print(json.dumps(whole, ensure_ascii=False, indent=2))
    print(f'\n→ {q}')


def cmd_stage2(wd):
    """把第一步的产出组装成第二步的输入。

    第二步是文本模型，看不到视频。它需要每段的起点才能换算绝对时间，
    所以这里把起止时间显式写进每段的表头。
    """
    segs, d, det = read_segs(wd)
    parts = [f'【素材时长】{d:.1f} 秒　共 {len(segs)} 段\n']
    for sg in segs:
        parts.append(f"── 第 {sg['_序号']} 段　{sg['_起']:.1f} 到 {sg['_止']:.1f} 秒"
                     f"（{sg['_段时长']:.1f} 秒）")
        sc = sg.get('场景') or {}
        parts.append(f"场景：{sc.get('地点')}")
        who = '、'.join(f"{p.get('称呼')}（{p.get('外观')}）" for p in sg.get('人物') or [])
        parts.append(f'人物：{who}')
        parts.append('事件：')
        for e in sg.get('事件') or []:
            mark = ''
            if e.get('_同刻读法数', 1) > 1:
                mark = (f"　【同一时刻有 {e.get('_同刻读法数')} 种读法，这条来自"
                        f"{e.get('_出现遍数')} 遍，下面还会列出别的读法，你要裁决】")
            line = (f"  {e['绝对时间']:.1f} 秒　{e.get('画面')}"
                    f"　｜变化：{e.get('这一刻发生了什么变化')}"
                    f"　｜在场 {e.get('在场人数')} 人，旁人在看：{e.get('有旁人在看吗')}{mark}")
            if e.get('台词'):
                line += f"\n            台词：{e['台词']}"
            parts.append(line)
        if sg.get('屏幕上的文字'):
            parts.append('屏幕上的文字：')
            for e in sg['屏幕上的文字']:
                dwell = (f"，挂了 {e['挂了多久']:.0f} 秒"
                         if e.get('挂了多久') is not None else '')
                parts.append(f"  {e['绝对时间']:.1f} 秒　{e.get('内容')}"
                             f"（{e.get('位置')}{dwell}）")
        parts.append(f"段落叙述：{sg.get('段落叙述')}")
        parts.append(f"这一段最后一个镜头：{sg.get('这一段最后一个镜头')}\n")
    r3p = os.path.join(wd, 'round3_out.json')
    if os.path.exists(r3p):
        r3 = json.load(open(r3p))
        parts.append('【整条素材的呈现形式】')
        for k in ('字幕形式', '字幕语言', '语音形式'):
            parts.append(f'  {k}：{r3.get(k)}')
    q = os.path.join(wd, 'stage2_input.txt')
    open(q, 'w', encoding='utf-8').write('\n'.join(parts))
    print(f'{len(segs)} 段，{sum(len(sg.get("事件") or []) for sg in segs)} 个事件'
          f'，{len(open(q,encoding="utf-8").read())} 字')
    print(f'→ {q}')


MOOD = {
    ('被压制', '占上风'): '憋屈转爆发', ('被压制', '平等'): '憋屈转缓和',
    ('被压制', '被压制'): '一路虐', ('占上风', '被压制'): '甜转虐',
    ('占上风', '平等'): '爽转缓和', ('占上风', '占上风'): '一路爽',
    ('平等', '占上风'): '平转爽', ('平等', '被压制'): '平转虐',
    ('平等', '平等'): '平',
}


def cmd_post2(wd):
    """第二步跑完。整条字段全部从段规格算出来，模型不出整条计数。

    段是可复用单元：起止是 ffmpeg 的真实剪辑点，一段一份规格。整条这一层
    只做汇总——哪几段能当开头、哪几段能当钩子、剧情点怎么分布。
    """
    st1 = json.load(open(os.path.join(wd, 'stage1.json')))
    r2 = json.load(open(os.path.join(wd, 'stage2_out.json')))
    det = json.load(open(os.path.join(wd, 'detect.json')))
    d, shots, fades = st1['整条']['素材时长'], det['镜头切换'], det['强黑场']
    shot_avg = det['平均镜头长度']
    specs = {s.get('段序号'): s for s in r2.get('段规格') or []}
    segs = st1['段']
    cov = r2.get('_覆盖') or {}

    def snapt(v):
        return round(snap(float(v), shots), 1) if v is not None else None

    # 段规格并回段，让每段既有本地指标又有模型判断
    for sg in segs:
        sp = specs.get(sg['_序号']) or {}
        sg['最强的一刻'] = sp.get('这一段最强的一刻是什么')
        sg['最强的一刻时间'] = snapt(sp.get('最强的一刻在第几秒'))
        sg['结尾留没留悬念'] = sp.get('结尾留没留悬念')
        sg['留的是什么'] = sp.get('留的是什么')
        pts = sp.get('段里的剧情点') or []
        for p in pts:
            p['时间'] = snapt(p.get('时间'))
        sg['剧情点'] = pts
        sg['剧情点数'] = len(pts)
        # 峰值那一刻属于哪一类：按时间对上段里的剧情点
        t = sg['最强的一刻时间']
        hit = min((p for p in pts if p.get('时间') is not None),
                  key=lambda p: abs(p['时间'] - t), default=None) if t is not None else None
        sg['最强的一刻属于哪一类'] = hit.get('哪一类') if hit else None
        for k in ('亲密行为', '暴力行为', '羞辱行为'):
            acts = sp.get(k) or []
            for e in acts:
                e['时间'] = snapt(e.get('时间'))
            sg[k] = acts
        sg['亲密双方性别'] = sp.get('亲密双方性别')
        sg['阶层落差'] = sp.get('阶层落差')
        sg['_看成了'] = sg['_序号'] not in (cov.get('被拦的段') or [])

    seen = [sg for sg in segs if sg['_看成了']]
    allpts = [p for sg in seen for p in sg['剧情点']]
    kinds = ('反转', '爽点', '悬念点', '冲突', '情绪')

    def frac(n, m):
        return round(n / m, 2) if m else None

    def merge(key):
        """同一种行为在多段里都出现，整条只留最早那一次。

        分档看的是这条素材出现过的最重那一种行为，同种重复几次不影响档位，
        留最早那次是为了能回到原片去核对。
        """
        got = {}
        for sg in seen:
            for e in sg.get(key) or []:
                k, t = e.get('行为'), e.get('时间')
                if not k:
                    continue
                cur = got.get(k)
                if cur is None or (t is not None and (cur.get('时间') is None or t < cur['时间'])):
                    got[k] = e
        return sorted(got.values(), key=lambda e: e.get('时间') if e.get('时间') is not None else 0)

    # 各段报的是这一段里亲密双方的性别。整条出现过两种以上算「混合」；
    # 一种都没有分两种情况——真的没有亲密场面，和有场面但看不出性别。
    combos = {sg.get('亲密双方性别') for sg in seen} - {None, '无亲密场面', '判不出'}
    if len(combos) > 1:
        sex = '混合'
    elif combos:
        sex = combos.pop()
    elif any(sg.get('亲密双方性别') == '判不出' for sg in seen):
        sex = '判不出'
    else:
        sex = '无亲密场面'
    gap = [sg['_序号'] for sg in seen if sg.get('阶层落差') == '有']
    acts = {k: merge(k) for k in ('亲密行为', '暴力行为', '羞辱行为')}

    dur = [sg['_段时长'] for sg in segs]
    gou = [sg['_序号'] for sg in seen if sg['结尾留没留悬念'] == '留了']
    first = next((sg for sg in segs if sg['_序号'] == 1), None)

    out = dict(st1['整条'])
    out.update({
        '题材一级': (r2.get('题材') or {}).get('一级') if isinstance(r2.get('题材'), dict) else None,
        '题材二级': (r2.get('题材') or {}).get('二级') if isinstance(r2.get('题材'), dict) else None,
        '覆盖率': cov.get('覆盖率'),
        '被拦的段': cov.get('被拦的段') or [],
        # 内容被拒的完整记录。少了它，一条被拒的素材跑完只剩一个空壳文件，
        # 看不出是模型拒绝分析还是这条素材本来就没内容。
        '内容被拒': {
            '被拒段数': len(cov.get('被拦的段') or []),
            '总段数': cov.get('总段数'),
            '第一轮被拒': r2.get('_第一轮被拦'),
            '各段原因': cov.get('拒绝原因') or {},
            '这条能不能用': ('能' if (cov.get('覆盖率') or 0) >= 0.7 else
                             '不能，看成的段不到七成，整条统计跟别的素材不可比'),
        },

        # 拼装规则的骨架
        '段类型序列': [sg['最强的一刻属于哪一类'] for sg in segs],
        '结尾留悬念的段': gou,
        '结尾留悬念的段占比': frac(len(gou), len(seen)),
        '段时长最短': min(dur) if dur else None,
        '段时长最长': max(dur) if dur else None,
        '段时长中位': round(sorted(dur)[len(dur) // 2], 1) if dur else None,

        # 剧情点分布
        '剧情点总数': len(allpts),
        '剧情点每分钟': round(len(allpts) / (d / 60), 2) if d else None,
        **{f'{k}次数': sum(1 for p in allpts if p.get('哪一类') == k) for k in kinds},
        # 旁人在场和主角的方向分开统计。旧版只有一个「当众」，
        # 主角当众赢和主角当众输被算成同一件事——W08 十三处剧情点全是「当众有」，
        # 但一处主角动作都没有。
        '有旁人在场占比': frac(sum(1 for p in allpts if p.get('有旁人在看吗') == '有'), len(allpts)),

        # 高转化场景的词表是拿别人的行业经验定的，装不下我们的素材是有可能的。
        # 「都不是」占比就是它的验收指标：超过三成说明这套分类要换或者要扩。
        **{f'场景_{k}': sum(1 for p in allpts if p.get('高转化场景') == k)
           for k in ('当众反打脸', '身份揭露', '被迫亲密', '家庭危机', '权力失衡', '都不是')},
        '场景套不上占比': frac(sum(1 for p in allpts if p.get('高转化场景') == '都不是'), len(allpts)),
        '主角施加次数': sum(1 for p in allpts if p.get('主角在这一刻是') == '施加方'),
        '主角承受次数': sum(1 for p in allpts if p.get('主角在这一刻是') == '承受方'),
        '主角不在场次数': sum(1 for p in allpts if p.get('主角在这一刻是') == '都不是'),
        '主角有动作占比': frac(sum(1 for p in allpts
                                if p.get('主角在这一刻是') in ('施加方', '承受方')), len(allpts)),
        '施加占主角动作比': frac(
            sum(1 for p in allpts if p.get('主角在这一刻是') == '施加方'),
            sum(1 for p in allpts if p.get('主角在这一刻是') in ('施加方', '承受方'))),
        # 受压和反击交替了几次。一直被压或一路赢都算 0 次交替。
        '受压反击交替次数': (lambda seq: sum(1 for a, b in zip(seq, seq[1:]) if a != b))(
            [p['主角在这一刻是'] for p in allpts
             if p.get('主角在这一刻是') in ('施加方', '承受方')]),

        # 对答案那一轮没跑时这个数不是 0，是没问过。报 0 会被当成「悬念都给了答案」
        '未解悬念点次数': (None if any(e.get('_没跑对答案') for e in r2.get('悬念点') or [])
                        else sum(1 for e in r2.get('悬念点') or []
                                 if e.get('后面给答案了吗') == '没给')),
        # 悬念抛出到给答案隔了多久。间隔短说明素材自己把话说完了，
        # 间隔长说明它把观众吊着。没给答案的不计入。
        '悬念到答案的间隔中位': (lambda g: round(sorted(g)[len(g) // 2], 1) if g else None)(
            [float(e['答案在哪']) - float(e['时间'])
             for e in r2.get('悬念点') or []
             if e.get('后面给答案了吗') == '给了'
             and e.get('答案在哪') is not None and e.get('时间') is not None]),

        # 尺度：模型只报行为名，分档在 grade.py 里按这批素材的分布做，
        # 改档位不用重跑模型。
        **acts,
        **{f'{k}种数': len(v) for k, v in acts.items()},
        '性别组合': sex,
        '阶层落差的段': gap,
        '阶层落差段占比': frac(len(gap), len(seen)),
    })
    q = os.path.join(wd, 'structure.json')
    json.dump({'整条': out, '段': segs, '推理': r2},
              open(q, 'w'), ensure_ascii=False, indent=2)

    # 被拒的情况印在最显眼的地方。跑批时一屏几十行，不加这个框会被划过去。
    nb = out['内容被拒']
    if nb['被拒段数'] or nb['第一轮被拒']:
        name = os.path.basename(wd.rstrip('/')).replace('_拆解', '')
        print('\n' + '=' * 66)
        print(f'  ⚠ {name} 有内容被模型拒绝分析')
        print(f'    题材：{out.get("题材一级")} / {out.get("题材二级")}')
        if nb['第一轮被拒']:
            print(f'    整条脉络那一轮被拒：{nb["第一轮被拒"]}')
            print(f'    后果：这一条没有题材，也没有剧情时间线')
        if nb['被拒段数']:
            print(f'    {nb["总段数"]} 段里有 {nb["被拒段数"]} 段被拒'
                  f'（第 {out["被拦的段"]} 段），覆盖率 {out["覆盖率"]:.0%}')
            for n, w2 in list((nb['各段原因'] or {}).items())[:3]:
                print(f'      段 {n}：{w2}')
        print(f'    结论：{nb["这条能不能用"]}')
        print('=' * 66 + '\n')
    print(json.dumps(out, ensure_ascii=False, indent=2)[:2000])
    print(f'\n→ {q}')


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    c, a = sys.argv[1], sys.argv[2:]
    {'sub': cmd_sub, 'detect': cmd_detect, 'round1': cmd_round1,
     'round2': cmd_round2, 'round3': cmd_round3,
     'post1': cmd_post1, 'stage2': cmd_stage2, 'post2': cmd_post2}[c](*a)
