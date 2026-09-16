# -*- coding: utf-8 -*-
"""给每一段打四个维度的分。

第三步。输入是第二步拆出的段落记录，输出四个 1 到 10 的分，写回 structure.json。
提示词在 Work/prompts/短剧段落打分_提示词.md。

分打在段上，不打在段内的小片段上：段的中位时长 40 秒，「撑不撑得住」在十几秒
的片段上没有意义。这四个分替掉模板里原来的候选排序——那些写的是「台词密度从
高到低」「段时长靠近 40 秒」这类代理指标，在猜哪一段更好。

按段调用，一段一次。段之间没有依赖，并发跑。

用法：
    python3 hl.py <段库目录> [集号...]      # 不给集号就跑整个目录
    python3 hl.py <段库目录> --看           # 只看已有的分，不调模型
"""
import concurrent.futures as cf
import glob
import json
import os

from .环境 import 拆解失败
import sys

from . import call as C

PAR = int(os.environ.get('HL_PAR', '4'))
THINK = 3072          # 只做判断不做检索，给固定额度
HEAD = 3.0            # 「抓人速度」看开头几秒
维 = ('抓人速度', '撑不撑得住', '情绪强度', '离开上下文看不看得懂')

C.SCHEMA['seg'] = [维]

P = lambda: C.block('短剧段落打分_提示词.md', '## 提示词')


def 事件们(s):
    """时间统一成集内绝对秒。事件的「时间」是段内相对秒，段的起止是绝对秒，
    混着摆等于拿两个钟比。930 条事件都带「绝对时间」，跟段起加相对秒差在 1 秒内。"""
    out = []
    起 = s.get('_起') or 0
    for e in s.get('事件') or []:
        t = e.get('绝对时间')
        if t is None and e.get('时间') is not None:
            t = 起 + e['时间']
        if t is not None:
            out.append((float(t), e))
    return out


def 一行(t, e):
    行 = [f'{t:>6.1f}s', '画面：' + (e.get('画面') or '').strip()]
    if e.get('台词'):
        行.append('台词：' + str(e['台词']).strip())
    旁 = []
    if e.get('在场人数') is not None:
        旁.append(f'在场 {e["在场人数"]} 人')
    if e.get('有旁人在看吗') == '有':
        旁.append('有旁人在看')
    if 旁:
        行.append('现场：' + '，'.join(旁))
    return ' | '.join(行)


def 开头表(s):
    """段起点后 3 秒内的记录。

    让模型自己回完整记录里找开头，实测 9 条里 3 条找错——那 3 条窗内本来就
    没有记录，规则写了该填 null，模型还是往外抓了最近的一条。摆出来它就没得抓。
    事件的中位间隔 2.4 秒，3 秒窗有 17% 是空的；紧邻段起点之前那一条描述的画面
    延续到开头，补进来覆盖到 100%。
    """
    ev = 事件们(s)
    起 = s.get('_起') or 0
    窗 = [(t, e) for t, e in ev if 起 <= t <= 起 + HEAD]
    前 = [(t, e) for t, e in ev if t < 起]
    out = []
    if 前 and (not 窗 or 前[-1][0] > 起 - HEAD):
        out.append(一行(*前[-1]) + '（这个镜头延续到开头）')
    out += [一行(t, e) for t, e in 窗]
    return '\n'.join(out) or '无记录'


def 事件表(s):
    return '\n'.join(一行(t, e) for t, e in 事件们(s))


def 拼(s):
    return (f'这一场讲的是：{s.get("段落叙述") or ""}\n\n'
            f'开头 3 秒：\n{开头表(s)}\n\n'
            f'这一场的完整记录：\n{事件表(s)}')


def 打一段(wd, s):
    r = C.gen(P(), text=拼(s), thinking=THINK,
              tag=f'seg:{os.path.basename(wd)}#{s.get("段序号")}')
    for k in 维:
        v = r.get(k)
        s[k] = int(v) if isinstance(v, (int, float)) else None
    s['观众会缺哪个信息'] = r.get('观众会缺哪个信息')
    s['这一场为什么留得住人'] = r.get('这一场为什么留得住人')
    return 1 if any(s.get(k) is not None for k in 维) else 0


def 跑(f):
    d = json.load(open(f, encoding='utf-8'))
    wd = os.path.dirname(f)
    segs = d.get('段') or []
    with cf.ThreadPoolExecutor(PAR) as ex:
        n = sum(ex.map(lambda s: 打一段(wd, s), segs))
    json.dump(d, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'{os.path.basename(wd)}　{len(segs)} 段　打分 {n} 段')
    return n


def 看(root):
    for f in sorted(glob.glob(os.path.join(root, '*_拆解', 'structure.json'))):
        d = json.load(open(f, encoding='utf-8'))
        for s in d.get('段') or []:
            if all(s.get(k) is None for k in 维):
                continue
            print(f'{os.path.basename(os.path.dirname(f))[:3]} 段{s.get("段序号")}　'
                  f'{s.get("_起"):>6.1f}→{s.get("_止"):<6.1f}　'
                  + '　'.join(f'{k[:2]}{s.get(k)}' for k in 维)
                  + f'　{s.get("这一场为什么留得住人") or ""}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root = sys.argv[1]
    if '--看' in sys.argv:
        看(root)
        sys.exit(0)
    集 = [a for a in sys.argv[2:] if not a.startswith('--')]
    fs = sorted(glob.glob(os.path.join(root, '*_拆解', 'structure.json')))
    if 集:
        fs = [f for f in fs if any(os.path.basename(os.path.dirname(f)).startswith(e) for e in 集)]
    if not fs:
        raise 拆解失败('这个目录下没有拆解产物')
    print(f'{len(fs)} 个拆解，模型 {C.MODEL}，段内并发 {PAR}')
    总 = sum(跑(f) for f in fs)
    print(f'\n一共打了 {总} 段')
    C.report()
