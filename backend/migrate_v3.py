#!/usr/bin/env python3
"""
迁移 v3：去掉 uploads/ 中间层，将 DramaCreate 文件放到 DramaCreate/ 下。

旧结构 (UPLOAD_DIR = skyart-data/uploads/):
  uploads/
    admake/Source|Parts|Export/project_xxx/
    clips/...
    exports/...
    images/...
    videos/...

新结构 (UPLOAD_DIR = skyart-data/):
  AdMake/Source|Parts|Export/project_xxx/
  DramaCreate/clips/...
  DramaCreate/exports/...
  DramaCreate/images/...
  DramaCreate/videos/...
"""

import os
import re
import shutil
import sqlite3

DATA_DIR  = os.path.expanduser("~/Library/Application Support/SkyArtDemo/skyart-data")
OLD_BASE  = os.path.join(DATA_DIR, "uploads")    # 旧 UPLOAD_DIR
NEW_BASE  = DATA_DIR                              # 新 UPLOAD_DIR
DB_PATH   = os.path.join(DATA_DIR, "skyart.db")


# 路径前缀映射: (旧前缀, 新前缀)
# 旧路径相对于 OLD_BASE；新路径相对于 NEW_BASE
REMAP = [
    ("admake/Source/", "AdMake/Source/"),
    ("admake/Parts/",  "AdMake/Parts/"),
    ("admake/Export/", "AdMake/Export/"),
    ("clips/",         "DramaCreate/clips/"),
    ("exports/",       "DramaCreate/exports/"),
    ("images/",        "DramaCreate/images/"),
    ("videos/",        "DramaCreate/videos/"),
    ("generated/",     "DramaCreate/generated/"),
]


def remap_path(old_rel: str) -> str | None:
    for old_prefix, new_prefix in REMAP:
        if old_rel.startswith(old_prefix):
            return new_prefix + old_rel[len(old_prefix):]
    return None


def move_tree(old_dir: str, new_dir: str):
    """Move all files from old_dir into new_dir, preserving subdirectory structure."""
    if not os.path.isdir(old_dir):
        return
    for root, dirs, files in os.walk(old_dir):
        for fname in files:
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, old_dir)
            dst = os.path.join(new_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.move(src, dst)
                print(f"  [move] {os.path.relpath(src, DATA_DIR)} → {os.path.relpath(dst, DATA_DIR)}")
            else:
                print(f"  [skip] 目标已存在: {os.path.relpath(dst, DATA_DIR)}")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ── 1. 移动物理文件 ────────────────────────────────────────
    print("=== 移动物理文件 ===")

    # AdMake: uploads/admake/ → AdMake/
    for sub in ("Source", "Parts", "Export"):
        old_dir = os.path.join(OLD_BASE, "admake", sub)
        new_dir = os.path.join(NEW_BASE, "AdMake", sub)
        if os.path.isdir(old_dir):
            print(f"\n[admake/{sub}] → [AdMake/{sub}]")
            move_tree(old_dir, new_dir)
        else:
            print(f"\n[admake/{sub}] 不存在，跳过")

    # DramaCreate subfolders
    for name in ("clips", "exports", "images", "videos"):
        old_dir = os.path.join(OLD_BASE, name)
        new_dir = os.path.join(NEW_BASE, "DramaCreate", name)
        if os.path.isdir(old_dir):
            print(f"\n[{name}] → [DramaCreate/{name}]")
            move_tree(old_dir, new_dir)
        else:
            print(f"\n[{name}] 不存在，跳过")

    # ── 2. 更新 DB 文件路径 ────────────────────────────────────
    print("\n=== 更新 DB 路径 ===")

    def update_col(table: str, col: str):
        rows = cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
        updated = 0
        for row_id, old_path in rows:
            new_path = remap_path(old_path)
            if new_path and new_path != old_path:
                cur.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (new_path, row_id))
                updated += 1
        print(f"  {table}.{col}: {len(rows)} 条, 更新 {updated} 条")

    update_col("admake_sources",   "file_path")
    update_col("admake_parts",     "file_path")
    update_col("admake_exports",   "file_path")
    update_col("episodes",         "video_path")
    update_col("timeline_clips",   "clip_video_path")
    update_col("storyboard_scenes","clip_path")
    update_col("asset_images",     "image_path")
    update_col("characters",       "base_image_path")
    update_col("scene_settings",   "base_image_path")
    update_col("props",            "base_image_path")

    # storyboard_scenes.generated_videos 是 JSON 数组
    rows = cur.execute("SELECT id, generated_videos FROM storyboard_scenes WHERE generated_videos IS NOT NULL AND generated_videos != '[]'").fetchall()
    import json
    updated_gen = 0
    for row_id, gv in rows:
        paths = json.loads(gv)
        new_paths = [remap_path(p) or p for p in paths]
        if new_paths != paths:
            cur.execute("UPDATE storyboard_scenes SET generated_videos = ? WHERE id = ?",
                        (json.dumps(new_paths, ensure_ascii=False), row_id))
            updated_gen += 1
    print(f"  storyboard_scenes.generated_videos: {len(rows)} 条, 更新 {updated_gen} 条")

    conn.commit()
    conn.close()
    print("\nDB 更新完成。")

    # ── 3. 清理空旧目录 ────────────────────────────────────────
    print("\n=== 清理旧目录 ===")
    for sub in ("admake", "clips", "exports", "images", "videos"):
        d = os.path.join(OLD_BASE, sub)
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
                print(f"  [deleted] uploads/{sub}/")
            except Exception as e:
                print(f"  [error] 无法删除 uploads/{sub}/: {e}")

    # 尝试删除 uploads/ 本身
    if os.path.isdir(OLD_BASE):
        remaining = [f for f in os.listdir(OLD_BASE) if not f.startswith('.')]
        if not remaining:
            shutil.rmtree(OLD_BASE, ignore_errors=True)
            print("  [deleted] uploads/")
        else:
            print(f"  [保留] uploads/ (仍有内容: {remaining})")

    print("\n迁移完毕！")


if __name__ == "__main__":
    main()
