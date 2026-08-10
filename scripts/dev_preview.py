#!/usr/bin/env python3
"""SkyArt 本地预览服务：起开发态后端 + 带自动刷新的前端静态服务。

用途：开发中改前端 HTML/CSS/JS 或后端代码后，浏览器里立刻能看到效果，
不必每次重新 PyInstaller + npm run pack 打整包。

    python3 scripts/dev_preview.py

开发后端占 8764，前端占 5173，都不碰已安装 App 的 8765。
"""
import argparse
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
RENDERER_DIR = REPO_DIR / "electron-app" / "renderer"
BACKEND_DIR = REPO_DIR / "backend"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"
FFMPEG_DIR = REPO_DIR / "electron-app" / "bin" / "ffmpeg-arm64"
BACKEND_LOG = REPO_DIR / "logs" / "dev-preview-backend.log"

USER_DATA_DIR = Path.home() / "Library" / "Application Support" / "SkyArtDemo"
APP_DATA_DIR = USER_DATA_DIR / "skyart-data"
CONFIG_FILE = USER_DATA_DIR / "skyart-config.json"

DEV_BACKEND_PORT = 8764
INSTALLED_APP_PORT = 8765
PREVIEW_PORT = 5173
WATCH_INTERVAL_SEC = 0.4
BACKEND_READY_TIMEOUT_SEC = 40
SSE_HEARTBEAT_SEC = 15
NO_RELOAD_FLAG = "__noreload=1"

# 注入进每个 HTML 响应，文件一变就整页重载。页面状态存在 localStorage，重载不丢标记。
LIVE_RELOAD_SNIPPET = """
<script>
(function () {
  var source = new EventSource('/__reload');
  source.onmessage = function () { location.reload(); };
  source.onerror = function () { /* 服务停了就静默重试，EventSource 自带重连 */ };
})();
</script>
"""


def is_port_listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def find_listener_pids(port):
    """返回占用某端口的进程 pid 列表；lsof 不存在或无占用时返回空列表。"""
    try:
        output = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[预览] 查端口 {port} 占用失败：{exc}", file=sys.stderr)
        return []
    return [int(line) for line in output.split() if line.strip().isdigit()]


def read_app_config():
    """读 App 密钥配置；文件缺失或格式坏时退回空配置（与 main.js readConfig 行为一致）。"""
    if not CONFIG_FILE.exists():
        print("[预览] 未找到 skyart-config.json，AI 分析类功能在预览里会报缺密钥")
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[预览] skyart-config.json 读取失败（{exc}），按空配置继续")
        return {}


def build_backend_env(use_app_data):
    """复刻 electron-app/main.js::buildBackendEnv() 的映射，另一份在那里，改这里要同步改。"""
    config = read_app_config()
    env = dict(os.environ)
    env["PATH"] = f"{FFMPEG_DIR}:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
    env["PORT"] = str(DEV_BACKEND_PORT)

    if use_app_data:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        env["UPLOAD_DIR"] = str(APP_DATA_DIR)
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{APP_DATA_DIR / 'skyart.db'}"

    env.update({
        "SECRET_KEY":            config.get("secretKey") or "skyart-default-secret-2024",
        "HAPPYHORSE_API_KEY":    config.get("happyhorseApiKey") or "",
        "HAPPYHORSE_BASE_URL":   config.get("happyhorseBaseUrl") or "https://dashscope-us.aliyuncs.com/api/v1",
        "GEMINI_PROXY_URL":      config.get("geminiProxyUrl") or "https://dscriptr.stg.shoplazza.com/skyart/gemini/analyze",
        "GEMINI_PROXY_KEY":      config.get("geminiProxyKey") or "",
        "QWEN_API_KEY":          config.get("qwenApiKey") or "",
        "QWEN_BASE_URL":         config.get("qwenBaseUrl") or "https://dashscope.aliyuncs.com/api/v1",
        "OSS_ACCESS_KEY_ID":     config.get("ossAccessKeyId") or "",
        "OSS_ACCESS_KEY_SECRET": config.get("ossAccessKeySecret") or "",
        "OSS_BUCKET_NAME":       config.get("ossBucketName") or "",
        "OSS_ENDPOINT":          config.get("ossEndpoint") or "https://oss-cn-hangzhou.aliyuncs.com",
        "OSS_CUSTOM_DOMAIN":     config.get("ossCustomDomain") or "",
        "OSS_KEY_SkyArt_US":     config.get("ossKeyUsWest") or "",
        "OSS_SECRET_SkyArt_US":  config.get("ossSecretUsWest") or "",
        "OSS_BUCKET_SkyArt_US":  config.get("ossBucketUsWest") or "saas-dscriptr-dscriptr-develop",
        "OSS_ENDPOINT_SkyArt_US": config.get("ossEndpointUsWest") or "https://oss-us-west-1.aliyuncs.com",
    })
    return env


def start_backend(use_app_data):
    """拉起开发态后端并等它就绪，返回 Popen。"""
    if not VENV_PYTHON.exists():
        raise SystemExit(f"[预览] 后端虚拟环境缺失：{VENV_PYTHON}")

    for pid in find_listener_pids(DEV_BACKEND_PORT):
        print(f"[预览] {DEV_BACKEND_PORT} 上有旧的开发后端（pid {pid}），先结束它")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise SystemExit(f"[预览] 结束 pid {pid} 失败（{exc}），请手动处理后重试")
        time.sleep(1.5)

    BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(BACKEND_LOG, "a", encoding="utf-8")
    process = subprocess.Popen(
        [str(VENV_PYTHON), "main.py"],
        cwd=str(BACKEND_DIR),
        env=build_backend_env(use_app_data),
        stdout=log_handle, stderr=subprocess.STDOUT,
    )

    deadline = time.time() + BACKEND_READY_TIMEOUT_SEC
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"[预览] 后端启动即退出，日志见 {BACKEND_LOG}")
        if is_port_listening(DEV_BACKEND_PORT):
            return process
        time.sleep(0.5)

    process.terminate()
    raise SystemExit(f"[预览] 后端 {BACKEND_READY_TIMEOUT_SEC}s 内没起来，日志见 {BACKEND_LOG}")


class ReloadBus:
    """文件变更的版本号广播，浏览器端靠 SSE 长连接订阅。"""

    def __init__(self):
        self._version = 0
        self._condition = threading.Condition()

    def bump(self):
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def current(self):
        with self._condition:
            return self._version

    def wait_for_change(self, seen_version, timeout):
        with self._condition:
            if self._version != seen_version:
                return self._version
            self._condition.wait(timeout)
            return self._version


def snapshot_renderer():
    """采集 renderer/ 下所有文件的 (路径, 修改时间, 大小)，用于比对变更。"""
    signature = {}
    for path in RENDERER_DIR.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        stat = path.stat()
        signature[str(path)] = (stat.st_mtime, stat.st_size)
    return signature


def watch_renderer(bus):
    previous = snapshot_renderer()
    while True:
        time.sleep(WATCH_INTERVAL_SEC)
        try:
            current = snapshot_renderer()
        except OSError as exc:
            print(f"[预览] 扫描 renderer 失败：{exc}", file=sys.stderr)
            continue
        if current != previous:
            changed = [Path(p).name for p in set(current) ^ set(previous)]
            changed += [Path(p).name for p in current if p in previous and current[p] != previous[p]]
            previous = current
            print(f"[预览] 检测到改动 → 通知浏览器刷新：{', '.join(sorted(set(changed))[:5])}")
            bus.bump()


class PreviewHandler(http.server.SimpleHTTPRequestHandler):
    reload_bus = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RENDERER_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 静态请求刷屏，只在出错时由上层打印

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True  # 刷新/关页会直接断连，不是异常情况

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/__reload":
            self._stream_reload_events()
            return
        local_path = Path(self.translate_path(self.path))
        if local_path.is_file() and local_path.suffix.lower() == ".html":
            # 无头浏览器截图时长连接会让页面永远处于加载中，用 __noreload=1 关掉注入
            self._serve_html(local_path, inject=NO_RELOAD_FLAG not in self.path)
            return
        super().do_GET()

    def _serve_html(self, local_path, inject):
        html = local_path.read_text(encoding="utf-8")
        if inject:
            html = (html.replace("</body>", LIVE_RELOAD_SNIPPET + "</body>", 1)
                    if "</body>" in html else html + LIVE_RELOAD_SNIPPET)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_reload_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = self.reload_bus.current()
        while True:
            latest = self.reload_bus.wait_for_change(seen, SSE_HEARTBEAT_SEC)
            payload = b"data: reload\n\n" if latest != seen else b": keep-alive\n\n"
            seen = latest
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # 页面关了或刷新了，连接自然断开


def serve_preview(bus):
    PreviewHandler.reload_bus = bus
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PREVIEW_PORT), PreviewHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    sys.stdout.reconfigure(line_buffering=True)  # 后台运行（nohup）时也要能实时看到进度
    parser = argparse.ArgumentParser(description="SkyArt 本地预览服务")
    parser.add_argument("--sandbox", action="store_true",
                        help="用仓库内的空库和 uploads/，不碰 App 真实数据")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    if is_port_listening(INSTALLED_APP_PORT):
        raise SystemExit(
            "[预览] 检测到已安装的 SkyArt 正在运行（8765）。\n"
            "       它和预览后端会同时写同一个数据库，请先退出 App 再跑本命令；\n"
            "       只想看界面不碰真实数据，加 --sandbox 参数。"
        )
    if is_port_listening(PREVIEW_PORT):
        raise SystemExit(f"[预览] 端口 {PREVIEW_PORT} 被占用，先释放：lsof -ti tcp:{PREVIEW_PORT} | xargs kill")

    backend = start_backend(use_app_data=not args.sandbox)
    bus = ReloadBus()
    threading.Thread(target=watch_renderer, args=(bus,), daemon=True).start()
    server = serve_preview(bus)

    entry_url = f"http://127.0.0.1:{PREVIEW_PORT}/home.html"
    data_label = "仓库内 uploads/（空库）" if args.sandbox else str(APP_DATA_DIR)
    print("\n".join([
        "",
        "  SkyArt 本地预览已就绪",
        f"  打开     {entry_url}",
        f"  后端     http://127.0.0.1:{DEV_BACKEND_PORT}（日志 {BACKEND_LOG}）",
        f"  数据     {data_label}",
        "  改了 renderer 下的文件会自动刷新页面；改了后端要 Ctrl-C 重跑本命令。",
        "  Ctrl-C 停止。",
        "",
    ]))
    if not args.no_open:
        webbrowser.open(entry_url)

    try:
        while backend.poll() is None:
            time.sleep(1)
        print(f"[预览] 后端进程退出（code {backend.returncode}），日志见 {BACKEND_LOG}")
    except KeyboardInterrupt:
        print("\n[预览] 收到 Ctrl-C，正在停止")
    finally:
        server.shutdown()
        if backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=8)
            except subprocess.TimeoutExpired:
                backend.kill()


if __name__ == "__main__":
    main()
