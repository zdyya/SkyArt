#!/usr/bin/env python3
"""qwen 调用层。

分析只用一种模型：qwen，走阿里云白名单通道直连，不经代理。每个请求都带绿网
免审头，输入输出都不过平台审核——素材是海外短剧，亲密和暴力场景占比高，过审
会把整段拦掉。通道地址和头的写法见 Work/阿里云白名单调用规范.md。

默认模型 qwen3.8-flash，默认每处调用跑 3 遍取并集。用 MODEL 和 RUNS 覆盖，
MODEL 只接受 qwen 开头的名字。

用法：
  call.py round1  <work_dir>            跑切段
  call.py round2  <work_dir>            跑逐段描述（按段循环）
  call.py round3  <work_dir>            跑整条呈现形式
  call.py stage2  <work_dir>            跑结构推理
  call.py cite    <work_dir>            只重跑依据核对
  call.py all     <work_dir>            四步连跑，中间自动接 prep.py

key 从环境变量 QWEN_WS_KEY 读，或写进 ~/.happyhorse.env 的 HAPPYHORSE_API_KEY。

视频和帧图片都走内联 base64，不上传、不经中转。
"""
import base64, glob, json, os, re, subprocess, sys, time
import concurrent.futures as cf
import threading

import httpx

# 输出改成行缓冲。管道里不是 tty 时 Python 默认块缓冲，攒到 8KB 才写盘，
# 长任务的进度要等几分钟才看得到，只能拿 lsof 猜它在干什么。
sys.stdout.reconfigure(line_buffering=True)

from .环境 import 拆解失败, 提示词目录, 分析模型, qwen_key as _环境key

BASE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = 提示词目录()


class BadSchema(Exception):
    """字段名被改了。重试一次可能就对了，所以跟 Blocked 分开。"""


class NoCredit(Exception):
    """余额耗尽。跟限速要分开：等多久都不会好，重试只是白烧时间。"""


class Blocked(Exception):
    """被安全策略拦了。跟调用失败要分开：同样的内容重试没有意义。"""


# 拿 W27 对照过 3.1-pro-preview 和 3.8-flash：引用核查指标持平（编造引用
# 都是 0，时间偏差中位都是 0.2 秒），flash 的剧情点召回少三成，价格低得多。
MODEL = 分析模型()
if not MODEL.startswith('qwen'):
    raise RuntimeError(f'MODEL 只能是 qwen 开头的名字，现在是 {MODEL}')
RUNS = int(os.environ.get('RUNS', '3'))

# 阿里云 MaaS 白名单通道，美国 us-east-1。调用规范见 Work/阿里云白名单调用规范.md。
QWEN_HOST = 'https://ws-bljb9l73522yj7vp.us-east-1.maas.aliyuncs.com'
QWEN_MM = QWEN_HOST + '/api/v1/services/aigc/multimodal-generation/generation'
QWEN_TXT = QWEN_HOST + '/compatible-mode/v1/chat/completions'
QWEN_INSPECT = '{"input":"disable","output":"disable"}'
# 视频走 base64 内联，超过这个大小先转码。90 秒 480p crf28 实测 2.4 MB。
# 视频要 base64 塞进请求体，编码后涨三分之一，同一个请求里还有十几张帧。
# 阈值按源文件大小定，所以要留出这两份余量：4 MB 的源片编码完约 5.3 MB，
# 加上帧仍在服务端上限内。E09 的 7.79 MB 片尾卡在旧阈值 8 MB 以下没转码，
# 编码后 10.4 MB 被判 RequestTooLarge。
QWEN_VIDEO_MB = 4

# 每次调用的 token 用量都累到这里，跑完打一份账。价格是每百万 token 的美元数，
# 促销价到 2026-12-31，之后翻倍；换模型记得改这张表。
# 单位：美元/百万 token（输入，输出）。qwen 的思考 token 按输出价计，
# 官方没有单独的思考单价。us-east-1 走「美国弗吉尼亚」那一档，
# 跟北京/香港/法兰克福/东京同价，比新加坡便宜约四分之一。
# 来源：Alibaba Cloud Model Studio 定价页，2026-09-08 查。
# 白名单是专属工作区，实际结算以控制台为准。
# 通道里的其他 qwen 型号还没查过单价，不写进来——表里没有就只报 token 不报钱，
# 比填一个猜的数字强。
PRICE = {
    'qwen3.8-flash': (0.113, 0.382),
}
USE = {'调用次数': 0, '输入': 0, '输出': 0, '思考': 0, '按轮': {}}


BILL_LOCK = threading.Lock()      # 窗并发之后多个线程同时记账，不加锁会丢计数


def _bill(tag, pin, out, th):
    USE['调用次数'] += 1
    USE['输入'] += pin
    USE['输出'] += out
    USE['思考'] += th
    k = (tag or '?').split(':')[0]
    r = USE['按轮'].setdefault(k, {'次': 0, '输入': 0, '输出': 0, '思考': 0})
    r['次'] += 1
    r['输入'] += pin
    r['输出'] += out
    r['思考'] += th


def bill_qwen(tag, u):
    """记一次调用的用量。思考 token 藏在 output_tokens_details 里，按输出价计费，
    它通常比正式输出还多。"""
    pin = u.get('input_tokens') or 0
    out = u.get('output_tokens') or 0
    th = (u.get('output_tokens_details') or {}).get('reasoning_tokens') or 0
    with BILL_LOCK:
        _bill(tag, pin, max(out - th, 0), th)


def money(pin, out):
    """按当前模型的单价算钱。表里没有这个模型就返回 None，不要瞎报数字。"""
    pr = PRICE.get(MODEL)
    if not pr:
        return None
    return pin / 1e6 * pr[0] + out / 1e6 * pr[1]


def report():
    """打一份 token 账。没调用过就什么都不打。"""
    if not USE['调用次数']:
        return
    tot_out = USE['输出'] + USE['思考']
    print(f"\n── token 用量　{MODEL}　{USE['调用次数']} 次调用 ──")
    print(f"  {'':10}{'次':>4}{'输入':>12}{'输出':>10}{'思考':>10}{'费用':>9}")
    for k, r in sorted(USE['按轮'].items(), key=lambda x: -(x[1]['输入'])):
        c = money(r['输入'], r['输出'] + r['思考'])
        print(f"  {k:10}{r['次']:>4}{r['输入']:>12,}{r['输出']:>10,}{r['思考']:>10,}"
              + (f"{c:>9.4f}" if c is not None else f"{'—':>9}"))
    c = money(USE['输入'], tot_out)
    print(f"  {'合计':10}{USE['调用次数']:>4}{USE['输入']:>12,}{USE['输出']:>10,}{USE['思考']:>10,}"
          + (f"{c:>9.4f}" if c is not None else f"{'—':>9}"))
    if USE['输出']:
        print(f"  思考量是正式输出的 {USE['思考'] / USE['输出']:.1f} 倍")
    if c is None:
        print(f"  {MODEL} 不在单价表里，只报 token 不报钱")


def qwen_key():
    return _环境key()


# ---------- 提示词 ----------

def block(fname, heading):
    """取出某一节标题之后的第一个代码块。

    不能按下一个 `## ` 去切分节：提示词的代码块内部就有 ## 规则、## 输出格式
    这些子标题，按 ## 切会在围栏之前就截断。直接找标题后的第一对围栏。
    """
    s = open(os.path.join(PROMPTS, fname), encoding='utf-8').read()
    i = s.index(heading) + len(heading)
    m = re.search(r'\n```(?:\w*)\n(.*?)\n```', s[i:], re.S)
    if not m:
        raise 拆解失败(f'{fname} 的「{heading}」后面没找到代码块')
    return m.group(1).strip()


P1 = lambda: block('短剧素材拆解_提示词.md', '## 第一轮：切段')
P2 = lambda: block('短剧素材拆解_提示词.md', '## 第二轮：逐段详细描述')
P3 = lambda: block('短剧素材拆解_提示词.md', '## 第三轮：整条素材的呈现形式')
PR = lambda: block('短剧素材结构推理_提示词.md', '## 第一轮：整条脉络')
PS = lambda: block('短剧素材结构推理_提示词.md', '## 第二轮：逐段定性')
PM = lambda: block('短剧素材结构推理_提示词.md', '## 第三轮：悬念对答案')
PP = lambda: block('短剧素材结构推理_提示词.md', '## 第四轮：整条画像')

WIN = 4      # 第二轮每窗几段。窗口越小召回越高，调用次数也越多
# 一条素材内部同时跑几个窗。窗之间没有依赖，串行跑纯属浪费——W25 十一个窗
# 串着跑了 13 分钟。跟 batch.py 的素材并发是相乘关系，两个都调大会撞速率墙。
PAR = int(os.environ.get('SCAN_PAR', '4'))


# 每轮的思考预算。-1 是拉满，正整数是上限 token 数，0 是关掉。
# 实测思考 token 是正式输出的 3.7 倍，按输出价计费，所以这是成本主项。
# 前两轮要在长文本里做判断，拉满；后两轮只做匹配和比对，给固定额度。
# 调这里之前先跑一条对照，看引用核查指标降不降。
# qwen 这边：正整数走 thinking_budget（实测封得住，2048 把思考从 2261 压到
# 1307，耗时 43 秒降到 18 秒），0 走 enable_thinking=false，-1 不封顶。
# 切段和呈现形式只填枚举字段，不给上限会思考成百倍于答案：实测切段要 175 个
# token 的答案思考了 12,414 个，呈现形式要 77 个思考了 4,421 个。
# scan 这一轮从整条里逐段挑剧情点，是四轮里最需要想的。3072 实测顶格，
# 剧情点从 21 个掉到 11 个，所以单独给它宽额度。
THINK = {'stage2': 4096, 'scan': 12288, 'answer': 4096, 'profile': 4096,
         'round1': 2048, 'round2': 3072, 'round3': 1024}


# ---------- API ----------

def qwen_video_uri(path):
    """视频转成 base64 data URI。太大先转码，转码结果缓存在原文件旁边。"""
    src = path
    if os.path.getsize(src) > QWEN_VIDEO_MB * (1 << 20):
        small = os.path.splitext(path)[0] + '.q480.mp4'
        if not os.path.exists(small) or os.path.getmtime(small) < os.path.getmtime(path):
            print(f'    转码 {os.path.basename(path)} → 480p')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', path,
                            '-c:v', 'libx264', '-crf', '28', '-preset', 'veryfast',
                            '-vf', 'scale=-2:480', '-c:a', 'aac', '-b:a', '64k', small],
                           check=True)
        src = small
    b = base64.b64encode(open(src, 'rb').read()).decode()
    return 'data:video/mp4;base64,' + b


def upload(path):
    """视频转成 data URI 直接带在请求里，没有上传这一步。"""
    return qwen_video_uri(path), 'video/mp4'


def gen_uri(prompt, uri, mime, text='', thinking=None, tag=''):
    """视频已经转过一次 data URI，直接拿来再调一次，不重复读盘转码。"""
    return _gen([{'file_data': {'mime_type': mime, 'file_uri': uri}}],
                prompt, text, thinking, tag)


def gen(prompt, video=None, images=(), text='', thinking=None, tag=''):
    """调一次 generateContent，返回解析好的 JSON。"""
    parts = []
    if video:
        uri, mime = upload(video)
        parts.append({'file_data': {'mime_type': mime, 'file_uri': uri}})
    for p in images:
        parts.append({'inline_data': {
            'mime_type': 'image/jpeg',
            'data': base64.b64encode(open(p, 'rb').read()).decode()}})
    return _gen(parts, prompt, text, thinking, tag)


# 每个 tag 前缀必须出现的字段名。模型偶尔把字段名写错——实测 30 条里 5 条把
# 「凭什么算给了答案」写成「凭国家算给了答案」「凭手什么」「凭伤什么」。那一栏
# 恰好没人读所以没出事，但同样的事发生在「主角在这一刻是」上会静默变成 0，
# 而且不报错。所以收到 JSON 就核字段名。
# 每轮必须出现的字段名，按层。只列下游真正要读的——列多了会因为模型省略
# 一个没人用的字段而白重试。
#   顶层：('a', 'b')
#   数组里每一项：('数组名', ('项里必须有的字段', ...))
#   再往里一层：('数组名', '子数组名', ('子项必须有的字段', ...))
SCHEMA = {
    'round1': [('段', ('起',))],
    'round2': [('段序号', '事件', '段落叙述', '声音'),
               ('事件', ('时间', '画面'))],
    'round3': [('字幕形式', '语音形式', '结尾字卡形式')],
    'stage2': [('剧情时间线', '题材'),
               ('剧情时间线', ('段', '这一段推进了什么'))],
    'scan':   [('段',),
               ('段', ('段序号', '段里的剧情点', '亲密行为', '暴力行为', '羞辱行为',
                       '亲密双方性别', '阶层落差')),
               ('段', '段里的剧情点',
                ('时间', '哪一类', '有旁人在看吗', '主角在这一刻是', '高转化场景')),
               ('段', '亲密行为', ('行为', '时间')),
               ('段', '暴力行为', ('行为', '时间')),
               ('段', '羞辱行为', ('行为', '时间'))],
    'answer': [('悬念点',),
               ('悬念点', ('时间', '后面给答案了吗'))],
    'profile': [('男频女频', '主角是谁', '对手是谁', '对手数量', '核心矛盾',
                 '一句话故事', '开场做的是什么',
                 '主角处境曲线', '段的角色', '结构', '装配套路',
                 '剪辑手法', '结尾停在哪', '结构上的缺口', '抓人的点'),
                ('段的角色', ('段', '角色')),
                ('主角处境曲线', ('段', '处境')),
                ('结构', ('部分', '从第几段到第几段', '在做什么')),
                ('装配套路', ('套路', '在哪几段', '凭什么')),
                ('抓人的点', ('是什么', '段', '秒'))],
}


def check_schema(d, tag):
    """核字段名。缺了就抛 BadSchema，让上层重试一次。

    模型偶尔改字段名，实测 30 条里 5 条把「凭什么算给了答案」写成
    「凭国家算给了答案」这类。那一栏恰好没人读所以没出事，同样的事发生在
    「主角在这一刻是」上会静默变成 0，而且不报错。
    """
    k = (tag or '').split('#')[0].split(':')[0]
    rules = SCHEMA.get(k)
    if not rules or not isinstance(d, dict):
        return
    for rule in rules:
        if all(isinstance(x, str) for x in rule):          # 顶层
            miss = [f for f in rule if f not in d]
            if miss:
                raise BadSchema(f'{k} 顶层缺 {miss}，实际给了 {list(d)[:8]}')
        elif len(rule) == 2:                               # 数组里每一项
            arr, fs = rule
            for i, e in enumerate(d.get(arr) or []):
                if not isinstance(e, dict):
                    continue
                m = [f for f in fs if f not in e]
                if m:
                    raise BadSchema(
                        f'{k} 的 {arr}[{i}] 缺 {m}，实际给了 {list(e)[:8]}')
        else:                                              # 再往里一层
            arr, sub, fs = rule
            for i, e in enumerate(d.get(arr) or []):
                if not isinstance(e, dict):
                    continue
                for j, x in enumerate(e.get(sub) or []):
                    if not isinstance(x, dict):
                        continue
                    m = [f for f in fs if f not in x]
                    if m:
                        raise BadSchema(
                            f'{k} 的 {arr}[{i}].{sub}[{j}] 缺 {m}，'
                            f'实际给了 {list(x)[:8]}')


def _to_qwen(parts):
    """把 Gemini 形状的 parts 翻成 DashScope 的 content。"""
    out = []
    for p in parts:
        if 'text' in p:
            out.append({'text': p['text']})
        elif 'file_data' in p:
            fd = p['file_data']
            k = 'video' if fd['mime_type'].startswith('video') else 'image'
            out.append({k: fd['file_uri']})
        elif 'inline_data' in p:
            d = p['inline_data']
            out.append({'image': f"data:{d['mime_type']};base64,{d['data']}"})
    return out


def _qwen_max(budget):
    """max_tokens 要把思考额度算进去。3.8 系列的思考过程按 completion_tokens 计
    费，不显式给额度时思考会把默认上限吃掉，正文写到一半被截断——实测 JSON 断在
    第 2000 到 2600 个字符处，重试一次撞一次。按预算加 6000 给正文，封在 32768。
    """
    return min(32768, (budget or 4096) + 6000)


def _qwen_stream(url, hdr, body, media):
    """流式读一次 qwen 调用，返回 (错误码, 错误文本, 正文, 用量)。

    白名单网关对非流式响应在 298 秒切断（实测 elapsed_ms=298009），长视频那一轮
    经常超，重试撞的是同一面墙。开增量输出后 token 一直在流，网关的空闲计时不触发。
    两个端点的分片形状不同：多模态端点每片是完整响应对象，兼容端点是 OpenAI 的 delta。
    """
    hdr = dict(hdr)
    hdr['X-DashScope-SSE'] = 'enable'
    body = dict(body)
    if media:
        par = dict(body.get('parameters') or {})
        par['incremental_output'] = True
        body['parameters'] = par
    else:
        body['stream'] = True
        body['stream_options'] = {'include_usage': True}
    buf, usage = [], {}
    with httpx.stream('POST', url, headers=hdr, json=body, timeout=1800) as r:
        if r.status_code >= 400:
            raw = r.read().decode('utf-8', 'replace')
            try:
                d = json.loads(raw)
                return (d.get('code') or r.status_code,
                        str(d.get('message') or raw)[:220], None, None)
            except json.JSONDecodeError:
                return r.status_code, raw[:220], None, None
        for line in r.iter_lines():
            if not line.startswith('data:'):
                continue
            pay = line[5:].strip()
            if not pay or pay == '[DONE]':
                continue
            try:
                d = json.loads(pay)
            except json.JSONDecodeError:
                continue
            if d.get('code'):
                return d['code'], str(d.get('message'))[:220], None, None
            if media:
                ch = ((d.get('output') or {}).get('choices') or [{}])[0]
                c = (ch.get('message') or {}).get('content')
                if isinstance(c, list):
                    buf += [x.get('text') or '' for x in c]
                elif c:
                    buf.append(c)
                if d.get('usage'):
                    usage = d['usage']
            else:
                ch = (d.get('choices') or [{}])[0]
                piece = (ch.get('delta') or {}).get('content')
                if piece:
                    buf.append(piece)
                u = d.get('usage')
                if u:
                    usage = {'input_tokens': u.get('prompt_tokens'),
                             'output_tokens': u.get('completion_tokens'),
                             'output_tokens_details':
                                 u.get('completion_tokens_details') or {}}
    return None, None, ''.join(buf), usage


# 绿网关掉之后剩下的拦截来自模型自己。它不返回错误码，而是把拒绝的话当正文吐出来，
# 于是在 json.loads 那里变成 JSONDecodeError，跟网络抖动混在一起重试四次再退出，
# 整条任务被一段拒答拖死。认出来抛 Blocked，上游那几处 except 会跳过这一段接着跑。
拒答词 = ('could not process', 'cannot process', 'unable to process',
          'i cannot', "i can't", 'i am unable', 'as an ai',
          '无法处理', '无法提供', '不能提供', '抱歉，我', '很抱歉')
# 网关侧的内容拦截码。绿网头带上以后不该再出现，出现了也按拒答处理，重试没有意义。
拦截码 = ('DataInspectionFailed', 'ContentFilter', 'content_filter',
          'InappropriateContent', 'ResponseContentFilter')


def _拒答了吗(txt):
    t = (txt or '').strip().lower()
    if not t or t[0] in '{[':
        return False
    return any(w in t for w in 拒答词) or len(t) < 400


def _gen_qwen(parts, thinking, tag):
    content = _to_qwen(parts)
    media = [c for c in content if 'video' in c or 'image' in c]
    hdr = {'Authorization': f'Bearer {qwen_key()}', 'Content-Type': 'application/json',
           'X-DashScope-DataInspection': QWEN_INSPECT}
    think = thinking is None or thinking != 0
    budget = thinking if isinstance(thinking, int) and thinking > 0 else None
    if media:
        url = QWEN_MM
        par = {'enable_thinking': think, 'max_tokens': _qwen_max(budget)}
        if budget:
            par['thinking_budget'] = budget
        body = {'model': MODEL, 'input': {'messages': [{'role': 'user', 'content': content}]},
                'parameters': par}
    else:
        url = QWEN_TXT
        body = {'model': MODEL, 'temperature': 0, 'enable_thinking': think,
                'max_tokens': _qwen_max(budget),
                'messages': [{'role': 'user',
                              'content': ''.join(c['text'] for c in content)}]}
        if budget:
            body['thinking_budget'] = budget
    last = None
    for wait in (0, 30, 60, 120):
        if wait:
            print(f'    {wait} 秒后重试…（上一次：{last}）')
            time.sleep(wait)
        try:
            code, err, txt, usage = _qwen_stream(url, hdr, body, media)
            if code:
                last = f'{code} {err}'
                if str(code) in ('access_denied', 'Model.AccessDenied', 'InvalidApiKey'):
                    raise 拆解失败(f'{tag} 调用失败：{last}')
                if str(code) in 拦截码:
                    raise Blocked(f'网关拦了：{last}')
                continue
            bill_qwen(tag, usage)
            txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', (txt or '').strip())
            if _拒答了吗(txt):
                raise Blocked(f'模型拒答：{txt.strip()[:160]}')
            out = json.loads(txt)
            check_schema(out, tag)
            return out
        except Blocked:
            raise          # 同样的内容重试没有意义
        except BadSchema as e:
            last = f'BadSchema: {e}'
            print(f'    {tag} 字段名不对：{e}')
        except (httpx.HTTPError, KeyError, IndexError, TypeError,
                json.JSONDecodeError) as e:
            last = f'{type(e).__name__}: {e}'
    raise 拆解失败(f'{tag} 四次都失败：{last}')


def _gen(parts, prompt, text='', thinking=None, tag=''):
    parts = list(parts)
    parts.append({'text': prompt + ('\n\n' + text if text else '')})
    return _gen_qwen(parts, thinking, tag)


def _who(p):
    """人物的合并键：称呼里第一个称呼词。

    同一个人在三遍里可能写成「Leah」「Leah、姐姐」「Leah（女主）」,
    按 `首次出现` 的秒数去归并会把不同的人凑到一起——top2 里 59 条人物
    实际只有 38 个人。取顿号、括号、斜杠之前的第一段，精确相等才算同一人。
    """
    s = str(p.get('称呼') or '').strip()
    return re.split(r'[、,，/（(]', s)[0].strip()


def merge_people(runs):
    """人物按称呼首词合并，外观取最先出现的那一遍，首次出现取最早。"""
    seen = {}
    for r in runs:
        for p in r.get('人物') or []:
            key = _who(p)
            if not key:
                continue
            t = _tof(p)
            if key not in seen:
                seen[key] = dict(p, 称呼=key, _出现遍数=1)
            else:
                q = seen[key]
                q['_出现遍数'] += 1
                if t is not None and (q.get('首次出现') is None
                                      or t < float(q['首次出现'])):
                    q['首次出现'] = t
    return sorted(seen.values(), key=lambda p: _tof(p) or 0)


def _tof(e):
    for k in ('时间', '首次出现', '起'):
        if e.get(k) is not None:
            try:
                return float(e[k])
            except (TypeError, ValueError):
                return None
    return None


LISTS = ('事件', '屏幕上的文字', '反转', '爽点', '悬念点', '段')


def merge(runs):
    """同一次调用跑多遍，合并成一份。

    召回取并集：时间相差 1 秒内算同一时刻，漏报补不回来、多报能筛掉。

    本地不做语义合并。之前这里拿字符二元组重合率去认「重复措辞」，标定过
    三轮都失败：同一时刻语义相反的两句重合率 0.15，同义改写最低 0.19，
    分不开。词面相似度判不了语义，判错的代价是静默丢掉一条读法，下游
    根本不知道这里有分歧。

    所以只去掉一模一样的整条，剩下的全部保留，每条带 `_出现遍数`，同一
    时刻留下几条就标 `_同刻读法数`，由第二步的推理模型看着裁决。
    """
    if len(runs) == 1:
        return runs[0]
    out = dict(runs[0])
    for k in LISTS:
        if not isinstance(runs[0].get(k), list):
            continue
        buckets, loose = [], []
        for ri, r in enumerate(runs):
            for e in r.get(k) or []:
                t = _tof(e)
                if t is None:
                    if e not in loose:
                        loose.append(e)
                    continue
                b = next((x for x in buckets if abs(x['t'] - t) <= 1.0), None)
                if b is None:
                    buckets.append({'t': t, 'items': [(ri, e)]})
                else:
                    b['items'].append((ri, e))
        acc = []
        for b in buckets:
            groups = []
            for ri, e in b['items']:
                g = next((x for x in groups if x[0][1] == e), None)
                if g is None:
                    groups.append([(ri, e)])
                else:
                    g.append((ri, e))
            for g in groups:
                e = dict(g[0][1])
                e['_出现遍数'] = len({ri for ri, _ in g})
                e['_同刻读法数'] = len(groups)
                acc.append(e)
        out[k] = sorted(acc + loose, key=lambda e: _tof(e) or 0)
    out['人物'] = merge_people(runs)

    def vote(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        try:
            return max(set(vals), key=vals.count)
        except TypeError:
            # 叶子本身是 list 或 dict 时不可哈希，投不了票，取第一遍的读法。
            # qwen3.8-flash 会把「声音」里的某些叶子写成数组，Gemini 写成字符串。
            return vals[0]

    for k, v in runs[0].items():
        if isinstance(v, list):
            continue
        if isinstance(v, dict):
            # 场景这类嵌套字段逐个叶子投票，否则等于只取第一遍
            out[k] = {kk: vote([(r.get(k) or {}).get(kk) for r in runs])
                      for kk in v}
        else:
            out[k] = vote([r.get(k) for r in runs]) or v
    out['_合并'] = {'遍数': len(runs)}
    return out


def save(wd, name, obj):
    p = os.path.join(wd, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p, 'w'), ensure_ascii=False, indent=2)
    print(f'  → {p}')
    return obj


def done(wd, name):
    p = os.path.join(wd, name)
    if os.path.exists(p):
        print(f'  已有 {name}，跳过。要重跑先删掉它。')
        return json.load(open(p))
    return None


# ---------- 四步 ----------

def 跑几遍(做一遍, 遍数=None):
    """同一份输入跑 RUNS 遍取并集，几遍之间互不依赖，并发发出去。

    原来是串着发的：第一轮切段 26 帧、三遍，实测在一个 5 分钟的 Part 上花了
    12 分钟，其中三分之二是在等前一遍回来。通道本身不是瓶颈——同一份代码里
    逐段描述已经并发 6、结构推理并发 4，注释里记着实测文本并发 96 全返回 200。

    某一遍抛异常不拖垮整批：能回来几遍就取几遍的并集，一遍都没回来才报错。
    """
    n = 遍数 or RUNS
    if n <= 1:
        return [做一遍(0)]
    出, 错 = [], []
    with cf.ThreadPoolExecutor(n) as ex:
        for f in [ex.submit(做一遍, i) for i in range(n)]:
            try:
                出.append(f.result())
            except Blocked as e:
                错.append(f'被拦：{e}')
            except Exception as e:
                错.append(f'{type(e).__name__}: {e}')
    if not 出:
        raise 拆解失败('这一轮 %d 遍全失败：%s' % (n, '；'.join(错[:3])))
    if 错:
        print(f'    {n} 遍里回来 {len(出)} 遍，失败的：{"；".join(错[:2])}')
    return 出


def c_round1(wd):
    if done(wd, 'segs.json'):
        return
    frames = sorted(glob.glob(os.path.join(wd, 'round1_frames', '*.jpg')))
    txt = open(os.path.join(wd, 'round1_input.txt'), encoding='utf-8').read()
    print(f'第一轮切段：{len(frames)} 帧')
    runs = 跑几遍(lambda i: gen(P1(), images=frames, text=txt,
                                thinking=THINK['round1'], tag=f'round1#{i}'))
    save(wd, 'segs.json', merge(runs))


def _norm_seg(raw, n):
    """第二轮偶尔不按格式返回，救得回来的救回来。

    要了 responseMimeType=application/json 也只保证是合法 JSON，不保证是
    对象。实测 30 条里有 9 条撞上：模型直接返回一个数组，要么是把整个对象
    包在数组里，要么干脆只给事件列表。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        ds = [x for x in raw if isinstance(x, dict)]
        if len(ds) == 1 and ('事件' in ds[0] or '段落叙述' in ds[0]):
            return ds[0]
        if ds and all('时间' in x for x in ds):
            return {'段序号': int(n), '事件': ds}
    return None


def _one_seg(wd, c, n):
    """跑一段，返回要打印的几行。段之间互不依赖，可以并发。"""
    lg = [f'  段 {n}…']
    txt = open(c.replace('.mp4', '_input.txt'), encoding='utf-8').read()
    uri, mime = upload(c)
    # 三遍并发发。段之间本来就并发，这里再并发一层，同时打到通道上的请求数
    # 是 SEGPAR × RUNS；每一路都带着一段 base64 视频，所以 SEGPAR 跟着调小，
    # 乘积维持在原来的量级，别让内存和通道一起吃紧。
    def 一遍(i):
        raw = gen_uri(P2(), uri, mime, text=txt,
                      thinking=THINK['round2'], tag=f'seg{n}#{i}')
        g = _norm_seg(raw, n)
        if g is None:
            raise BadSchema(f'返回的不是对象（{type(raw).__name__}）')
        return g

    runs = []
    with cf.ThreadPoolExecutor(RUNS) as ex:
        for i, f in enumerate([ex.submit(一遍, i) for i in range(RUNS)]):
            try:
                runs.append(f.result())
            except Blocked as e:
                lg.append(f'    第 {i + 1} 遍被拦：{e}')
            except BadSchema as e:
                lg.append(f'    第 {i + 1} 遍{e}，丢掉')
            except Exception as e:
                lg.append(f'    第 {i + 1} 遍失败：{type(e).__name__}: {e}')
    if not runs:
        # 不落盘，下次续跑会重试这一段。落个失败标记会被 done() 当成
        # 已完成永远跳过。
        lg.append(f'    段 {n} 三遍都没拿到，跳过')
        return lg
    if len(runs) < RUNS:
        lg.append(f'    段 {n} 只拿到 {len(runs)}/{RUNS} 遍')
    r = merge(runs)
    mg = r.get('_合并') or {}
    lg.append(f'    {RUNS} 遍并集：事件 {len(r.get("事件") or [])} 条'
              f'（单遍 {[len(x.get("事件") or []) for x in runs]}）'
              f'　读法不同 {mg.get("读法不同的时刻数", 0)} 处'
              f'　只出现一遍 {mg.get("只出现一遍的条数", 0)} 条')
    for e in r.get('事件') or []:
        if e.get('_读法不同'):
            lg.append(f'      ⚠ {e.get("时间")}s（{e["_出现遍数"]}/{RUNS} 遍）'
                      f'{(e.get("这一刻发生了什么变化") or "")[:34]}')
    save(wd, f'round2/seg{n}_out.json', r)
    lg.append(f'  → {os.path.join(wd, f"round2/seg{n}_out.json")}')
    return lg


def c_round2(wd):
    # 只要 seg01.mp4 这种，别把转码缓存 seg01.q480.mp4 也收进来——
    # 首次跑缓存还没生成，续跑时才会撞上。
    clips = sorted(c for c in glob.glob(os.path.join(wd, 'round2', 'seg*.mp4'))
                   if re.search(r'seg\d+\.mp4$', c))
    if not clips:
        raise 拆解失败('切片没生成出来，这一步没法往下走。重新分析一次这个 Part。')
    todo = []
    for c in clips:
        n = re.search(r'seg(\d+)\.mp4$', c).group(1)
        if not done(wd, f'round2/seg{n}_out.json'):
            todo.append((c, n))
    # 段之间没有依赖，一段一次视频理解，串着跑就是纯排队。并发宽度从
    # SEGPAR 读。2026-09-08 实测白名单通道：文本并发 96 全部返回 200，单次
    # 中位延迟只从 4.5 秒涨到 6.4 秒；带视频的多模态端点并发 11 也全通，
    # 中位 9.4 秒。原来写死 3 是没测过的保守值。默认给 6，留出余量给
    # 同时在跑的别的素材——批次并发乘以 SEGPAR 才是真正打到通道上的数。
    # 段内已经并发 RUNS 遍，这里的并发数相应除下去，乘积还是原来那个 6
    par = max(1, int(os.environ.get('SEGPAR', str(max(1, 6 // max(1, RUNS))))))
    print(f'第二轮逐段描述：{len(clips)} 段，还差 {len(todo)} 段，并发 {par}')
    if not todo:
        return
    with cf.ThreadPoolExecutor(min(par, len(todo))) as ex:
        for lines in ex.map(lambda x: _one_seg(wd, x[0], x[1]), todo):
            print('\n'.join(lines))


def c_round3(wd):
    if done(wd, 'round3_out.json'):
        return
    frames = sorted(glob.glob(os.path.join(wd, 'round3_frames', '*.jpg')))
    tail = os.path.join(wd, 'round3', 'tail30.mp4')
    txt = open(os.path.join(wd, 'round3_input.txt'), encoding='utf-8').read()
    print(f'第三轮呈现形式：{len(frames)} 帧 + 片尾切片')
    runs = 跑几遍(lambda i: gen(P3(), video=tail, images=frames, text=txt,
                                thinking=THINK['round3'], tag=f'round3#{i}'))
    save(wd, 'round3_out.json', merge(runs))


def c_stage2(wd):
    """第二步三轮：整条脉络 → 逐段定性 → 悬念对答案。画像是第四轮，单独跑。

    可复用单元是段，不是整条。段边界来自 ffmpeg 检出的黑场和场景切换，是
    真实剪辑点，所以不再让模型去猜高光起止。剧情点降级挂在段下面。

    逐段定性单独开窗，是因为整条一次问会被少报：实测 30 条，时长涨 18 倍，
    ffmpeg 数的事件数涨 15 倍，而一次问出来的悬念点只从 3 条涨到 7 条。

    原来还有一轮桥段比对，找整条里重复的桥段。28 条里 23 条结果是 0，
    留着每条素材白花一次调用，已经删掉。
    """
    f = os.path.join(wd, 'stage2_out.json')
    if os.path.exists(f):
        print('  已有 stage2_out.json，跳过。要重跑先删掉它。')
        return cite(wd)
    p = os.path.join(wd, 'stage2_input.txt')
    if not os.path.exists(p):
        raise 拆解失败('理结构要用的文本没生成出来。重新分析一次这个 Part。')
    txt = open(p, encoding='utf-8').read()
    print(f'第二步第一轮 整条脉络：{len(txt)} 字，{MODEL}，'
          f'思考预算 {THINK["stage2"]}')
    try:
        whole = gen(PR(), text=txt, thinking=THINK['stage2'], tag='stage2')
    except Blocked as e:
        # 模型自己拒答，同样的内容重试没有意义。但只有这一轮吃整条全文，
        # 后面几轮输入小得多，未必会被拒，所以不退出。
        print(f'  第一轮被拦：{e}　后面几轮照跑')
        whole = {'_第一轮被拦': str(e)}
        print('  这一条没有题材和剧情时间线，第二轮少了整条脉络这个上下文')
    out = dict(whole)
    out.update(c_scan(wd, whole.get('剧情时间线') or []))
    save(wd, 'stage2_out.json', out)
    cite(wd)


def _seg_text(sg, i, start):
    """把一段的完整记录写成给模型看的文字，时间换成绝对时间。"""
    L = [f"── 第 {i} 段　从第 {start:.1f} 秒起，长 {sg.get('_段时长') or 0:.1f} 秒"]
    L.append(f"场景：{(sg.get('场景') or {}).get('地点')}")
    who = '、'.join(f"{p.get('称呼')}（{p.get('外观')}）"
                    for p in sg.get('人物') or [])
    L.append(f'人物：{who}')
    L.append('事件：')
    for e in sg.get('事件') or []:
        t = e.get('绝对时间')
        if t is None:
            t = float(e.get('时间') or 0) + start
        line = (f"  {float(t):.1f} 秒　{e.get('画面')}"
                f"　｜变化：{e.get('这一刻发生了什么变化')}"
                f"　｜在场 {e.get('在场人数')} 人，旁人在看：{e.get('有旁人在看吗')}")
        L.append(line)
        if e.get('台词'):
            L.append(f'      台词：{e["台词"]}')
    for t in sg.get('屏幕上的文字') or []:
        L.append(f"  屏幕文字 {t.get('绝对时间', t.get('时间'))} 秒：{t.get('内容')}")
    L.append(f"段落叙述：{sg.get('段落叙述')}")
    L.append(f"这一段最后一个镜头：{sg.get('这一段最后一个镜头')}")
    return '\n'.join(L)


def c_scan(wd, timeline):
    """第二轮按窗出段规格，第三轮给悬念对答案。"""
    segs = read_segs(wd)
    if not segs:
        print('  没有段落记录，跳过逐段定性')
        return {'_覆盖': {'总段数': 0, '看成的段数': 0, '被拦的段': [], '覆盖率': 0.0}}
    tl = '\n'.join(f"第 {e.get('段')} 段：{e.get('这一段推进了什么')}"
                    for e in timeline) or '（第一轮没给时间线）'
    wins = [segs[i:i + WIN] for i in range(0, len(segs), WIN)]
    print(f'第二步第二轮 逐段定性：{len(segs)} 段分 {len(wins)} 窗，每窗最多 {WIN} 段')
    specs, okseg, blkseg = [], [], []

    why = {}      # 段号 → 被拒的原因，最后要写进产物，不能只在日志里一闪而过

    def ask(w, tag):
        """把一组段送去定性。被拦返回 None，跟「这几段没有剧情点」区分开。

        拒绝原因记进 why。少了它，一条素材跑完只剩一个空的 structure.json，
        看不出是内容被拒还是这条素材本来就没东西。
        """
        body = '\n\n'.join(
            _seg_text(sg, sg.get('_序号') or 0, float(sg.get('_起') or 0))
            for sg in w)
        txt = (f'【整条素材的剧情时间线】\n{tl}\n\n'
               f'【你这一次要处理的段】共 {len(w)} 段\n{body}\n')
        try:
            r = gen(PS(), text=txt, thinking=THINK['scan'], tag=tag)
        except Blocked as e:
            for sg in w:
                why[sg.get('_序号')] = str(e)
            return None
        if not isinstance(r, dict):
            return {}
        return r

    def do_win(iw):
        """跑一个窗。已经有产物就读回来，不重跑。返回窗号和结果。"""
        wi, w = iw
        f = os.path.join(wd, 'stage2_scan', f'win{wi:02d}.json')
        if os.path.exists(f):
            return wi, json.load(open(f)), True
        r = ask(w, f'scan{wi}')
        if r is None and len(w) > 1:
            # 整窗被拦不代表每一段都过不去。实测一段边缘素材会把同窗的
            # 其他段一起拖下水，拆成单段能救回大部分。
            got, bad = [], []
            for sg in w:
                n = sg.get('_序号')
                one = ask([sg], f'scan{wi}s{n}')
                if one is None:
                    bad.append(n); continue
                got += one.get('段') or []
            r = {'段': got, '_被拦的段': bad,
                 '_看成的段': [sg.get('_序号') for sg in w
                               if sg.get('_序号') not in bad],
                 '_拒绝原因': {str(n): why.get(n) for n in bad}}
        elif r is None:
            bad = [sg.get('_序号') for sg in w]
            r = {'段': [], '_被拦的段': bad, '_看成的段': [],
                 '_拒绝原因': {str(n): why.get(n) for n in bad}}
        else:
            r['_看成的段'] = [sg.get('_序号') for sg in w]
            r['_被拦的段'] = []
        save(wd, f'stage2_scan/win{wi:02d}.json', r)
        return wi, r, False

    jobs = list(enumerate(wins, 1))
    par = min(PAR, len(jobs))
    if par > 1:
        print(f'  {par} 个窗同时跑')
    with cf.ThreadPoolExecutor(par) as ex:
        done = sorted(ex.map(do_win, jobs), key=lambda x: x[0])   # 按窗号排回来
    for wi, r, cached in done:
        w = wins[wi - 1]
        okseg += r.get('_看成的段') or []
        blkseg += r.get('_被拦的段') or []
        got = r.get('段') or []
        pts = sum(len(x.get('段里的剧情点') or []) for x in got)
        mark = f"　拦掉段 {r['_被拦的段']}（拆成单段重试后仍不过）" if r.get('_被拦的段') else ''
        print(f"  第 {wi} 窗（段 {w[0].get('_序号')}–{w[-1].get('_序号')}）"
              f"　出 {len(got)} 段规格，剧情点 {pts} 处{mark}"
              + ('　（读缓存）' if cached else ''))
        specs += got

    # 覆盖率跟着结果走。被拦的段出不了规格，那几段的空白是「没看成」，
    # 不是「没有」。不标出来，下游会把它当真值算进统计。
    cov = {'总段数': len(segs), '看成的段数': len(okseg),
           '被拦的段': sorted(blkseg),
           '拒绝原因': {str(n): why.get(n) for n in sorted(blkseg)},
           '覆盖率': round(len(okseg) / len(segs), 3) if segs else 0.0}
    if blkseg:
        print(f"  覆盖率 {cov['覆盖率']:.0%}：{len(segs)} 段里 {len(blkseg)} 段被拦"
              f"（段 {cov['被拦的段']}）")

    specs.sort(key=lambda x: x.get('段序号') or 0)
    sus = [{'时间': p.get('时间'), '是什么': p.get('是什么')}
           for x in specs for p in x.get('段里的剧情点') or []
           if p.get('哪一类') == '悬念点' and p.get('时间') is not None]
    sus.sort(key=lambda e: float(e['时间']))
    ans = []
    if sus:
        txt = (f'【整条素材的剧情时间线】\n{tl}\n\n【悬念点】\n'
               + json.dumps(sus, ensure_ascii=False, indent=1))
        print(f'第二步第三轮 悬念对答案：{len(sus)} 条，{len(txt)} 字')
        try:
            m = gen(PM(), text=txt, thinking=THINK['answer'], tag='answer')
            ans = m.get('悬念点') or [] if isinstance(m, dict) else []
        except Blocked as e:
            print(f'  第三轮被拦：{e}　悬念一律记作没给答案')
        if len(ans) != len(sus):
            # 条数不对就不用它，宁可全部记作没给答案，也不要错配时间
            if ans:
                print(f'  第三轮返回 {len(ans)} 条，输入 {len(sus)} 条，对不上，弃用')
            ans = [dict(e, 后面给答案了吗='没给', 答案在哪=None,
                        凭什么算给了答案=None, _没做对答案=True) for e in sus]
        else:
            n = sum(1 for e in ans if e.get('后面给答案了吗') == '给了')
            print(f'  {n}/{len(ans)} 条后面给了答案')
    return {'段规格': specs, '悬念点': ans, '_覆盖': cov}


def c_profile(wd):
    """第五轮：整条画像。输入是前四轮的结果，不给原始记录。

    前四轮把素材拆成零件，这一轮装回去。缺了它，产物只有一堆占比和中位数，
    回答不了「这条素材讲了什么、怎么剪的、靠什么抓人」。
    """
    f = os.path.join(wd, 'stage2_out.json')
    if not os.path.exists(f):
        raise 拆解失败('理结构那一步没出结果，后面接不下去。重新分析一次这个 Part。')
    d = json.load(open(f))
    st1p = os.path.join(wd, 'stage1.json')
    st1 = json.load(open(st1p)) if os.path.exists(st1p) else {'整条': {}, '段': []}
    segs = {s.get('_序号'): s for s in st1.get('段') or []}
    specs = sorted(d.get('段规格') or [], key=lambda x: x.get('段序号') or 0)
    if not specs:
        print('  没有段规格，跳过画像')
        return

    tl = {e.get('段'): e.get('这一段推进了什么') for e in d.get('剧情时间线') or []}
    L = [f"整条素材 {st1['整条'].get('素材时长', 0):.0f} 秒，{len(specs)} 段。",
         f"题材：{(d.get('题材') or {}).get('一级')} / {(d.get('题材') or {}).get('二级')}", '']
    for x in specs:
        n = x.get('段序号')
        sg = segs.get(n) or {}
        a, b = sg.get('_起'), sg.get('_止')
        rng = f'{a:.0f}–{b:.0f} 秒' if a is not None and b is not None else '时间不详'
        L.append(f'── 第 {n} 段　{rng}')
        # 地点用来判「一场戏切多段」「单场到底」，人物用来判「交替铺垫」
        # 「强者兜底」。两样第一步都识别出来了，不给的话这几条判不了。
        loc = (sg.get('场景') or {}).get('地点')
        if loc:
            L.append(f'  地点：{loc}')
        who = [c.get('称呼') for c in (sg.get('人物') or []) if c.get('称呼')]
        if who:
            L.append(f'  这一段出场：{"、".join(who[:8])}')
        if tl.get(n):
            L.append(f'  这一段推进了：{tl[n]}')
        L.append(f"  最强的一刻：{x.get('这一段最强的一刻是什么')}"
                 f"（第 {x.get('最强的一刻在第几秒')} 秒）")
        L.append(f"  结尾{x.get('结尾留没留悬念')}悬念"
                 + (f"：{x.get('留的是什么')}" if x.get('留的是什么') else ''))
        kinds = [p.get('哪一类') for p in x.get('段里的剧情点') or []]
        if kinds:
            L.append(f"  这一段的剧情点：{'、'.join(k for k in kinds if k)}")
        L.append('')
    sus = d.get('悬念点') or []
    if sus:
        no = sum(1 for e in sus if e.get('后面给答案了吗') == '没给')
        L.append(f'整条抛了 {len(sus)} 个悬念，其中 {no} 个到结尾没给答案。')
    txt = '\n'.join(L)
    print(f'第二步第四轮 整条画像：{len(specs)} 段，{len(txt)} 字')
    try:
        r = gen(PP(), text=txt, thinking=THINK['profile'], tag='profile')
    except Blocked as e:
        print(f'  画像被拦：{e}')
        r = {'_被拦': str(e)}
    _profile_local(r, st1, segs)
    d['画像'] = r
    save(wd, 'stage2_out.json', d)
    q = os.path.join(wd, 'structure.json')
    if os.path.exists(q):
        st = json.load(open(q))
        st['画像'] = r
        json.dump(st, open(q, 'w'), ensure_ascii=False, indent=2)
    print(f"  {r.get('男频女频')}　{r.get('一句话故事')}")
    print(f"  手法：{r.get('剪辑手法')}")
    if r.get('结构上的缺口'):
        print(f"  缺口：{'；'.join(r['结构上的缺口'])}")


def _profile_local(r, st1, segs):
    """秒数和占比本地算，模型只给段号。

    问模型要占比，它会拿段数去估，估出来的数跟真实时长差很远——段时长
    从 20 秒到 120 秒都有，段数占比不等于时长占比。
    """
    if not isinstance(r, dict):
        return
    dur = st1.get('整条', {}).get('素材时长') or 0

    def span(a, b):
        """第 a 到第 b 段的真实起止秒和占全片比例。"""
        xs = [segs[n] for n in range(int(a), int(b) + 1) if n in segs]
        if not xs:
            return None
        lo = min(x.get('_起') or 0 for x in xs)
        hi = max(x.get('_止') or 0 for x in xs)
        return {'起': round(lo, 1), '止': round(hi, 1),
                '时长': round(hi - lo, 1),
                '占全片': round((hi - lo) / dur, 3) if dur else None}

    for x in r.get('结构') or []:
        a = x.get('从第几段到第几段') or []
        if len(a) == 2:
            sp = span(a[0], a[1])
            if sp:
                x['秒数'] = [sp['起'], sp['止']]
                x['占全片'] = sp['占全片']
    for x in r.get('装配套路') or []:
        a = x.get('在哪几段') or []
        if len(a) == 2:
            sp = span(a[0], a[1])
            if sp:
                x['秒数'] = [sp['起'], sp['止']]
                x['占全片'] = sp['占全片']
    # 高潮占了多少时长，是判断长片撑不撑得住的那个数
    hi = [x for x in (r.get('结构') or []) if x.get('部分') == '高潮']
    r['_高潮占全片'] = round(sum(x.get('占全片') or 0 for x in hi), 3) if hi else 0.0
    r['_有铺垫段'] = any(x.get('部分') == '铺垫' for x in (r.get('结构') or []))
    r['_结构块数'] = len(r.get('结构') or [])
    # 处境翻转了几次。一路赢或一路输都是 0。
    seq = [x.get('处境') for x in (r.get('主角处境曲线') or [])
           if x.get('处境') in ('占上风', '被压制')]
    r['_处境翻转次数'] = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    r['_开局处境'] = seq[0] if seq else None
    r['_结局处境'] = seq[-1] if seq else None
    # 段的角色要求每段都标。漏了就记下来，别等汇总时才发现分母不对。
    n = len(segs)
    got = {x.get('段') for x in (r.get('段的角色') or []) if isinstance(x.get('段'), int)}
    r['_角色覆盖'] = round(len(got & set(segs)) / n, 3) if n else None
    r['_角色漏的段'] = sorted(set(segs) - got)
    # 同一条套路可以在不同段落各出现一次，横向比组的时候按有没有算，不按次数
    seen, uniq = set(), []
    for x in r.get('装配套路') or []:
        k = x.get('套路')
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    r['_套路'] = uniq


def read_segs(wd):
    """按段号读回第二轮的输出，附上起止和绝对时间。

    直接复用 prep.read_segs，它同时算好了绝对时间和对齐到镜头切点。
    那个函数返回三样（段、时长、检出结果），这里只要段。
    """
    if not glob.glob(os.path.join(wd, 'round2', 'seg*_out.json')):
        return []
    from . import prep as _prep
    return _prep.read_segs(wd)[0]


def cite(wd):
    """核对第二步的依据：编造引用率、类型错配、覆盖率。"""
    import subprocess
    sub = next((c for c in (os.path.join(wd, 'sub.tsv'),
                            wd.rstrip('/').replace('_拆解', '.sub.tsv'))
                if os.path.exists(c)), None)
    if not sub:
        print('  找不到字幕 TSV，跳过依据核对')
        return
    f = os.path.join(wd, 'stage2_out.json')
    if os.path.exists(f):
        print()
        subprocess.run([sys.executable, os.path.join(BASE, 'check_cite.py'),
                        f, sub, wd])


def c_all(wd):
    import subprocess
    prep = os.path.join(BASE, 'prep.py')

    def run(*a):
        print(f'\n$ prep.py {" ".join(a)}')
        subprocess.run([sys.executable, prep, *a], check=True)

    mp4 = os.path.join(os.path.dirname(wd.rstrip('/')),
                       os.path.basename(wd.rstrip('/')).replace('_拆解', '') + '.mp4')
    sub = mp4.replace('.mp4', '.sub.tsv')
    c_round1(wd)
    run('round2', mp4, sub, os.path.join(wd, 'segs.json'))
    c_round2(wd)
    c_round3(wd)
    run('post1', wd)
    run('stage2', wd)
    c_stage2(wd)
    run('post2', wd)
    print(f'\n完成 → {wd}/structure.json')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    c = sys.argv[1]
    try:
        {'round1': c_round1, 'round2': c_round2, 'round3': c_round3,
         'stage2': c_stage2, 'cite': cite,
         'profile': c_profile, 'all': c_all}[c](sys.argv[2])
    finally:
        report()      # 中途失败也要知道已经烧了多少
