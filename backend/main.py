"""
SkyArt Backend — FastAPI application entry point.

Run locally:
    cd backend && uvicorn main:app --reload --port 8764

Electron integration:
    Electron spawns this process on startup and terminates it on exit.
"""
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.database import create_tables, AsyncSessionLocal
from app.routers import auth, projects, episodes, assets, storyboard, admake, admake_template
from app.core.logs import setup_logging, log_event, machine_profile

# 版本号只写这一处。之前散在 FastAPI(version=)、/、自检接口三个地方，
# 改版号漏掉一处就会出现「首页显示 beta.4、自检里还写着 beta.3」
APP_VERSION = "1.0.7-beta.1"

# 尽早安装日志 tee（捕获含 uvicorn 在内的全部 stdout/stderr → 按天滚动日志文件）
setup_logging()


async def _seed_default_user():
    """首次启动时创建默认 admin 账号，方便试用。"""
    from sqlalchemy import select
    from app.models.user import User
    from app.core.security import hash_password
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.username == "admin"))
        if res.scalar_one_or_none():
            return  # 已存在，跳过
        user = User(username="admin", email="admin@skyart.local",
                    hashed_password=hash_password("123456"))
        db.add(user)
        await db.commit()
        print("✅  Default user created: admin / 123456")


def _log_ffmpeg_status():
    """启动时打印 ffmpeg/ffprobe 检测结果，便于排查“业务机器缺 ffmpeg”。
    纯诊断用途：整段包 try/except，任何异常都不得影响后端启动。"""
    try:
        from app.services.video import check_dependencies
        deps = check_dependencies()
        if deps["ok"]:
            print(f"✅  ffmpeg:  {deps['ffmpeg']['path']}  ({deps['ffmpeg']['version']})")
            print(f"✅  ffprobe: {deps['ffprobe']['path']}")
        else:
            print("⚠️  ffmpeg/ffprobe 不可用 —— 视频裁切/合并/导出将失败：")
            for t in ("ffmpeg", "ffprobe"):
                d = deps[t]
                print(f"     {t}: {('OK ' + str(d['path'])) if d['ok'] else ('NOT FOUND — ' + str(d['error']))}")
    except Exception as e:
        # 诊断失败绝不能拖垮后端启动
        print(f"⚠️  ffmpeg 检测异常（不影响启动）: {e}")


def _install_orphan_guard():
    """Electron 强退(SIGKILL)时 before-quit 不执行，本进程会被 launchd 收养成孤儿、
    继续占着端口 → 下次启动串版本(KNOWLEDGE #7)。两条独立信号任一触发即自退：
      1) stdin(父进程持有的管道)EOF —— 父进程一死 OS 关闭写端，即时、最可靠；
      2) getppid() 变化 —— 被 launchd 收养(2s 轮询兜底)。
    仅在被父进程管道托管时启用；独立/终端运行不启用，以免误退。"""
    import stat
    import sys
    import threading
    import time

    # 是否被父进程管道托管：Electron spawn 时 stdin 是 pipe(FIFO)；终端/独立运行是 tty
    # 或重定向文件，都不满足。两条信号都只在托管时启用，避免终端调试时被误退。
    try:
        managed = stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode)
    except Exception:
        managed = False
    if not managed:
        return

    # 信号 1：stdin 管道 EOF —— 父进程一死 OS 关闭写端，即时、最可靠
    def _watch_stdin():
        try:
            while sys.stdin.readline():
                pass
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_watch_stdin, daemon=True, name="guard-stdin").start()

    # 信号 2：父进程消失后 ppid 变化（2s 轮询兜底）
    start_ppid = os.getppid()
    if start_ppid > 1:
        def _watch_ppid():
            while True:
                time.sleep(2)
                if os.getppid() != start_ppid:
                    os._exit(0)
        threading.Thread(target=_watch_ppid, daemon=True, name="guard-ppid").start()


async def _sweep_stale_tasks():
    """启动清扫：上一次进程退出时卡在进行中的任务（导出/Part 合成/AI 分析）没有任何
    执行器会接手，永远停在 pending/processing——统一标记 failed，任务中心和编辑器
    才不会把两周前的尸体当"正在执行"展示。"""
    from sqlalchemy import update, or_
    from app.database import AsyncSessionLocal
    from app.models.admake import AdmakeExport, AdmakePart
    async with AsyncSessionLocal() as db:
        r1 = await db.execute(
            update(AdmakeExport)
            .where(AdmakeExport.status.in_(("pending", "processing")))
            .values(status="failed"))
        r2 = await db.execute(
            update(AdmakePart)
            .where(AdmakePart.status.in_(("pending", "processing")))
            .values(status="failed"))
        r3 = await db.execute(
            update(AdmakePart)
            .where(AdmakePart.ai_status == "analyzing")
            .values(ai_status="failed"))
        await db.commit()
        n = (r1.rowcount or 0) + (r2.rowcount or 0) + (r3.rowcount or 0)
        if n:
            log_event("app.sweep_stale_tasks", exports=r1.rowcount,
                      parts=r2.rowcount, ai=r3.rowcount)
            print(f"🧹  启动清扫：{n} 条中断任务标记为 failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────
    _install_orphan_guard()
    await create_tables()
    await _seed_default_user()
    await _sweep_stale_tasks()
    print("✅  SkyArt Backend started — SQLite tables ready")
    _log_ffmpeg_status()
    log_event("app.start", app_version=app.version, **machine_profile())
    yield
    # ── Shutdown ─────────────────────────────────────────
    print("👋  SkyArt Backend shutting down")


app = FastAPI(
    title="SkyArt API",
    version=APP_VERSION,
    description="天工短剧 AI 短剧创作平台后端",
    lifespan=lifespan,
)

# ── CORS (allow Electron renderer and local dev) ──────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS + ["*"],  # loosen for local dev; tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static file serving for uploaded media ────────────────────────────────────
upload_abs = os.path.abspath(settings.UPLOAD_DIR)
app.mount("/uploads", StaticFiles(directory=upload_abs), name="uploads")

# 花字字体（供模板编辑页 @font-face 预览用，导出时后端 Pillow 也用同一批文件）
_fonts_abs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "fonts")
if os.path.isdir(_fonts_abs):
    app.mount("/fonts", StaticFiles(directory=_fonts_abs), name="fonts")

# ── API Routes ────────────────────────────────────────────────────────────────
API = "/api/v1"
app.include_router(auth.router,        prefix=API)
app.include_router(projects.router,    prefix=API)
app.include_router(episodes.router,    prefix=API)
app.include_router(assets.router,      prefix=API)
app.include_router(storyboard.router,  prefix=API)
app.include_router(admake.router,      prefix=API)
app.include_router(admake_template.router, prefix=API)


@app.get("/")
async def health():
    return {"status": "ok", "service": "SkyArt API", "version": APP_VERSION}


@app.get("/api/v1/system/health")
async def system_health():
    """环境依赖检测：ffmpeg/ffprobe 是否可用。前端据此在缺失时提示用户。"""
    from app.services.video import check_dependencies
    return await asyncio.to_thread(check_dependencies)


@app.get("/api/v1/system/config-status")
async def system_config_status():
    """密钥完整性检查（只查非空、不发网络请求）：启动时前端据此提示缺哪些配置。
    连通性实测在 AI 分析入口做（admake._precheck_ai_credentials），此处保持毫秒级。"""
    def _missing(pairs):
        return [label for label, value in pairs if not value]

    ai_gemini = _missing([
        ("Gemini 代理地址", settings.GEMINI_PROXY_URL),
        ("Gemini 代理密钥", settings.GEMINI_PROXY_KEY),
        ("美区 OSS AccessKey", settings.OSS_KEY_SkyArt_US),
        ("美区 OSS Secret", settings.OSS_SECRET_SkyArt_US),
        ("美区 OSS Bucket", settings.OSS_BUCKET_SkyArt_US),
    ])
    ai_qwen = _missing([
        ("HappyHorse API Key", settings.HAPPYHORSE_API_KEY),
    ])
    drama_video = _missing([
        ("HappyHorse API Key", settings.HAPPYHORSE_API_KEY),
        ("国内 OSS AccessKey", settings.OSS_ACCESS_KEY_ID),
        ("国内 OSS Secret", settings.OSS_ACCESS_KEY_SECRET),
        ("国内 OSS Bucket", settings.OSS_BUCKET_NAME),
    ])
    return {
        "ai_gemini":   {"ok": not ai_gemini, "missing": ai_gemini},
        "ai_qwen":     {"ok": not ai_qwen, "missing": ai_qwen},
        "drama_video": {"ok": not drama_video, "missing": drama_video},
    }


@app.get("/api/v1/system/selfcheck")
async def system_selfcheck(deep: int = 0):
    """一次跑完全部体检项，前端按最差的一项决定灯色。

    分两档：浅检(deep=0)只碰本地——进程、可执行文件、目录可写、磁盘、数据库、密钥非空，
    毫秒级，开机自动跑。深检(deep=1)才发网络请求实测密钥连通，秒级，只在用户点
    「重新检测」时跑：放进开机流程会因为一次网络抖动就报红灯，还要人干等。

    每项的 level 有三档：
      ok    正常
      warn  软件能用，某些功能不可用（密钥缺、磁盘偏紧）
      fail  核心不可用，必须处理（ffmpeg 缺、目录不可写、数据库读不了、磁盘见底）
    """
    import shutil as _sh, tempfile as _tf, time as _t
    from sqlalchemy import text as _text

    items: list[dict] = []

    def add(key, name, level, detail, hint=None):
        items.append({"key": key, "name": name, "level": level,
                      "detail": detail, "hint": hint})

    # ① 后端自身：能走到这行就说明进程活着
    add("backend", "后端服务", "ok", f"运行中 · v{APP_VERSION}")

    # ② ffmpeg / ffprobe
    from app.services.video import check_dependencies
    import re as _re
    dep = await asyncio.to_thread(check_dependencies)

    def _ver(raw: str) -> str:
        """从 ffmpeg 的版本串里只取版本号。

        我们打包的这个构建自带发布方标识，整串长这样：
        `ffmpeg version 8.1.2-https://www.martin-riedl.de Copyright (c) ...`
        直接显示会在状态面板里冒出一个陌生网址，看着像应用在连什么外部服务。
        """
        m = _re.search(r"version\s+(\S+)", raw or "")
        # 取 version 后的第一个词再按 - 截断：官方构建是 `7.1`，我们这个是
        # `8.1.2-https://...`，源码构建是 `N-12345-gabcdef`，三种都能出个能读的东西
        return m.group(1).split("-")[0] if m else "未知"

    for tool in ("ffmpeg", "ffprobe"):
        d = dep.get(tool) or {}
        add(tool, tool, "ok" if d.get("ok") else "fail",
            _ver(d.get("version")) if d.get("ok") else (d.get("error") or "不可用"),
            None if d.get("ok") else "重装应用可修复；缺了它视频裁切/合并/导出全部会失败")

    # ③ 数据目录可写：真写一个临时文件再删，只判存在不算数（只读挂载、权限变更都能建目录失败）
    data_dir = settings.UPLOAD_DIR
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd, tmp = _tf.mkstemp(dir=data_dir); os.close(fd); os.remove(tmp)
        add("datadir", "数据目录", "ok", data_dir)
    except Exception as e:
        add("datadir", "数据目录", "fail", f"{data_dir} 不可写（{type(e).__name__}）",
            "检查磁盘是否已拔出或目录权限是否被改过")

    # ④ 磁盘余量：素材是几十上百 G 的量级，5G 以下随时会在导出中途写失败
    try:
        du = _sh.disk_usage(data_dir if os.path.isdir(data_dir) else "/")
        free_gb = round(du.free / 1073741824, 1)
        lv = "fail" if free_gb < 5 else ("warn" if free_gb < 20 else "ok")
        add("disk", "磁盘余量", lv, f"剩余 {free_gb} GB",
            None if lv == "ok" else "在项目里删掉不再需要的素材，或把导出目录指到外置盘")
    except Exception as e:
        add("disk", "磁盘余量", "warn", f"读不到（{type(e).__name__}）")

    # ⑤ 数据库
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(_text("select 1"))
        add("db", "数据库", "ok", "可读写")
    except Exception as e:
        add("db", "数据库", "fail", f"打不开（{type(e).__name__}）", "数据库文件可能损坏，联系管理员")

    # ⑥ 密钥：非空检查复用 config-status 的分组
    cfg = await system_config_status()
    # 短剧成片链路（drama_video）不列在这里：它是「剧本拆解」那条业务线的配置，
    # 出现在素材制作的状态面板里只会让人以为自己少配了什么。要看它的状态去那条线里看。
    for key, name, hint in (
        ("ai_gemini", "AI 分析（Gemini）", "在设置里补全后重启应用"),
        ("ai_qwen", "AI 分析（通义）", "在设置里补全后重启应用"),
    ):
        g = cfg[key]
        add(key, name, "ok" if g["ok"] else "warn",
            "已配置" if g["ok"] else "缺 " + "、".join(g["missing"]),
            None if g["ok"] else hint)

    # ⑦ 深检：实测连通。只探到「能不能连上」为止，不做任何写操作
    if deep:
        t0 = _t.monotonic()
        try:
            from app.routers.admake import _precheck_ai_credentials
            await _precheck_ai_credentials("gemini")
            # 这个毫秒数是密钥探测的往返耗时（美区 OSS get_bucket_info + DashScope /models），
            # 不是 AI 分析本身的耗时——写清楚，不然会被当成"分析要花这么久"
            add("net_ai", "AI 链路连通", "ok",
                f"实测通过 · 密钥探测往返 {int((_t.monotonic()-t0)*1000)} ms")
        except Exception as e:
            msg = getattr(e, "detail", None) or str(e)
            add("net_ai", "AI 链路连通", "warn", str(msg)[:120], "网络或密钥有问题，AI 分析会失败")

    order = {"fail": 2, "warn": 1, "ok": 0}
    worst = max((order[i["level"]] for i in items), default=0)
    return {"level": ["ok", "warn", "fail"][worst], "deep": bool(deep), "items": items}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8764))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
