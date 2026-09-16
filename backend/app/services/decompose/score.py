# -*- coding: utf-8 -*-
"""给剧情点打两个分：剧情分和尺度分。

剧情分回答「这一刻在故事上有多强」，尺度分回答「这一刻的身体冲击有多大」。
两件事分开记——实测全剧剧情分前 15% 的点里，只有四分之一带行为，
合成一个分会让亲密戏和暴力戏盖掉所有身份揭露、当众打脸这类剧情高点。

剧情分 = 段落类型权重 + 经典桥段权重 + 段尾悬念 1.5 + 主角施加方 1.0
权重按取值在段库里的稀缺度定：越少见的取值分越高，log(总数/出现次数)，
封顶 3.0，出现少于 10 次的按 10 次算，免得只出现一两次的取值吃掉全部权重。

尺度分直接取这一刻前后 2 秒内命中的行为里最高的那一档，没有行为就是 0。

剧情点本身只是一个时间点，跳过去看到的是一个两秒的镜头，像衔接不像戏。
所以再给每个点配一段起止，用在两处：进度条上画刻度，以及超过格时长上限的
段进格时裁哪一截——64 段里有 13 段长于 55 秒，这些必须裁。

配法：先拿字幕找出它所在的那一整块连续对白（句与句间隔不超过 2.5 秒算连着），
两头再吸附到最近的镜头切换点，最后夹进本段、限在 8 到 30 秒。落进同一块对白的
相邻点合成一段，留分最高的那个当代表。没有剧情点伴随的亲密、暴力、羞辱行为
也各算一条——E01 段 1 只有接吻没有剧情点，光看剧情点会整段漏掉。

用法：
    python3 score.py <拆解目录>          # 就地给 structure.json 写剧情分和高光候选
    python3 score.py <拆解目录> --看      # 只看分布和排行，不写文件
"""
import collections
import glob
import json
import math
import os

from .环境 import 拆解失败
import sys

from .grade import TABLES, canon

GAP = 2.5        # 字幕句与句间隔在这个秒数内算同一块对白
# 片段时长区间。模板里一个格要 20 到 55 秒、装 1 到 2 段，所以片段短于 8 秒
# 填不满格，也看不出戏；长于 30 秒两条就顶满一个格，失去挑选余地。
MIN_D = 8.0
MAX_D = 30.0

WIN = 2.0        # 行为和剧情点的时间差在这个秒数内才算命中
# 关键时刻两头至少留多少秒。对白块的边界常常正好落在动作那一下上，实测 128 条
# 高光里有 28 条结尾距关键时刻不到 2 秒——铺垫留全了，那一下被切掉。
HEAD_PAD = 1.0
TAIL_PAD = 2.0
CAP = 3.0        # 单项权重上限
FLOOR = 10       # 出现次数少于这个数的取值，按这个数算稀缺度
TAIL_W = 1.5     # 落在段尾五秒内且这一段留了悬念
ACTOR_W = 1.0    # 主角是施加方
TOP = 0.15       # 默认门槛取全库前多少

_GRADE = {(kind, name): lv
          for kind, tb in TABLES.items()
          for lv, names in tb.items()
          for name in names}


def 行为表(seg):
    """把一段里三类行为摊平成 (秒数, 档位) 列表。"""
    out = []
    for key, kind in (('亲密行为', '亲密'), ('暴力行为', '暴力'), ('羞辱行为', '羞辱')):
        for e in seg.get(key) or []:
            if e.get('时间') is None:
                continue
            lv = _GRADE.get((kind, canon(e.get('行为'), TABLES[kind])), 0)
            out.append((float(e['时间']), lv))
    return out


def 行为候选(seg, pts):
    """没有剧情点伴随的行为，单独当一条高光候选。

    判定按 WIN：行为时间点前后两秒内有剧情点，就算已经被那个点带上了，
    不再单列——并段那一步也会把它们并回去，单列只是多一条重复。
    """
    有 = [p['时间'] for p in pts if p.get('时间') is not None]
    out = []
    for key, kind in (('亲密行为', '亲密'), ('暴力行为', '暴力'), ('羞辱行为', '羞辱')):
        for e in seg.get(key) or []:
            t = e.get('时间')
            if t is None or any(abs(t - x) <= WIN for x in 有):
                continue
            name = canon(e.get('行为'), TABLES[kind])
            out.append({'时间': float(t), '哪一类': '行为',
                        '是什么': f'{kind}·{name}', '来自': '行为',
                        '剧情分': 0.0, '尺度分': _GRADE.get((kind, name), 0),
                        '主角在这一刻是': None, '有旁人在看吗': seg.get('有旁人在看吗'),
                        '高转化场景': '都不是', '依据': e.get('依据') or []})
    return out


def 尺度分(t, acts):
    near = [lv for sec, lv in acts if abs(sec - t) <= WIN]
    return max(near) if near else 0


def 读字幕(拆解目录):
    """字幕是整秒粒度，末尾补一秒才盖得住这句话说完。"""
    p = os.path.join(拆解目录, 'sub.tsv')
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p).read().strip().split('\n'):
        c = line.split('\t')
        if len(c) < 3:
            continue
        a, b = float(c[0]), float(c[1])
        out.append((a, max(b, a) + 1.0))
    return sorted(out)


def 对白块(subs, t):
    """t 落在哪一整块连续对白里。离最近的一句都超过 GAP 就算没有对白。"""
    if not subs:
        return None
    def 距(i):
        a, b = subs[i][0], subs[i][1]
        return 0 if a <= t <= b else min(abs(a - t), abs(b - t))
    i = min(range(len(subs)), key=距)
    if 距(i) > GAP:
        return None
    a = i
    while a > 0 and subs[a][0] - subs[a - 1][1] <= GAP:
        a -= 1
    b = i
    while b < len(subs) - 1 and subs[b + 1][0] - subs[b][1] <= GAP:
        b += 1
    return subs[a][0], subs[b][1]


def 吸附(v, shots, 向前):
    c = [x for x in shots if (x <= v if 向前 else x >= v)]
    if not c:
        return None
    return max(c) if 向前 else min(c)


def 配区间(t, subs, shots, lo, hi):
    k = 对白块(subs, t)
    a, b = k if k else (t - 2.0, t + 2.0)
    a = 吸附(a, shots, True) or a
    b = 吸附(b, shots, False) or b
    a, b = max(lo, a), min(hi, b)
    # 两头补够余地。补出来的那一头也吸附到镜头切换点，但切换点离得太远就不吸，
    # 免得为了对齐一个远处的切点把窗口拉长十几秒。
    if b - t < TAIL_PAD:
        want = min(hi, t + TAIL_PAD)
        nb = 吸附(want, shots, False)
        b = min(hi, nb if nb is not None and nb - want <= TAIL_PAD else want)
    if t - a < HEAD_PAD:
        want = max(lo, t - HEAD_PAD)
        na = 吸附(want, shots, True)
        a = max(lo, na if na is not None and want - na <= HEAD_PAD else want)
    if b - a < MIN_D:
        差 = MIN_D - (b - a)
        a = max(lo, a - 差 * 0.6)
        b = min(hi, b + 差 * 0.4)
    if b - a > MAX_D:
        a = max(lo, min(a, t - MAX_D * 0.7))
        b = min(hi, a + MAX_D)
    return round(a, 1), round(b, 1)


def 并段(pts):
    """区间重叠的相邻点并成一段，留分最高的当代表，其余记进 同段还有。"""
    if not pts:
        return []
    pts = sorted(pts, key=lambda p: (p['起'], p['止']))
    out = [[pts[0]]]
    for p in pts[1:]:
        if p['起'] < out[-1][-1]['止']:
            out[-1].append(p)
        else:
            out.append([p])
    res = []
    for 组 in out:
        主 = max(组, key=lambda p: (p['剧情分'], p['尺度分']))
        a = min(p['起'] for p in 组)
        b = max(p['止'] for p in 组)
        if b - a > MAX_D:          # 一长串连着说的话会把片段链到几十秒，掐回代表点周围
            t = 主.get('时间') or (a + b) / 2
            a = max(a, min(t - MAX_D * 0.7, b - MAX_D))
            b = min(b, a + MAX_D)
        主['起'], 主['止'] = round(a, 1), round(b, 1)
        主['尺度分'] = max(p['尺度分'] for p in 组)
        主['同段还有'] = [p['是什么'] for p in 组 if p is not 主]
        res.append(主)
    return res


def 定权重(segs):
    """按稀缺度定权重。段落类型和经典桥段各一张表。"""
    pts = [p for s in segs for p in (s.get('剧情点') or []) if p.get('时间') is not None]
    n = len(pts)

    def one(key, skip):
        c = collections.Counter(p.get(key) for p in pts)
        return {v: round(min(CAP, math.log(n / max(cnt, FLOOR))), 2)
                for v, cnt in c.items() if v not in skip}

    return one('哪一类', (None,)), one('高转化场景', ('都不是', None)), n


def 打分(segs, 字幕={}, 镜头={}):
    """就地给每个剧情点写 剧情分、尺度分、起止，给每段写 段内最高剧情分。"""
    w类, w桥段, n = 定权重(segs)
    for s in segs:
        subs = 字幕.get(id(s)) or []
        shots = 镜头.get(id(s)) or []
        acts = 行为表(s)
        留 = s.get('结尾留没留悬念') == '留了'
        止 = s.get('_止') or 0
        高 = 0.0
        止 = s.get('_止') or 0
        for p in s.get('剧情点') or []:
            t = p.get('时间')
            if t is None:
                p['剧情分'] = 0.0
                p['尺度分'] = 0
                continue
            v = w类.get(p.get('哪一类'), 0) + w桥段.get(p.get('高转化场景'), 0)
            if 留 and (止 - t) <= 5:
                v += TAIL_W
            if p.get('主角在这一刻是') == '施加方':
                v += ACTOR_W
            p['剧情分'] = round(v, 1)
            p['尺度分'] = 尺度分(t, acts)
            p['起'], p['止'] = 配区间(t, subs, shots, s.get('_起') or 0, 止)
            高 = max(高, p['剧情分'])
        候 = [dict(p) for p in (s.get('剧情点') or []) if p.get('时间') is not None]
        for c in 候:
            c['来自'] = '剧情点'
        for c in 行为候选(s, s.get('剧情点') or []):
            c['起'], c['止'] = 配区间(c['时间'], subs, shots, s.get('_起') or 0, 止)
            候.append(c)
        s['剧情片段'] = 并段(候)
        s['段内最高剧情分'] = round(高, 1)
    return w类, w桥段, n


def 门槛(segs, top=TOP):
    vs = sorted((p['剧情分'] for s in segs for p in (s.get('剧情点') or [])), reverse=True)
    return vs[int(len(vs) * top)] if vs else 0.0


def 收(root):
    """读一个目录下所有 structure.json，按整库定权重。字幕和镜头按段挂上去。"""
    files = sorted(glob.glob(os.path.join(root, '*_拆解', 'structure.json')))
    docs, segs, 字幕, 镜头 = [], [], {}, {}
    for f in files:
        d = json.load(open(f))
        docs.append((f, d))
        wd = os.path.dirname(f)
        subs = 读字幕(wd)
        det = os.path.join(wd, 'detect.json')
        shots = (json.load(open(det)).get('镜头切换') or []) if os.path.exists(det) else []
        for s in d.get('段') or []:
            segs.append(s)
            字幕[id(s)] = subs
            镜头[id(s)] = shots
    return docs, segs, 字幕, 镜头


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root = sys.argv[1]
    只看 = '--看' in sys.argv
    docs, segs, 字幕, 镜头 = 收(root)
    if not segs:
        raise 拆解失败('这个 Part 还没拆出段来，先跑一次 AI 分析。')
    w类, w桥段, n = 打分(segs, 字幕, 镜头)
    thr = 门槛(segs)
    print(f'{len(docs)} 个拆解，{len(segs)} 段，{n} 个剧情点')
    print('段落类型权重：' + '　'.join(f'{k} {v}' for k, v in sorted(w类.items(), key=lambda x: -x[1])))
    print('经典桥段权重：' + '　'.join(f'{k} {v}' for k, v in sorted(w桥段.items(), key=lambda x: -x[1])))
    pts = [(p, s) for s in segs for p in (s.get('剧情点') or [])]
    过 = [x for x in pts if x[0]['剧情分'] >= thr]
    print(f'\n前 {TOP:.0%} 门槛 = {thr} 分，过门槛 {len(过)} 个，平均每段 {len(过)/len(segs):.1f} 个')
    print('\n排行前 10：')
    for p, s in sorted(pts, key=lambda x: -x[0]['剧情分'])[:10]:
        tag = [p.get('哪一类')]
        if p.get('高转化场景') not in ('都不是', None):
            tag.append(p['高转化场景'])
        if p['尺度分']:
            tag.append(f"尺度档{p['尺度分']}")
        print(f"  {p['剧情分']:>4} 段{s.get('段序号')} {p['时间']:>6.1f}s "
              f"{(p.get('是什么') or '')[:26]:<28}（{'＋'.join(tag)}）")
    if 只看:
        sys.exit(0)
    for f, d in docs:
        json.dump(d, open(f, 'w'), ensure_ascii=False, indent=1)
    print(f'\n已写回 {len(docs)} 个 structure.json')
