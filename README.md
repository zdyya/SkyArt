# SkyArt 天工

短视频广告素材的批量剪辑工具，Electron 加 Python 的本地桌面应用。

## 三个模块

- **爆款素材分析** —— 把已投放的爆款素材拆成段，逐段标出剧情内容和剧情结构，存成可以套用的模板。
- **素材制作 2.0** —— 选一个爆款模板，每一格按模板写的条件从素材里挑段填进去，一次导出多条。
- **素材制作 1.0** —— AI 标注高光与钩子片段，按多种剪辑模式批量合成，产出可直接投放的广告素材。

短剧创作模块尚未开放。

## 安装

1. 到 [Releases](../../releases) 下载最新的 `SkyArt-x.x.x-arm64.dmg`。CI 只出 Apple 芯片包，Intel 机器要自己打，见下面的「构建」。
2. 拖进「应用程序」文件夹。
3. 首次打开提示「无法验证开发者」时，终端执行：

```bash
xattr -dr com.apple.quarantine /Applications/SkyArt.app
```

系统要求 macOS 13 Ventura 及以上。ffmpeg 已经打进包里，不用另外装。

## 构建

推送 `v` 开头的标签触发 GitHub Actions 打包，runner 是 macos-15，产物直接发到 Releases。

```bash
git tag v1.1.1 && git push origin v1.1.1
```

标签名不带 `-beta` 且仓库 Variables 里有 `FEISHU_NOTIFY=true` 时，打包完会往飞书群发一条发版通知。默认不发。

本地打包：

```bash
cd electron-app
npm run pack        # Apple 芯片
npm run pack:x64    # Intel，只能在 Intel 机器上打
```

版本号写在两处，必须一致，否则打包会被 `verify-build.js` 拦下：`electron-app/package.json` 的 `version` 和 `backend/main.py` 的 `APP_VERSION`。

## 本地开发

在浏览器里跑，改完刷新就能看：

```bash
./scripts/dev-web.sh
```

后端起在 8764，前端起在 8088，打开 <http://127.0.0.1:8088/home.html> 即可。浏览器里会用演示账号直接进首页。API Key 取自装好的 app 的配置，没装过 app 就写进 `backend/.env`，格式见 `backend/.env.example`。

数据库默认是 `backend/skyart.db`，和装好的 app 那份（在 Application Support 里）互不影响。

跑 Electron 本体：

```bash
cd backend && .venv/bin/python main.py    # 后端
cd electron-app && npm start              # 前端
```

## 遇到问题

菜单「视图 → 打开日志目录」，把当天的 `backend-YYYYMMDD.log` 发给开发排查。
