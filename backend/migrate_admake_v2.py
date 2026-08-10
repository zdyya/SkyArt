#!/usr/bin/env python3
"""
迁移 AdMake 文件 v2：将旧的"按项目分组"结构迁移到新的"按文件类型分组"结构。

旧结构：
  admake/
    project_{id}_{slug}/
      sources/  文件...
      parts/    文件...
      exports/  文件...

新结构：
  admake/
    Source/
      project_{id}_{slug}/  文件...
    Parts/
      project_{id}_{slug}/  文件...
    Export/
      project_{id}_{slug}/  文件...
    ai_tmp/   (不变)
    orphans/  (不变)
"""

import os
import re
import shutil
import sqlite3

DB_PATH = os.path.expanduser(
    "~/Library/Application Support/SkyArtDemo/skyart-data/skyart.db"
)
UPLOAD_DIR = os.path.expanduser(
    "~/Library/Application Support/SkyArtDemo/skyart-data/uploads"
)
ADMAKE_ROOT = os.path.join(UPLOAD_DIR, "admake")


def slug(text: str) -> str:
    return re.sub(r'[^\w一-鿿\-]', '_', text).strip('_')[:50]


def safe_move(src: str, dst: str) -> bool:
    if not os.path.exists(src):
        print(f"    [SKIP] 源文件不存在: {src}")
        return False
    if os.path.exists(dst):
        print(f"    [SKIP] 目标已存在: {dst}")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return True


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    projects = cur.execute(
        "SELECT id, title FROM admake_projects ORDER BY id"
    ).fetchall()
    print(f"找到 {len(projects)} 个项目\n")

    for proj in projects:
        pid, title = proj['id'], proj['title']
        proj_slug = f"project_{pid}_{slug(title)}"
        old_proj_dir = os.path.join(ADMAKE_ROOT, proj_slug)

        print(f"── 项目 {pid}: {title}")

        # ── Sources ──────────────────────────────────────────────────
        sources = cur.execute(
            "SELECT id, file_path FROM admake_sources WHERE project_id = ?",
            (pid,)
        ).fetchall()
        print(f"   Sources: {len(sources)} 个")
        for src in sources:
            if not src['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, src['file_path'])
            fname = os.path.basename(old_abs)
            new_abs = os.path.join(ADMAKE_ROOT, "Source", proj_slug, fname)
            new_rel = os.path.relpath(new_abs, UPLOAD_DIR)

            if src['file_path'] == new_rel:
                print(f"    [OK] 已是新路径: {fname}")
                continue
            ok = safe_move(old_abs, new_abs)
            if ok:
                print(f"    [move] {src['file_path']} → {new_rel}")
            cur.execute(
                "UPDATE admake_sources SET file_path = ? WHERE id = ?",
                (new_rel, src['id'])
            )

        # ── Parts ─────────────────────────────────────────────────────
        parts = cur.execute(
            "SELECT id, file_path FROM admake_parts WHERE project_id = ?",
            (pid,)
        ).fetchall()
        print(f"   Parts:   {len(parts)} 个")
        for part in parts:
            if not part['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, part['file_path'])
            fname = os.path.basename(old_abs)
            new_abs = os.path.join(ADMAKE_ROOT, "Parts", proj_slug, fname)
            new_rel = os.path.relpath(new_abs, UPLOAD_DIR)

            if part['file_path'] == new_rel:
                print(f"    [OK] 已是新路径: {fname}")
                continue
            ok = safe_move(old_abs, new_abs)
            if ok:
                print(f"    [move] {part['file_path']} → {new_rel}")
            cur.execute(
                "UPDATE admake_parts SET file_path = ? WHERE id = ?",
                (new_rel, part['id'])
            )

        # ── Exports ───────────────────────────────────────────────────
        exports = cur.execute(
            "SELECT e.id, e.file_path "
            "FROM admake_exports e "
            "JOIN admake_parts p ON e.part_id = p.id "
            "WHERE p.project_id = ?",
            (pid,)
        ).fetchall()
        if exports:
            print(f"   Exports: {len(exports)} 个")
        for exp in exports:
            if not exp['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, exp['file_path'])
            fname = os.path.basename(old_abs)
            new_abs = os.path.join(ADMAKE_ROOT, "Export", proj_slug, fname)
            new_rel = os.path.relpath(new_abs, UPLOAD_DIR)

            if exp['file_path'] == new_rel:
                print(f"    [OK] 已是新路径: {fname}")
                continue
            ok = safe_move(old_abs, new_abs)
            if ok:
                print(f"    [move] {exp['file_path']} → {new_rel}")
            cur.execute(
                "UPDATE admake_exports SET file_path = ? WHERE id = ?",
                (new_rel, exp['id'])
            )

        # 清理空旧目录
        for sub in ("sources", "parts", "exports"):
            d = os.path.join(old_proj_dir, sub)
            if os.path.isdir(d):
                try:
                    os.rmdir(d)
                    print(f"    [rmdir] {os.path.relpath(d, ADMAKE_ROOT)}")
                except OSError:
                    print(f"    [非空，保留] {os.path.relpath(d, ADMAKE_ROOT)}")
        if os.path.isdir(old_proj_dir):
            try:
                os.rmdir(old_proj_dir)
                print(f"    [rmdir] {proj_slug}/")
            except OSError:
                print(f"    [非空，保留] {proj_slug}/")
        print()

    conn.commit()
    conn.close()
    print("DB 更新完成。迁移完毕！")


if __name__ == "__main__":
    main()
