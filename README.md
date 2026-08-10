 # SkyArt 天工短剧

AI 短剧创作平台（Electron + Python 本地桌面应用）。

## 系统要求

- macOS 13 Ventura 及以上（Apple 芯片 / Intel 均支持）
- 无需手动安装 ffmpeg，App 内置

## 安装

1. 从 [Actions Artifacts](../../actions) 下载最新 `SkyArt-x.x.x-arm64.dmg`
2. 拖入「应用程序」文件夹
3. 首次打开若提示"无法验证开发者"，终端执行：
   ```
   xattr -dr com.apple.quarantine /Applications/SkyArt.app
   ```

## 构建

正式包通过 GitHub Actions 自动构建（macos-14 runner，兼容 macOS 13+），推送到 main 后自动触发，无需手动打包。

本地开发调试：

```bash
# 后端
cd backend && .venv/bin/python main.py

# 前端
cd electron-app && npm start
```

## 遇到问题

菜单「视图 → 打开日志目录」，把当天的 `backend-YYYYMMDD.log` 发给开发排查。
