"""轻量日志：tee 落盘 + 结构化事件 + 机器画像/资源快照。

设计目标（现阶段）：
- 够用：把现有所有 print 自动落盘（tee），业务代码零改动；
- 能排查：关键失败点用 log_event 记 ids / ffmpeg 报错 / 磁盘等结构化字段；
- 可扩展：加字段 = 给 log_event 多传一个 kwarg，无需改格式/迁移。

日志绝不能影响主流程：所有写入都包了 try/except，失败就静默。
"""
import os
import sys
import glob
import time
import platform
import shutil
from datetime import datetime
from app.core.config import settings

# 日志目录：与 skyart-data 同级的 logs/（即 UPLOAD_DIR 的父目录下）
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(settings.UPLOAD_DIR)), "logs")
_RETAIN_DAYS = 7


class _LogFile:
    """按天切割的日志文件写入器（被 stdout/stderr 的 Tee 共享）。"""
    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._date = None
        self._fh = None

    def write(self, text: str):
        try:
            today = datetime.now().strftime("%Y%m%d")
            if today != self._date or self._fh is None:
                if self._fh:
                    self._fh.close()
                os.makedirs(self.log_dir, exist_ok=True)
                self._fh = open(
                    os.path.join(self.log_dir, f"backend-{today}.log"),
                    "a", encoding="utf-8", buffering=1,
                )
                self._date = today
            self._fh.write(text)
        except Exception:
            pass  # 日志失败绝不影响主流程


def _ts() -> str:
    now = datetime.now()
    return now.strftime("[%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}] "


class _Tee:
    """一进两出：给每行行首补时间戳后，同时写到原始流（控制台）+ 日志文件。
    不脱敏 —— 用户名/项目名等保留，便于内部排查（密钥/token 本就不记入日志）。"""
    def __init__(self, original, logfile: _LogFile):
        self._orig = original
        self._lf = logfile
        self._at_start = True  # 行首标记

    def write(self, text):
        try:
            for line in text.splitlines(keepends=True):
                prefix = _ts() if self._at_start else ""
                out = prefix + line
                self._orig.write(out)   # 控制台
                self._lf.write(out)     # 日志文件
                self._at_start = line.endswith("\n") or line.endswith("\r")
        except Exception:
            try:
                self._orig.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self._orig, "isatty", lambda: False)()

    def fileno(self):
        return self._orig.fileno()


_installed = False


def _cleanup_old():
    cutoff = time.time() - _RETAIN_DAYS * 86400
    for f in glob.glob(os.path.join(LOG_DIR, "backend-*.log")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except Exception:
            pass


def setup_logging():
    """安装 stdout/stderr tee + 清理过期日志。幂等；应尽量早调用（捕获 uvicorn 日志）。"""
    global _installed
    if _installed:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        lf = _LogFile(LOG_DIR)
        sys.stdout = _Tee(sys.stdout, lf)
        sys.stderr = _Tee(sys.stderr, lf)
        _installed = True
        _cleanup_old()
    except Exception as e:
        try:
            print(f"[logs] setup_logging 失败（忽略）: {e}")
        except Exception:
            pass


def _fmt(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(str(x) for x in v) + "]"
    return str(v).replace("\n", " ").replace("\r", " ").strip()


def log_event(event: str, level: str = "INFO", **fields):
    """结构化事件一行： [ts] [LEVEL] [event] k=v k=v ...
    None 值自动忽略；加字段就是多传一个 kwarg。"""
    try:
        # 不再自带时间戳：由 tee 在写文件时给每行统一补，避免双重时间戳
        kv = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
        print(f"[{level}] [{event}] {kv}".rstrip())
    except Exception:
        pass


def _disk_free_gb(path: str):
    try:
        return round(shutil.disk_usage(path).free / (1024 ** 3), 1)
    except Exception:
        return None


def _mem_total_gb():
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 1)
    except Exception:
        return None


def machine_profile() -> dict:
    """机器画像（启动记一次）。app_version 由调用方补充（FastAPI app.version）。"""
    try:
        from app.services.video import check_dependencies
        deps = check_dependencies()
        ff = deps.get("ffmpeg", {})
        ver = (ff.get("version") or "").split(" Copyright")[0].strip() or None
        return {
            "arch": platform.machine(),
            "os": f"{platform.system()} {platform.release()}",
            "cpu_cores": os.cpu_count(),
            "mem_total_gb": _mem_total_gb(),
            "disk_free_gb": _disk_free_gb(settings.UPLOAD_DIR),
            "ffmpeg_ok": deps.get("ok"),
            "ffmpeg_path": ff.get("path"),
            "ffmpeg_version": ver,
            "data_dir": os.path.abspath(settings.UPLOAD_DIR),
        }
    except Exception as e:
        return {"profile_error": str(e)[:200]}


def resource_snapshot() -> dict:
    """失败/导出时的轻量资源快照（重点：磁盘剩余空间）。"""
    return {"disk_free_gb": _disk_free_gb(settings.UPLOAD_DIR)}


def _ffmpeg_stderr(exc) -> str | None:
    """从 ffmpeg.Error 提取真实 stderr（quiet=True 已自动捕获到 exc.stderr）。"""
    raw = getattr(exc, "stderr", None)
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        return str(raw).strip()[-800:]
    except Exception:
        return None


def log_failure(event: str, exc, **fields):
    """记录一次失败：自动带 error 摘要、ffmpeg 真实报错（若有）、资源快照。
    用法： log_failure("admake.build_part", e, part_id=pid)"""
    log_event(
        event, level="ERROR",
        error=str(exc)[:300],
        ffmpeg_stderr=_ffmpeg_stderr(exc),
        **resource_snapshot(),
        **fields,
    )
