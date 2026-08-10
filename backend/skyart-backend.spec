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
