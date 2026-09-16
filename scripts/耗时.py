# -*- coding: utf-8 -*-
"""从后端日志里量出「Part 合成」和「AI 分析六步」各花了多久。

日志每行带时间戳（_Tee 加的），阶段边界靠管线自己打印的那几句话认。
同一个 Part 的行按它的拆解目录名归堆，多个 Part 并发时不会串。
"""
import re, sys, glob
from datetime import datetime

行 = re.compile(r'^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\]\s?(.*)$')

def 读(路径):
    出 = []
    for ln in open(路径, encoding='utf-8', errors='ignore'):
        m = 行.match(ln)
        if m:
            出.append((datetime.fromisoformat(m.group(1)), m.group(2)))
    return 出

def 名(t):
    # 用完整路径当键：不同项目的 Part 目录重名（都叫 Part1_拆解），
    # 只取最后一段会把两次跑的时间戳混成一堆，量出 1400 秒这种假数。
    m = re.search(r'(/\S*?/([^/]+)_拆解)/', t)
    if not m:
        return None
    return m.group(1).replace('/Users/jimmy/VScode/SkyArt-段库分析/backend/uploads/AdMake/', '')

def 秒(a, b):
    return (b - a).total_seconds()

def 拆解耗时(事):
    """每个 Part 一张表：六步各自的起止。"""
    活 = {}
    for ts, t in 事:
        n = 名(t)
        # 认字幕这一步打印的三句都不带路径，只能挂到紧接着出现的那条 sub.tsv 上。
        # 取最近一次而不是队列头：中间隔着别的 Part 跑过的话，队列头是上一轮的，
        # 配出来就是十几分钟的假数。
        if '字幕带裁剪区' in t or '没找到台词字幕带' in t:
            活['_裁'] = ts
            continue
        if '帧，开始识别' in t:
            活['_识'] = ts
            continue
        if not n:
            continue
        d = 活.setdefault(n, {})
        if 'sub.tsv' in t:
            d['认字幕止'] = ts
            if 活.get('_裁'): d['认字幕起'] = 活.pop('_裁')
            if 活.get('_识'): d['识别起'] = 活.pop('_识')
        elif 'detect.json' in t:       d['找镜头止'] = ts
        elif 'round1_input.txt' in t:  d['切段起'] = ts
        elif 'segs.json' in t:         d['切段止'] = ts
        elif 'round2/' in t and t.strip().endswith('round2/'): d['描述起'] = ts
        elif re.search(r'seg\d+_out\.json', t):
            d['描述止'] = ts
            d.setdefault('描述起', d.get('切段止'))
        elif 'stage1.json' in t:       d['理结构起'] = ts
        elif 'structure.json' in t:    d['理结构止'] = ts
        elif '打分' in t:              d['打分止'] = ts
    for k in ('_裁', '_识'):
        活.pop(k, None)
    return 活

def 报(活):
    for n, d in 活.items():
        段 = []
        def 加(名字, a, b):
            if d.get(a) and d.get(b): 段.append((名字, 秒(d[a], d[b])))
        加('认字幕·抽帧', '认字幕起', '识别起')
        加('认字幕·OCR', '识别起', '认字幕止')
        加('找镜头', '认字幕止', '找镜头止')
        加('切段前等待', '找镜头止', '切段起')
        加('切段（模型）', '切段起', '切段止')
        加('逐段描述（模型）', '描述起', '描述止')
        加('理结构（模型）', '理结构起', '理结构止')
        加('给段打分', '理结构止', '打分止')
        if not 段: continue
        总 = sum(x[1] for x in 段)
        print(f'\n{n}　合计 {总:.0f} 秒')
        for k, v in 段:
            条 = '█' * max(1, round(v / max(总, 1) * 40))
            print(f'  {k:<16}{v:7.1f} 秒  {v/总*100:5.1f}%  {条}')

def 合成耗时(事):
    起 = {}
    print('\n— Part 合成 —')
    for ts, t in 事:
        if 'video.merge_reencode' in t or 'video.merge_copy' in t:
            m = re.search(r'out_path=(\S+)', t)
            起[m.group(1) if m else '?'] = (ts, '重编码' if 'reencode' in t else '直拼')
        for k in ('admake.copy_source', 'admake.build_part', 'admake.cut_part'):
            if k in t and 'status=ready' in t:
                d = re.search(r'duration_sec=([\d.]+)', t)
                if 起:
                    p, (a, 法) = list(起.items())[-1]
                    起.pop(p, None)
                    用 = 秒(a, ts)
                    长 = float(d.group(1)) if d else 0
                    print(f'  {法}　{用:6.1f} 秒　片长 {长:.0f} 秒　'
                          f'{用/长*60:.1f} 秒/分钟素材' if 长 else f'  {法}　{用:6.1f} 秒')

f = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(
    '/Users/jimmy/VScode/SkyArt-段库分析/backend/logs/backend-*.log'))[-1]
事 = 读(f)
合成耗时(事)
print('\n— AI 分析 —')
报(拆解耗时(事))
