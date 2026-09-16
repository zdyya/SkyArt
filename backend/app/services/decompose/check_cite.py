#!/usr/bin/env python3
"""核对推理结果里的依据是不是编的。

用法：
  check_cite.py <推理输出.json> <字幕.tsv> [第一步产物目录]

指标：
  编造引用率   依据原文在语料里找不到的比例
  段错配率     原文找到了，但依据写的段序号跟它实际所在的段不符
  时间偏差     依据写的时间跟原文实际时间的差
  无依据实值   填了具体值却没给依据的字段数
  覆盖率       填出具体值、而不是「无法判断」的字段占比

台词依据拿字幕转写核，是唯一跨臂可比的硬指标：不管模型是边看边推理还是
先看后推理，它引用的台词要么在转写里，要么是编的。
"""
import json, re, sys, os, statistics


def norm(s):
    if not isinstance(s, str):
        return ''
    s = s.lower()
    s = re.sub(r"[^\w一-鿿]+", ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def load_sub(path):
    rows = []
    for line in open(path, encoding='utf-8', errors='replace'):
        p = line.rstrip('\n').split('\t')
        if len(p) >= 3 and p[0].strip().isdigit():
            rows.append((float(p[0]), float(p[1]), p[2].strip()))
    return rows


def build_corpus(sub_rows, stage1_dir):
    """返回 [(来源, 时间, 归一化文本, 原文)]。"""
    corpus = [('字幕', a, norm(t), t, '台词') for a, b, t in sub_rows if norm(t)]
    r2 = os.path.join(stage1_dir or '', 'round2')
    if not os.path.isdir(r2):
        return corpus, {}
    bounds = {}
    bp = os.path.join(r2, 'bounds.json')
    if os.path.exists(bp):
        st = json.load(open(bp))['段边界']
        bounds = {i + 1: (st[i], st[i + 1]) for i in range(len(st) - 1)}
    for f in sorted(os.listdir(r2)):
        m = re.match(r'seg(\d+)_out\.json$', f)
        if not m:
            continue
        n = int(m.group(1))
        off = bounds.get(n, (0, 0))[0]
        d = json.load(open(os.path.join(r2, f)))
        for e in d.get('事件') or []:
            t = off + float(e.get('时间', 0))
            for k, typ in (('画面', '画面'), ('台词', '台词'),
                           ('这一刻发生了什么变化', '画面')):
                if e.get(k):
                    corpus.append((f'第一步.段{n}.{k}', t, norm(e[k]), e[k], typ))
        for k in ('段落叙述', '这一段最后一个镜头'):
            if d.get(k):
                corpus.append((f'第一步.段{n}.{k}', off, norm(d[k]), d[k], '画面'))
        for e in d.get('屏幕上的文字') or []:
            if e.get('内容'):
                corpus.append((f'第一步.段{n}.屏幕文字',
                               off + float(e.get('时间', 0)), norm(e['内容']),
                               e['内容'], '屏幕文字'))
    return corpus, bounds


def find(text, corpus):
    """在语料里找这段原文。返回命中列表。

    宽松匹配会把编造的引用放过去：一句编出来的长台词里，随便嵌着一个真实
    的短句片段，就会被当成命中。所以包含关系两个方向都设了长度门槛。
    """
    q = norm(text)
    if not q:
        return []
    qw = q.split()
    hits = [c for c in corpus if q == c[2]]
    if hits:
        return hits
    for c in corpus:
        cw = c[2].split()
        if c[2] in q and len(cw) >= 4:
            hits.append(c)
        elif q in c[2] and len(qw) >= 3:
            hits.append(c)
    if hits:
        return hits
    if len(qw) >= 4:
        qs = set(qw)
        for c in corpus:
            cs = set(c[2].split())
            shared = qs & cs
            if len(shared) >= 4 and len(shared) / len(qs) >= 0.8:
                hits.append(c)
    return hits


def walk(o, path=''):
    """找出所有带依据的节点，产出 (路径, 值, 依据数组)。

    依据从单条 dict 改成了数组，一条判断可以同时引台词和画面。
    """
    if isinstance(o, dict):
        if '依据' in o:
            v = o.get('值', o.get('类型', o.get('时间')))
            ev = o['依据']
            if isinstance(ev, dict):
                ev = [ev]
            yield path, v, ev if isinstance(ev, list) else []
        for k, val in o.items():
            if k not in ('依据', '理由'):
                yield from walk(val, f'{path}.{k}' if path else k)
    elif isinstance(o, list):
        for i, val in enumerate(o):
            yield from walk(val, f'{path}[{i}]')


UNKNOWN = ('无法判断', None, '', 'null')


def main(pred_p, sub_p, stage1=None):
    pred = json.load(open(pred_p))
    corpus, bounds = build_corpus(load_sub(sub_p), stage1)
    nodes = list(walk(pred))

    fab, mism, devs, noev, offt, typo = [], [], [], [], [], []
    concrete, cited = 0, 0
    for path, val, evs in nodes:
        known = val not in UNKNOWN
        if known:
            concrete += 1
        real = [e for e in evs if isinstance(e, dict) and e.get('原文')]
        if not real:
            if known:
                noev.append(path)
            continue
        for j, ev in enumerate(real):
            cited += 1
            tag = f'{path}.依据[{j}]'
            hits = find(ev['原文'], corpus)
            if not hits:
                fab.append((tag, ev['原文']))
                continue
            claim_t = float(ev.get('时间') or 0)
            best = min(hits, key=lambda c: abs(c[1] - claim_t))
            dt = round(abs(best[1] - claim_t), 1)
            devs.append((tag, dt))
            if dt > 5.0:
                offt.append((tag, ev['原文'], claim_t, best[1]))
            want = ev.get('类型')
            if want and all(c[4] != want for c in hits):
                typo.append((tag, want, hits[0][4], ev['原文']))
            seg = ev.get('段')
            if seg and seg in bounds:
                a, b = bounds[seg]
                if not (a - 1 <= best[1] <= b + 1):
                    mism.append((tag, seg, best[1]))

    def pct(n):
        return f'{n}/{max(cited,1)} = {n/max(cited,1)*100:.1f}%'

    print(f'字段总数          {len(nodes)}')
    print(f'依据条数          {cited}')
    print(f'覆盖率            {concrete}/{len(nodes)} = {concrete/max(len(nodes),1)*100:.1f}%')
    print(f'编造引用率        {pct(len(fab))}')
    print(f'依据类型错配率    {pct(len(typo))}（说是台词，实际只在画面描述里找到，或反之）')
    print(f'段错配率          {pct(len(mism))}')
    print(f'时间错位率        {pct(len(offt))}（原文找到了，但差 5 秒以上）')
    if devs:
        print(f'时间偏差中位数    {statistics.median(d for _, d in devs):.1f} 秒'
              f'（最大 {max(d for _, d in devs):.1f} 秒）')
    print(f'填了值但无依据    {len(noev)} 个')
    if fab:
        print('\n编造的引用：')
        for t, x in fab[:15]:
            print(f'  {t}\n    「{x}」')
    if typo:
        print('\n依据类型错配：')
        for t, w, g, x in typo[:10]:
            print(f'  {t}  写的是{w}，实际匹配到{g}\n    「{x}」')
    if offt:
        print('\n时间错位：')
        for t, x, claim, real in offt[:10]:
            print(f'  {t}  依据写 {claim:.1f} 秒，原文实际在 {real:.1f} 秒\n    「{x}」')
    if mism:
        print('\n段错配：')
        for t, sg, rt in mism[:10]:
            print(f'  {t}  依据写段{sg}，原文实际在 {rt:.1f} 秒')
    if noev:
        print('\n填了值但没给依据：')
        for t in noev[:15]:
            print(f'  {t}')


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    main(*sys.argv[1:4])
