# -*- coding: utf-8 -*-
"""把拆解的十二步串成一次 Part 分析。

研究管线原来靠 batch.py 用 subprocess 逐步调 prep.py 和 call.py。装进 app 之后
不能那么干：这些模块用相对导入拿提示词和 ocrbin 的路径，单独起进程跑会找不到。
所以这里直接进程内调它们的函数，顺带省掉十二次起解释器的开销。

每一步做完都在工作目录里留下产物，跑到一半失败再点一次重新分析，已经做完的
步骤直接跳过——跟 batch.py 的续跑是同一套判断，靠产物在不在，不另记状态。

整套是同步阻塞的（ffmpeg、OCR、模型调用都是），调用方要丢进线程池，别在事件
循环里直接 await。
"""
import concurrent.futures as cf
import glob
import json
import os

from . import call as C
from . import prep
from . import score
from . import hl
from .装配 import 装配
from .环境 import 拆解失败

# 键名要跟前端的 AI_STAGE_LABEL 对上，对不上会显示成「处理中」
步骤 = (
    ('subtitle',   '认字幕'),
    ('shots',      '找镜头'),
    ('cutting',    '切段'),
    ('describing', '逐段描述'),
    ('structure',  '理结构'),
    ('rating',     '给段打分'),
)

# 研究管线的第三轮「整条呈现形式」默认不跑。它出字幕形式、语音形式、结尾字卡、
# 长镜头占比、题材一级二级等 26 个整条级字段，界面和模板匹配一个都不读；跑一次
# 要传整条的帧加片尾 30 秒视频，三遍，约 4 分钟。下游两处读它都带了文件存在判断，
# 不跑只会让那几个字段留空，不会断。要它就把 跑整条 设成 True。
跑整条 = False


def 工作目录(mp4: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(mp4)),
                     os.path.splitext(os.path.basename(mp4))[0] + '_拆解')
    os.makedirs(d, exist_ok=True)
    return d


def _轮二做完了(wd: str) -> bool:
    """逐段描述没有单一产物，按段数对齐判断做没做完。"""
    b = os.path.join(wd, 'round2', 'bounds.json')
    if not os.path.exists(b):
        return False
    try:
        n = len(json.load(open(b, encoding='utf-8'))['段边界']) - 1
    except (KeyError, ValueError, json.JSONDecodeError):
        return False
    return len(glob.glob(os.path.join(wd, 'round2', 'seg*_out.json'))) >= n


def 拆一个Part(mp4: str, 报阶段=None, 跑整条=跑整条) -> dict:
    """跑完十二步，返回 structure.json 的内容。

    报阶段(键, 名) 在每个阶段开始时调一次，界面拿它显示卡在哪一步。
    """
    if not os.path.exists(mp4):
        raise 拆解失败(f'找不到 Part 视频 {mp4}')
    wd = 工作目录(mp4)
    账 = C.新账本()
    C.当前账本.set(账)
    sub = os.path.join(wd, 'sub.tsv')
    有 = lambda *p: os.path.exists(os.path.join(wd, *p))

    def 到(键):
        if 报阶段:
            报阶段(键, dict(步骤)[键])

    # 认字幕和找镜头都是本地 ffmpeg 加 OCR，谁也不吃谁的产物，串着跑纯粹是排队。
    # 找镜头先丢进后台，认字幕在前台跑，等切段那一步之前把它收回来——
    # 一个 6 分钟的 Part 这样能省下找镜头那一分钟。
    到('subtitle')
    with C._Pool(1) as ex:
        镜头活 = ex.submit(prep.cmd_detect, mp4) if not 有('detect.json') else None
        if not 有('sub.tsv'):
            prep.cmd_sub(mp4)
        到('shots')
        if 镜头活:
            镜头活.result()      # 出错在这里抛，不会被吞掉

    到('cutting')
    if os.environ.get('LOCAL_CUT', '1') == '1':
        # 不问模型，按黑场和镜头切点本机切段（LOCAL_CUT=0 改回问模型）
        if not 有('segs.json'):
            prep.cmd_local_cut(mp4, sub)
    else:
        if not 有('round1_input.txt'):
            prep.cmd_round1(mp4, sub)
        if not 有('segs.json'):
            C.c_round1(wd)

    到('describing')
    if not 有('round2', 'bounds.json'):
        prep.cmd_round2(mp4, sub, os.path.join(wd, 'segs.json'))
    扫窗把手 = None
    if not _轮二做完了(wd):
        # 提前开扫的窗口在后台接着跑，这里只拿到把手，等到整条脉络发出去之后再收
        扫窗把手 = C.c_round2(wd)
    # 从这里往下任何一步出错，都要把还开着的线程池收掉再抛出去。漏一个池，
    # 解释器退出时会去 join 它的线程，而一次模型调用最长等 1800 秒。
    打分池 = None
    try:
        return _理结构到打分(wd, mp4, 到, 有, 账, 扫窗把手)
    finally:
        C.收扫窗(扫窗把手)


def _理结构到打分(wd, mp4, 到, 有, 账, 扫窗把手):
    打分池 = None
    if 跑整条:
        if not 有('round3_input.txt'):
            prep.cmd_round3(mp4)
        if not 有('round3_out.json'):
            C.c_round3(wd)

    到('structure')
    if not 有('stage1.json'):
        prep.cmd_post1(wd)
    # 给段打分只读逐段描述，不读理结构的产物，却排在理结构后面串行跑，白等 40 多秒。
    # 在这里先丢到后台打（PAR_RATING=0 关），理结构跑完再把分并回 structure.json。
    先打的 = None
    if os.environ.get('PAR_RATING', '1') == '1' and not 有('structure.json'):
        打分池 = C._Pool(1)
        先打的 = 打分池.submit(hl.先打, os.path.join(wd, 'stage1.json'))
    try:
        if not 有('stage2_input.txt'):
            prep.cmd_stage2(wd)
        if not 有('stage2_out.json'):
            C.c_stage2(wd, 扫窗把手)
        else:
            C.收扫窗(扫窗把手)
        if not 有('structure.json'):
            prep.cmd_post2(wd)
        if 先打的 is not None:
            try:
                hl.并入(os.path.join(wd, 'structure.json'), 先打的.result())
            except Exception as e:
                # 并行打分失败不拦路，下面按老路子补打
                print(f'  并行打分没成（{type(e).__name__}: {e}），改为串行补打')
    finally:
        # 理结构是这条管线最常失败的一步，它一失败就漏一个打分池，而池里还套着
        # 一个四线程的池在给每段打分，钱照烧，退出照样被卡住。
        if 打分池 is not None:
            打分池.shutdown(wait=False)

    f = os.path.join(wd, 'structure.json')
    if not os.path.exists(f):
        raise 拆解失败('六步都跑完了，却没拿到拆解结果。重新分析一次这个 Part。')

    到('rating')
    # 四个维度的分打在段上，段之间互不依赖，按 Part 跑没问题。
    # 剧情分和尺度分不在这里：它们的权重按整库的稀缺度算，一个 Part 才几段，
    # 算出来的权重没意义，要等整个项目的 Part 都跑完再统一跑一次。
    d = json.load(open(f, encoding='utf-8'))
    if any(s.get('抓人速度') is None for s in (d.get('段') or [])):
        hl.跑(f)
        d = json.load(open(f, encoding='utf-8'))
    账['费用美元'] = C.money(账['输入'], 账['输出'] + 账['思考'])
    账['模型'] = C.MODEL
    d['_token账'] = 账
    return d


def 全项目打分(根: str) -> dict:
    """所有 Part 拆完之后跑一次：给剧情点打剧情分和尺度分，并段成剧情片段。

    权重按取值在整库里的稀缺度定，越少见的取值分越高。所以必须一次看全项目的
    段，按 Part 分开跑会让每个 Part 各算一套权重，互相之间没法比。

    就地改写各个 Part 的 structure.json，返回 {Part 工作目录: 装配好的段列表}。
    """
    docs, segs, 字幕, 镜头 = score.收(根)
    if not segs:
        return {}
    score.打分(segs, 字幕, 镜头)
    出 = {}
    for f, d in docs:
        json.dump(d, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        出[os.path.dirname(f)] = 装配(d)
    return 出
