# -*- coding: utf-8 -*-
"""拆解管线在 app 里的落脚点：提示词在哪、ocrbin 在哪、key 从哪来。

研究管线是按「脚本旁边就是 prompts/」的布局写的，装进 app 之后这条不成立：
开发模式下提示词在仓库根目录，PyInstaller 打包之后在 _internal/ 里。
两种情况都要能找到，所以路径一律从这里取，别的模块不自己拼。
"""
import os
import pathlib
import sys


class 拆解失败(Exception):
    """拆解过程中的任何致命错误。

    研究管线原来遇到这些情况直接 sys.exit，那是脚本的活法；跑在后端进程里
    必须抛出来，让分析任务标成 failed 并把话带给界面，而不是把服务打死。
    """


def 根目录() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys._MEIPASS)
    # app/services/decompose/ 往上四级到仓库根
    return pathlib.Path(__file__).resolve().parents[4]


def 提示词目录() -> str:
    return str(根目录() / "prompts")


def ocr程序() -> str:
    """OCR 二进制跟本模块放在一起，打包时整个目录一起收进去。"""
    p = pathlib.Path(__file__).resolve().parent / "ocrbin"
    if not p.exists():
        raise 拆解失败(f"找不到 OCR 程序 {p}，这一版打包漏收了")
    if not os.access(p, os.X_OK):
        try:
            os.chmod(p, 0o755)
        except OSError as e:
            raise 拆解失败(f"OCR 程序不可执行：{e}")
    return str(p)


def 分析模型() -> str:
    """段库拆解用哪个模型。只认 qwen 开头的名字，别处要显示模型名一律问这里。"""
    m = os.environ.get("MODEL", "qwen3.8-flash")
    return m if m.startswith("qwen") else "qwen3.8-flash"


# 设置面板里「Qwen · API 地址」的默认值，也是白名单通道的地址。
# 用户把那一栏清空时回落到它，免得整条管线因为一个空串全挂。
默认通道 = "https://ws-bljb9l73522yj7vp.us-east-1.maas.aliyuncs.com"


def qwen_host() -> str:
    """拆解管线的接口地址，跟 app 其余部分读同一个配置项。

    配置里存的是带 /api/v1 的完整地址（chat 接口那边要），这里要的是主机部分，
    多模态和文本两个端点各自往后拼，所以把尾巴摘掉。
    """
    try:
        from app.core.config import settings
        u = (getattr(settings, "QWEN_BASE_URL", "") or "").strip()
    except Exception:
        u = ""
    u = u or 默认通道
    return u.rstrip("/").removesuffix("/api/v1").rstrip("/")


def qwen_key() -> str:
    """跟 app 其余部分共用一把 key，不再读 ~/.happyhorse.env。

    走白名单通道直连，视频用 base64 带在请求里，不经 OSS——平台侧还没配
    美区 OSS 的环境变量，那条路在用户机器上是断的。
    """
    k = os.environ.get("QWEN_WS_KEY")
    if k:
        return k
    try:
        from app.core.config import settings
        k = getattr(settings, "QWEN_API_KEY", "") or ""
    except Exception:
        k = ""
    if not k:
        raise 拆解失败("未配置 QWEN_API_KEY，请在设置里填好 Qwen API Key 再重启应用")
    return k
