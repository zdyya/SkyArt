# -*- coding: utf-8 -*-
"""按剪辑模板从段库里挑段、排出成片计划。

移植自 Work/creative-decompose/demo/core.py，算法和分值照 demo/契约.md，
数据源从扫目录读 structure.json 换成查 admake_segments。

产出的计划表跟高光钩子那条路同形状（file_path / start / end / label /
part_name / score），后面的裁切、拼接、命名、日志、重试原样复用。
"""
import statistics

# 内容条件的分值。改这里等于改模板的匹配口径，改前先看 demo/契约.md 第三节。
分值 = {
    '最强的一刻属于哪一类': 3,
    '主角在这一刻是': 2,
    '本能刺激': 2,
    '高转化场景': 1,
    '有没有旁人在看': 1,
    '阶层落差': 1,
}
结构分值 = {
    '最强的一刻位置': 1,
    '结尾留没留悬念': 1,
    '事件密度下限': 1,
    '台词密度下限': 1,
}

# 模板用的是拆解管线那套字段名，段库里存的是界面那套。差异只在这张表上。
模板名到库名 = {
    '最强的一刻属于哪一类': '这段算哪种戏',
    '主角在这一刻是': '主角是动手还是挨打',
    '本能刺激': '本能刺激',
    '高转化场景': '哪种经典桥段',
    '有没有旁人在看': '有没有旁人在看',
    '阶层落差': '有没有身份差距',
}


def _要的值(v):
    """枚举条件写数组是取其一，写字符串是必须等于它。统一成集合。"""
    if v is None:
        return None
    return set(v) if isinstance(v, (list, tuple, set)) else {v}


def 段字典(seg, part, 序号) -> dict:
    """把一行 AdmakeSegment 摊成算分用的字典。

    起止用的是 Part 内的绝对秒，跟播放器、导轨和裁切是同一条时间轴。
    """
    f = seg.fields or {}
    峰 = f.get('最强的一刻时间')
    时长 = float(seg.duration_sec or 0) or 1.0
    前摇 = ((float(峰) - float(seg.start_sec)) / 时长) if 峰 is not None else 0.5
    刺激 = f.get('本能刺激')
    if isinstance(刺激, str):
        刺激 = [刺激]
    return {
        '段ID': f'P{序号}-{seg.seg_index}',
        'id': seg.id,
        'part_id': seg.part_id,
        'part_name': getattr(part, 'name', '') or '',
        'file_path': getattr(part, 'file_path', None),
        '起': float(seg.start_sec or 0),
        '止': float(seg.end_sec or 0),
        '段时长': 时长,
        '这段算哪种戏': seg.seg_type or None,
        '段的尺度档': int(seg.intensity or 0),
        '前摇占比': max(0.0, min(1.0, 前摇)),
        '主角是动手还是挨打': f.get('主角是动手还是挨打'),
        '本能刺激': list(刺激 or []),
        '哪种经典桥段': f.get('哪种经典桥段'),
        '有没有旁人在看': f.get('有没有旁人在看'),
        '有没有身份差距': f.get('有没有身份差距'),
        '结尾留没留悬念': f.get('结尾留没留悬念'),
        '每分钟几件事': f.get('每分钟几件事'),
        '每分钟几句台词': f.get('每分钟几句台词'),
        '抓人速度': f.get('抓人速度'),
        '情绪强度': f.get('情绪强度'),
        '离开上下文看不看得懂': f.get('离开上下文看不看得懂'),
        '一句话': f.get('最强的一刻') or (f.get('段落叙述') or '')[:40],
    }


def 段分配(格们, 目标时长, 库):
    """时长摊到每一段上，不摊到每一格上。

    模板的格数来自爆款素材，跟这部剧的段长分布无关；按格摊会摊出段库够不着的区间。
    """
    M = statistics.median([s['段时长'] for s in 库]) if 库 else 0
    M = M or 1.0
    N = max(len(格们), 1)
    装得下 = max(1, int(目标时长 / M + 0.5))
    段数上限 = N * 4
    K = min(max(装得下, N), 段数上限)
    底, 余 = divmod(K, N)
    取几段 = [底 + (1 if i < 余 else 0) for i in range(N)]
    for i, g in enumerate(格们):
        取几段[i] = max(取几段[i], g.get('取几段') or 1)
    总段数 = sum(取几段)
    每段 = 目标时长 / 总段数
    return {'中位段长': round(M, 1), '装得下几段': 装得下, '段数上限': 段数上限,
            '总段数': 总段数, '每格取几段': 取几段,
            '每段预算': [round(每段 * 0.6, 1), round(每段 * 1.4, 1)]}


def 筛段(库, 格, 预算):
    """两个硬条件：尺度档不超过这一格的上限；段时长落在预算区间内。"""
    上限 = (格.get('内容条件') or {}).get('尺度档上限')
    上限 = 4 if 上限 is None else 上限
    lo, hi = 预算
    return [s for s in 库 if s['段的尺度档'] <= 上限 and lo <= s['段时长'] <= hi]


# 主角立场有两种写法：拆解管线原始产物写「施加方／承受方」，装配层映射成
# 「动手／挨打」存进段库，模板里写的也是后者。段库里混进旧写法时照样要能对上。
主角归一 = {'施加方': '动手', '承受方': '挨打'}


def 对得上(段, k, 要):
    if k == '本能刺激':
        return bool(set(段['本能刺激']) & 要)
    v = 段.get(模板名到库名[k])
    if k == '主角在这一刻是':
        v = 主角归一.get(v, v)
        要 = {主角归一.get(x, x) for x in 要}
    return v in 要


def 结构对得上(段, k, 要):
    if k == '最强的一刻位置':
        p = 段['前摇占比']
        实 = '前四分之一' if p < 0.25 else ('后四分之一' if p > 0.75 else '中间')
        return 实 == 要
    if k == '结尾留没留悬念':
        return 段['结尾留没留悬念'] == 要
    # 密度类下限：没有硬字幕的剧台词密度是空的，这种情况跳过这一条，
    # 不要当成 0 判不通过——那会让整部剧每一段都卡在台词密度上。
    if k == '事件密度下限':
        v = 段['每分钟几件事']
        return True if v is None else float(v) >= 要
    if k == '台词密度下限':
        v = 段['每分钟几句台词']
        return True if v is None else float(v) >= 要
    return False


def 打分(段, 格):
    """匹配度＝这一格声明的条件里满足了多少。模板里缺省的项不计入分母。"""
    c = 格.get('内容条件') or {}
    q = 格.get('结构条件') or {}
    得, 满 = 0, 0
    for k, w in 分值.items():
        要 = _要的值(c.get(k))
        if 要 is None:
            continue
        满 += w
        if 对得上(段, k, 要):
            得 += w
    for k, w in 结构分值.items():
        要 = q.get(k)
        if 要 is None:
            continue
        满 += w
        if 结构对得上(段, k, 要):
            得 += w
    return 1.0 if not 满 else round(得 / 满, 3)


def 排序键(段, 格, 第一格, 末格=False):
    """先按匹配度降序，同分再按位置排。

    第一格看抓人速度——前三秒决定观众还往不往下看；末格看情绪强度——收尾要靠
    一个情绪高点停住；中间格按事件密度。没打过分的段退回代理指标。
    """
    if 第一格:
        序 = (-(段.get('抓人速度') or 0), -(段.get('离开上下文看不看得懂') or 0), 段['前摇占比'])
    elif 末格:
        序 = (-(段.get('情绪强度') or 0), -(float(段['每分钟几件事'] or 0)))
    else:
        序 = (-(float(段['每分钟几件事'] or 0)),)
    return (-打分(段, 格), 序, 段['段ID'])


条件叫法 = {'最强的一刻属于哪一类': '段类型', '主角在这一刻是': '主角',
            '本能刺激': '本能刺激', '高转化场景': '经典桥段',
            '有没有旁人在看': '旁人', '阶层落差': '身份差距'}


def 条件说法(k, 要):
    值 = '或'.join(str(x) for x in sorted(要, key=str))
    if k == '有没有旁人在看':
        return '要有旁人在看' if 值 == '有' else '要没有旁人在看'
    if k == '阶层落差':
        return '要有身份差距' if 值 == '有' else '要没有身份差距'
    return f'{条件叫法.get(k, k)}要{值}'


def 条件一句话(格):
    c = 格.get('内容条件') or {}
    出 = [条件说法(k, _要的值(c[k])) for k in 分值 if _要的值(c.get(k)) is not None]
    档 = c.get('尺度档上限')
    if 档 is not None:
        出.append(f'尺度不超过档 {档}')
    return '，'.join(出) or '不挑'


def 内容对不上(候选, 格):
    """这一格声明了、但候选段里一段都对不上的条件。候选为空时不列。"""
    if not 候选:
        return []
    c = 格.get('内容条件') or {}
    出 = []
    for k in 分值:
        要 = _要的值(c.get(k))
        if 要 is None:
            continue
        if not any(对得上(s, k, 要) for s in 候选):
            出.append(条件说法(k, 要))
    return 出


def 筛空了怎么办(库, 格, 预算, 总段数):
    """某一格一段都筛不出来时，比预算区间和段库的段长范围，给出该怎么办。"""
    lo, hi = 预算
    if not 库:
        return '这个项目还没有拆出段，先跑一次 AI 分析。'
    最短 = min(s['段时长'] for s in 库)
    最长 = max(s['段时长'] for s in 库)
    if hi < 最短:
        要多长 = -(-int(最短 * 总段数 / 0.6 * 100) // 100)
        return (f'这一格要 {lo:.0f} 到 {hi:.0f} 秒的段，这部剧最短的段是 {最短:.1f} 秒。'
                f'目标时长改到 {要多长} 秒以上，或者换一个格数少的模板。')
    if lo > 最长:
        能到多长 = int(最长 * 总段数 / 0.6)
        return (f'这一格要 {lo:.0f} 到 {hi:.0f} 秒的段，这部剧最长的段是 {最长:.1f} 秒。'
                f'换一个格数多的模板，或者把目标时长降到 {能到多长} 秒以下。')
    档 = (格.get('内容条件') or {}).get('尺度档上限')
    档 = 4 if 档 is None else 档
    return (f'这一格要 {lo:.0f} 到 {hi:.0f} 秒的段、尺度不超过档 {档}，'
            f'这部剧里没有同时满足的段。')


def 算匹配(库, 格们, 目标时长, 要几条=3):
    """导出前的预检：每一格有多少候选、最高匹配度多少、推荐导几条。"""
    分配 = 段分配(格们, 目标时长, 库)
    预算 = 分配['每段预算']
    每格, 撑得起 = [], []
    for i, g in enumerate(格们):
        取几段 = 分配['每格取几段'][i]
        cand = 筛段(库, g, 预算)
        cand.sort(key=lambda s: 排序键(s, g, i == 0, i == len(格们) - 1))
        分 = sorted((打分(s, g) for s in cand), reverse=True)
        够格 = sum(1 for x in 分 if x >= 0.6)
        撑得起.append(((够格 or len(分)) * 3) // max(取几段, 1))
        每格.append({
            '序': g.get('序', i + 1),
            '格名': g.get('格名') or f'第{i + 1}格',
            '条件一句话': 条件一句话(g),
            '取几段': 取几段,
            '时长预算': 预算,
            '通过硬条件': len(cand),
            '最高匹配度': 分[0] if 分 else None,
            '六成以上': 够格,
            '内容对不上的条件': 内容对不上(cand, g),
            '候选': [{'段ID': s['段ID'], '匹配度': 打分(s, g),
                      '段时长': round(s['段时长'], 1), '一句话': s['一句话']}
                     for s in cand[:10]],
        })
    拦, 因 = False, None
    空 = [(i, x) for i, x in enumerate(每格) if x['通过硬条件'] == 0]
    if 空:
        i, x = 空[0]
        拦 = True
        因 = (f'第 {x["序"]} 格「{x["格名"]}」一段都筛不出来。'
              + 筛空了怎么办(库, 格们[i], 预算, 分配['总段数']))
    推荐 = 0 if 拦 else min(10, min(撑得起) if 撑得起 else 0)
    return {'每格': 每格, '推荐条数': 推荐, '拦住了吗': 拦, '拦住的原因': 因,
            '总段数': 分配['总段数'], '中位段长': 分配['中位段长']}


def 选段(库, 格们, 目标时长, 要几条=3):
    """同一条成片内段不重复；不同成片可以共用段，同一个段最多进 3 条。"""
    分配 = 段分配(格们, 目标时长, 库)
    预算 = 分配['每段预算']
    排好 = []
    for i, g in enumerate(格们):
        cand = 筛段(库, g, 预算)
        cand.sort(key=lambda s: 排序键(s, g, i == 0, i == len(格们) - 1))
        排好.append(cand)
    名次 = [{x['段ID']: j for j, x in enumerate(c)} for c in 排好]

    用过, 成片 = {}, []
    for 条 in range(1, 要几条 + 1):
        本条 = set()
        选中 = [[] for _ in 格们]

        def 挑一段(i):
            剩 = [s for s in 排好[i] if s['段ID'] not in 本条 and 用过.get(s['段ID'], 0) < 3]
            if not 剩:
                剩 = [s for s in 排好[i] if s['段ID'] not in 本条]
            if not 剩:
                return None
            # 用得少的排前面，几条成片才不会长一个样；候选用完了才回头复用
            剩.sort(key=lambda s: (用过.get(s['段ID'], 0), 名次[i][s['段ID']]))
            s = 剩[0]
            本条.add(s['段ID'])
            用过[s['段ID']] = 用过.get(s['段ID'], 0) + 1
            选中[i].append(s)
            return s

        # 先给每一格各留一段，再把剩下的名额分下去。不先留，候选少的时候前面的格
        # 会把段全占光，后面的格一段都填不上，整格从成片里消失。
        for i in range(len(格们)):
            挑一段(i)
        for i in range(len(格们)):
            for _ in range(分配['每格取几段'][i] - len(选中[i])):
                if 挑一段(i) is None:
                    break

        段们 = []
        for i, g in enumerate(格们):
            for s in 选中[i]:
                段们.append({**s, '填的是哪一格': g.get('格名') or f'第{i + 1}格',
                            '匹配度': 打分(s, g)})
        if not 段们:
            break
        成片.append({'序': 条, '总时长': round(sum(x['段时长'] for x in 段们), 1), '段': 段们})
    return 成片


def 排成计划(段们) -> list[dict]:
    """把选出来的段排成 _run_export_multi 认的计划表。

    它只读 file_path、start、end 三个键，_write_export_log 另读 label、
    part_name、score。形状对上就能原样复用后面整条出片链路。
    """
    return [{
        'file_path': s['file_path'],
        'part_id': s['part_id'],
        'start': round(s['起'], 2),
        'end': round(s['止'], 2),
        'label': 'segment',
        'part_name': s['part_name'],
        'score': round(s['匹配度'] * 10, 1),
        'seg_id': s['段ID'],
        'slot': s['填的是哪一格'],
    } for s in 段们]
