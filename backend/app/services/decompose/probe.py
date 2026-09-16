"""探测一条素材的版式：哪一条横带是台词字幕。

写死裁剪区只对量过的那两条素材有效。这批 30 条里有的完全没有台词字幕，
只有剧名和结尾引导语，拿固定裁剪区去认，会把剧名当成字幕反复记下来。

判据是变不变：剧名、角标、结尾引导语在整条素材里一直是同一行字；台词
一直在换。所以抽若干帧全画面识别，按纵向位置分带，看每条带里的文字有
几种不同内容。
"""
import glob
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(line_buffering=True)
BASE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(BASE, 'ocrbin')
FRAMES = 24          # 抽多少帧。太少判不出变不变，太多白费时间
BAND = 6.0           # 纵向位置差在这个百分比内算同一条带
MIN_VARY = 0.7       # 一条带里内容几乎每帧都不同才算字幕。固定带实测都在 0.45 以下
MIN_FRAMES = 4       # 只在两三帧里出现的是片头标题卡，不是字幕带


def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout


def probe(mp4, keep=False, frames=FRAMES):
    d = float(sh(f'ffprobe -v error -show_entries format=duration '
                 f'-of csv=p=0 "{mp4}"').strip())
    wd = os.path.join(os.path.dirname(mp4), '_probe',
                      f'{os.path.basename(mp4)[:-4]}_{frames}')
    os.makedirs(wd, exist_ok=True)
    # 掐掉头尾各 5%：片头有标题卡、片尾有引导语，两头都不代表正片版式
    times = [d * (0.05 + 0.9 * i / (frames - 1)) for i in range(frames)]
    for i, t in enumerate(times):
        p = f'{wd}/{i:03d}.jpg'
        if not os.path.exists(p):
            sh(f'ffmpeg -y -ss {t:.2f} -i "{mp4}" -frames:v 1 "{p}" 2>/dev/null')
    raw = sh(f'"{BIN}" "{wd}"/*.jpg')

    obs = []
    for ln in raw.splitlines():
        pt = ln.split('\t')
        if len(pt) < 4 or not pt[1].strip() or not pt[3].strip():
            continue
        obs.append({'帧': pt[0], '上': float(pt[1]), '下': float(pt[2]),
                    '文字': pt[3].strip()})

    bands = []
    for o in sorted(obs, key=lambda x: x['上']):
        b = next((x for x in bands if abs(x['中'] - (o['上'] + o['下']) / 2) <= BAND), None)
        if b is None:
            bands.append({'中': (o['上'] + o['下']) / 2, '上': o['上'],
                          '下': o['下'], '条目': [o]})
        else:
            b['条目'].append(o)
            b['上'] = min(b['上'], o['上']); b['下'] = max(b['下'], o['下'])
            b['中'] = sum((x['上'] + x['下']) / 2 for x in b['条目']) / len(b['条目'])

    for b in bands:
        txts = [x['文字'] for x in b['条目']]
        b['帧数'] = len({x['帧'] for x in b['条目']})
        b['不同内容数'] = len(set(txts))
        b['变化率'] = round(b['不同内容数'] / max(len(txts), 1), 2)
        b['是字幕'] = (b['变化率'] >= MIN_VARY and b['帧数'] >= MIN_FRAMES
                       and b['不同内容数'] >= 3)
        b['样例'] = txts[:2]
        del b['条目']
    # 多条带都像字幕时，取出现帧数最多的那条，再并进跟它挨着的
    subs = [b for b in bands if b['是字幕']]
    if subs:
        main = max(subs, key=lambda b: b['帧数'])
        subs = [b for b in subs if abs(b['中'] - main['中']) <= 15.0]
    if not keep:
        for f in glob.glob(f'{wd}/*.jpg'):
            os.remove(f)
    # 台词稀疏的素材（W14 三分钟里只有三处对话）在 24 帧里凑不够帧数门槛，
    # 多抽一倍帧再判一次，判完还是没有才算真没字幕。
    if not subs and frames == FRAMES:
        return probe(mp4, keep=keep, frames=FRAMES * 2)
    top = min((b['上'] for b in subs), default=None)
    bot = max((b['下'] for b in subs), default=None)
    return {'素材': os.path.basename(mp4)[:-4], '时长': round(d, 1),
            '有台词字幕': bool(subs), '字幕带上沿%': top, '字幕带下沿%': bot,
            '横带': sorted(bands, key=lambda b: b['中'])}


def crop(mp4, r=None, pad=1.5):
    """把字幕带换成 ffmpeg 的 crop 参数，按实际分辨率算，不写死像素。"""
    r = r or probe(mp4)
    if not r['有台词字幕']:
        return None
    w, h = sh(f'ffprobe -v error -select_streams v:0 -show_entries '
              f'stream=width,height -of csv=p=0 "{mp4}"').strip().split(',')
    w, h = int(w), int(h)
    y = max(0, int(h * (r['字幕带上沿%'] - pad) / 100))
    y2 = min(h, int(h * (r['字幕带下沿%'] + pad) / 100))
    return f'{w}:{y2 - y}:0:{y}'


if __name__ == '__main__':
    d = sys.argv[1]
    mp4s = sorted(glob.glob(os.path.join(d, '*.mp4'))) if os.path.isdir(d) else [d]
    out = []
    for m in mp4s:
        r = probe(m)
        r['裁剪区'] = crop(m, r)
        out.append(r)
        flag = f"字幕带 {r['字幕带上沿%']:.0f}–{r['字幕带下沿%']:.0f}%  裁剪 {r['裁剪区']}" \
            if r['有台词字幕'] else '没有台词字幕'
        print(f"{r['素材']:6} {r['时长']/60:5.1f} 分  {flag}")
        for b in r['横带']:
            print(f"    {b['上']:5.1f}–{b['下']:5.1f}%  {b['帧数']:2d} 帧  "
                  f"{b['不同内容数']:2d} 种  变化率 {b['变化率']:.2f}  "
                  f"{'字幕' if b['是字幕'] else '固定'}  {b['样例'][0][:44]}")
    if os.path.isdir(d):
        p = os.path.join(d, '_版式.json')
        json.dump(out, open(p, 'w'), ensure_ascii=False, indent=2)
        print(f'\n→ {p}')
