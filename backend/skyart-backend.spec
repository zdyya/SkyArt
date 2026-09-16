# -*- mode: python ; coding: utf-8 -*-
# onedir 模式：不再打包成单文件，避免每次启动解压（30s → 3-5s）

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # prompts/ 目录包含 AI 分析用的提示词文件，需随 bundle 一起打包
        ('../prompts', 'prompts'),
        # 成片模板花字字体（app/fonts/*.ttf），编辑页预览 + 导出渲染都要用
        ('app/fonts', 'app/fonts'),
        # 拆解管线的 OCR 程序。它是编好的 arm64 二进制，不是 .py，PyInstaller
        # 不会跟着 import 收进来，要显式打进去。decompose/环境.py 的 ocr程序()
        # 按「跟本模块同目录」找它，所以目标路径必须是 app/services/decompose。
        # 打包会丢执行位，afterPack.js 里补 chmod。
        ('app/services/decompose/ocrbin', 'app/services/decompose'),
        # 随包发的剪辑模板。模板库空着的话按模板剪没有任何东西可选，等于走不通，
        # 而写一个模板要填三格条件，不该是用户拿到工具的第一件事。
        ('app/data', 'app/data'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'aiosqlite',
        'oss2',
        'httpx',
        'passlib.handlers.bcrypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],           # onedir: 不把 binaries/datas 打进 exe
    exclude_binaries=True,
    name='skyart-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='skyart-backend',   # 输出目录名: dist/skyart-backend/
)
