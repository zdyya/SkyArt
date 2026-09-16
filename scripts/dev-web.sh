#!/usr/bin/env bash
# 在浏览器里跑 SkyArt，改完刷新就能看，不用重新打包。
#
# 起两个进程：后端 8764（FastAPI），前端 8088（静态文件）。
# 改前端只要刷新浏览器；改后端 uvicorn 自己热重载。
#
# 跟装好的 app 用的是同一套代码、同一个数据库。数据库位置由 DATABASE_URL 决定，
# 默认是 backend/skyart.db，跟 app 的那份（Application Support 里）是两回事——
# 想接着 app 里的数据调，把 DATABASE_URL 指过去。
#
# API Key 写在 backend/.env 里，格式见 backend/.env.example。
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONT="$ROOT/electron-app/renderer"
API_PORT="${API_PORT:-8764}"
WEB_PORT="${WEB_PORT:-8088}"

# 端口占着就先清掉。上一次没退干净的话，新起的后端会绑不上，
# 而浏览器照样连得上旧的那个，改了代码看不到效果，查半天。
for p in "$API_PORT" "$WEB_PORT"; do
  if lsof -ti ":$p" &>/dev/null; then
    echo "端口 $p 被占着，先清掉"
    kill -9 $(lsof -ti ":$p") 2>/dev/null || true
    sleep 0.5
  fi
done

if [ ! -d "$BACKEND/.venv" ]; then
  echo "建 Python 虚拟环境…"
  python3.12 -m venv "$BACKEND/.venv"
fi
"$BACKEND/.venv/bin/pip" install -q -r "$BACKEND/requirements.txt"

# API Key 直接用装好的 app 里那份配置，不让人再填一遍。
# 字段映射跟 electron-app/main.js 的 buildBackendEnv 一致，改那边这里要跟着改。
APP_CFG="$HOME/Library/Application Support/SkyArtDemo/skyart-config.json"
if [ -f "$APP_CFG" ]; then
  eval "$("$BACKEND/.venv/bin/python" - "$APP_CFG" <<'PYEOF'
import json, shlex, sys
m = {
    "happyhorseApiKey": "HAPPYHORSE_API_KEY",
    "happyhorseBaseUrl": "HAPPYHORSE_BASE_URL",
    "geminiProxyUrl": "GEMINI_PROXY_URL",
    "geminiProxyKey": "GEMINI_PROXY_KEY",
    "qwenApiKey": "QWEN_API_KEY",
    "qwenBaseUrl": "QWEN_BASE_URL",
    "ossAccessKeyId": "OSS_ACCESS_KEY_ID",
    "ossAccessKeySecret": "OSS_ACCESS_KEY_SECRET",
    "ossBucketName": "OSS_BUCKET_NAME",
    "ossEndpoint": "OSS_ENDPOINT",
    "ossKeyUsWest": "OSS_KEY_SkyArt_US",
    "ossSecretUsWest": "OSS_SECRET_SkyArt_US",
    "ossBucketUsWest": "OSS_BUCKET_SkyArt_US",
    "ossEndpointUsWest": "OSS_ENDPOINT_SkyArt_US",
}
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    cfg = {}
for k, env in m.items():
    v = cfg.get(k)
    if v:
        print(f"export {env}={shlex.quote(str(v))}")
PYEOF
)"
  echo "配置取自装好的 app：$APP_CFG"
elif [ ! -f "$BACKEND/.env" ] && [ -f "$BACKEND/.env.example" ]; then
  cp "$BACKEND/.env.example" "$BACKEND/.env"
  echo "没找到 app 的配置文件，建好了 backend/.env，填上 API Key 再重跑"
fi

trap 'echo; echo "关掉了"; kill 0; exit 0' INT TERM

cd "$BACKEND"
# --reload 只看 app/ 和 main.py：uploads 里的视频和数据库一变就重启的话，
# 传一次素材后端会重启十几次。
.venv/bin/uvicorn main:app --host 127.0.0.1 --port "$API_PORT" \
  --reload --reload-dir app --reload-include '*.py' &

cd "$FRONT"
"$BACKEND/.venv/bin/python" -m http.server "$WEB_PORT" --bind 127.0.0.1 &>/dev/null &

sleep 2
URL="http://127.0.0.1:$WEB_PORT/home.html"
echo
echo "  前端  $URL"
echo "  后端  http://127.0.0.1:$API_PORT      接口文档 /docs"
echo "  账号  admin / 123456"
echo
echo "  改前端刷新浏览器就行，改后端会自己重启。Ctrl+C 一起关。"
echo
command -v open &>/dev/null && open "$URL"

wait
