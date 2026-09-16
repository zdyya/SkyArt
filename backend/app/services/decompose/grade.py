# -*- coding: utf-8 -*-
"""把第二步标出的行为分档，再跟投放数据对一遍。

模型只报行为名，档位在这里定。档位按这批海外短剧自己的分布排——
牵手拥抱在这个品类里遍地都是，明确性行为和器械才是这批素材里的上限。
改档位不用重跑模型。

输入是各条素材的 structure.json（第二步跑完的产物），不再读 _尺度分级.json。
"""
import json
import os
import re
import statistics as st
from collections import Counter, defaultdict

# 跟投放数据对账那一段用的素材目录，只有 grade.py 单独跑才会读。用 W30 覆盖。
W30 = os.environ.get('W30', os.path.expanduser('~/Downloads/w30'))

# 档 1 是这个品类的地板，档 4 是这批素材里的上限
INT = {
    1: ['牵手', '拥抱', '搂腰', '亲脸', '性暗示台词'],
    2: ['亲脖子', '接吻', '换衣脱衣', '内衣出镜', '浴缸或淋浴同处'],
    3: ['深吻', '抚摸身体', '上身裸露', '床上同处', '贴身纠缠', '壁咚或压制', '跨坐'],
    4: ['大面积裸露', '明确性行为', '器械或情趣用品', '捆绑', '主奴称呼'],
}
VIO = {
    1: ['推搡', '拉扯', '抓手腕', '言语威胁', '摔东西'],
    2: ['掀桌', '打耳光', '泼水', '下跪或被迫下跪', '推倒在地', '按在墙上'],
    3: ['拳打脚踢', '掐脖子', '下药', '注射'],
    4: ['见血', '持械', '开枪', '绑架', '致死', '尸体出镜'],
}
# 羞辱按损尊严的方式排：只动嘴的最轻，逼人放弃人格的最重
HUM = {
    1: ['当众辱骂', '被叫侮辱性称呼', '被当众贬低出身', '被围观嘲笑', '被拒之门外'],
    2: ['被当众揭穿隐私或短处', '被当众赶走', '被扔钱或被当面数钱打发',
        '被当众解雇', '被当众否认身份或关系', '被抢走或毁坏心爱之物'],
    3: ['被泼水或泼食物', '被吐口水', '被迫道歉', '被要求叫尊称'],
    4: ['被迫下跪或磕头', '被撕扯衣服示众', '被逼吃或喝不该吃的'],
}
TABLES = {'亲密': INT, '暴力': VIO, '羞辱': HUM}


FIXED = Counter()      # 纠过的错别字，跑完报出来


def canon(name, table):
    """行为名对回词表。

    模型偶尔写错一个字，实测有把「被叫侮辱性称呼」写成「被叫侮辱性称妇」的。
    长度相同又只差一个字的对回去；差得更多的原样留着，让 unknown() 报出来
    ——那种更可能是我们词表不够用，不是错别字。
    """
    if not name:
        return name
    known = {x for v in table.values() for x in v}
    if name in known:
        return name
    for k in known:
        if len(k) == len(name) and sum(a != b for a, b in zip(k, name)) == 1:
            FIXED[f'{name} → {k}'] += 1
            return k
    return name


def lv(acts, table):
    """一条素材的档位：出现过的最重那一档。没有任何行为记 0。"""
    got = {e.get('行为') for e in acts or []}
    for k in sorted(table, reverse=True):
        if got & set(table[k]):
            return k
    return 0


def unknown(rows):
    """词表以外的行为名。模型造词或者我们词表不够用，都会落在这里。"""
    out = defaultdict(Counter)
    for r in rows:
        for kind, table in TABLES.items():
            known = {x for v in table.values() for x in v}
            for a in r['acts'][kind]:
                if a not in known:
                    out[kind][a] += 1
    return out


def load():
    inv = json.load(open(f'{W30}/_投放.json'))
    rows = []
    for p in sorted(os.listdir(W30)):
        if not re.fullmatch(r'W\d\d_拆解', p):
            continue
        tag = p[:3]
        q = f'{W30}/{p}/structure.json'
        if not os.path.exists(q) or tag not in inv:
            continue
        w = json.load(open(q))['整条']
        acts = {k: (w.get(f'{k}行为') or []) for k in TABLES}
        for kind, v in acts.items():
            for e in v:
                e['行为'] = canon(e.get('行为'), TABLES[kind])
        rows.append(dict(
            t=tag, iv=inv[tag],
            cov=w.get('覆盖率'),
            a=lv(acts['亲密'], INT), v=lv(acts['暴力'], VIO), h=lv(acts['羞辱'], HUM),
            sex=w.get('性别组合') or '?',
            gapf=w.get('阶层落差段占比'),
            n={k: len(v) for k, v in acts.items()},
            blk=w.get('被拦的段') or [],
            acts={k: {e.get('行为') for e in v} for k, v in acts.items()},
        ))
    return rows


def main():
    rows = load()
    if not rows:
        print('没有 structure.json，先跑第二步')
        return
    med = lambda ks: st.median([r['iv']['ROAS'] or 0 for r in ks]) if ks else 0

    print(f'{len(rows)} 条\n')
    print(f'{"":5}{"组":<4}{"ROAS":>6}{"花费":>7}'
          f'{"亲密":>5}{"种":>4}{"暴力":>5}{"种":>4}{"羞辱":>5}{"种":>4}'
          f'{"性别组合":<10}{"阶层落差":>9}{"被拦":>5}')
    for r in sorted(rows, key=lambda r: -(r['iv']['ROAS'] or 0)):
        gf = f"{r['gapf']:.2f}" if r['gapf'] is not None else '-'
        print(f"{r['t']:5}{r['iv']['组']:<4}{(r['iv']['ROAS'] or 0):>6.3f}{r['iv']['花费']:>7.0f}"
              f"{r['a']:>5}{r['n']['亲密']:>4}{r['v']:>5}{r['n']['暴力']:>4}"
              f"{r['h']:>5}{r['n']['羞辱']:>4}"
              f"{r['sex']:<10}{gf:>9}{len(r['blk']):>5}")

    for name, key in (('亲密', 'a'), ('暴力', 'v'), ('羞辱', 'h')):
        print(f'\n=== {name}档 × ROAS ===')
        for k in sorted({r[key] for r in rows}):
            g = [r for r in rows if r[key] == k]
            c = sum(r['iv']['花费'] for r in g)
            rv = sum(r['iv']['收入'] or 0 for r in g)
            print(f'  档 {k}　{len(g):>2} 条　ROAS 中位 {med(g):.3f}　'
                  f'合并 ROAS {(rv / c if c else 0):.4f}　花费 ${c:>6.0f}　'
                  f'{" ".join(r["t"] for r in g)}')

    print(f'\n=== 性别组合 × ROAS ===')
    g2 = defaultdict(list)
    for r in rows:
        g2[r['sex']].append(r)
    for k, g in sorted(g2.items(), key=lambda x: -med(x[1])):
        c = sum(r['iv']['花费'] for r in g)
        rv = sum(r['iv']['收入'] or 0 for r in g)
        print(f'  {k:<8}{len(g):>2} 条　ROAS 中位 {med(g):.3f}　'
              f'合并 ROAS {(rv / c if c else 0):.4f}　花费 ${c:>6.0f}　'
              f'{" ".join(r["t"] for r in g)}')

    print(f'\n=== 阶层落差段占比 × ROAS ===')
    has = [r for r in rows if r['gapf'] is not None]
    for lo, hi, name in ((0, 0.001, '一段都没有'), (0.001, 0.34, '不到三分之一'),
                         (0.34, 0.67, '三分之一到三分之二'), (0.67, 1.01, '三分之二以上')):
        g = [r for r in has if lo <= r['gapf'] < hi]
        if not g:
            continue
        c = sum(r['iv']['花费'] for r in g)
        rv = sum(r['iv']['收入'] or 0 for r in g)
        print(f'  {name:<12}{len(g):>2} 条　ROAS 中位 {med(g):.3f}　'
              f'合并 ROAS {(rv / c if c else 0):.4f}　花费 ${c:>6.0f}　'
              f'{" ".join(r["t"] for r in g)}')

    print(f'\n=== 各组的档位分布 ===')
    for grp in ('AI', '剪辑', '运营'):
        g = [r for r in rows if r['iv']['组'] == grp]
        if not g:
            continue
        print(f'  {grp:<4} 亲密档中位 {st.median([r["a"] for r in g]):.1f}　'
              f'暴力档中位 {st.median([r["v"] for r in g]):.1f}　'
              f'羞辱档中位 {st.median([r["h"] for r in g]):.1f}　'
              f'性别组合 {dict(Counter(r["sex"] for r in g))}')

    for kind in TABLES:
        c = Counter(a for r in rows for a in r['acts'][kind])
        if not c:
            continue
        print(f'\n=== 出现最多的{kind}行为 ===')
        for k, n in c.most_common(10):
            g = [r for r in rows if k in r['acts'][kind]]
            print(f'  {k:<18}{n:>3} 条　这些素材 ROAS 中位 {med(g):.3f}')

    # 词表以外的行为名。这是词表的验收指标：出现得多说明词表要扩。
    unk = unknown(rows)
    if unk:
        print(f'\n=== 词表以外的行为名 ===')
        for kind, c in unk.items():
            print(f'  {kind}：' + '　'.join(f'{k}×{n}' for k, n in c.most_common()))
    else:
        print(f'\n词表以外的行为名：无')

    if FIXED:
        print(f'\n=== 纠过的错别字 ===')
        for k, n in FIXED.most_common():
            print(f'  {k}　{n} 处')


if __name__ == '__main__':
    main()
