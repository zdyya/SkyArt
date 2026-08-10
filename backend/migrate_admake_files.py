#!/usr/bin/env python3
"""
迁移 AdMake 文件：将 admake/ 根目录的 UUID 文件整理到按项目分类的子目录中。

新结构：
  admake/
    project_{id}_{slug}/
      sources/  {order:02d}_{原文件名}
      parts/    Part{n}.mp4
      exports/  export_{时间戳}_{id}.mp4
    ai_tmp/     (AI 分析用临时文件，已有 finally 清理)
    orphans/    (无 DB 记录的残留文件，供人工审查)
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


def project_dir(project_id: int, title: str) -> str:
    return os.path.join(ADMAKE_ROOT, f"project_{project_id}_{slug(title)}")


def safe_move(src: str, dst: str, copy: bool = False) -> bool:
    if not os.path.exists(src):
        print(f"    [SKIP] 源文件不存在: {os.path.basename(src)}")
        return False
    if os.path.exists(dst):
        print(f"    [SKIP] 目标已存在: {os.path.basename(dst)}")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if copy:
        shutil.copy2(src, dst)
    else:
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

    # 找出所有 Part 和 Source 共享的文件路径（_set_single 情况）
    shared = set(
        row[0] for row in cur.execute(
            "SELECT p.file_path FROM admake_parts p "
            "WHERE p.file_path IN (SELECT s.file_path FROM admake_sources s)"
        ).fetchall()
    )
    if shared:
        print(f"检测到 {len(shared)} 个 Source/Part 共享文件（将 copy 给 source，move 给 part）\n")

    # 记录所有 DB 中的文件路径（用于最后识别孤儿文件）
    all_db_paths: set[str] = set()

    for proj in projects:
        pid, title = proj['id'], proj['title']
        pdir = project_dir(pid, title)
        parts_d   = os.path.join(pdir, "parts")
        sources_d = os.path.join(pdir, "sources")
        exports_d = os.path.join(pdir, "exports")
        os.makedirs(parts_d,   exist_ok=True)
        os.makedirs(sources_d, exist_ok=True)
        os.makedirs(exports_d, exist_ok=True)
        print(f"── 项目 {pid}: {title}")

        # ── Sources ──────────────────────────────────────────
        sources = cur.execute(
            "SELECT id, sort_order, original_filename, file_path "
            "FROM admake_sources WHERE project_id = ? ORDER BY sort_order",
            (pid,)
        ).fetchall()
        print(f"   Sources: {len(sources)} 个")
        for src in sources:
            if not src['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, src['file_path'])
            all_db_paths.add(old_abs)

            orig = src['original_filename'] or 'video.mp4'
            safe = re.sub(r'[^\w一-鿿\-\.]', '_', orig)[:80]
            new_name = f"{src['sort_order']:02d}_{safe}"
            new_abs  = os.path.join(sources_d, new_name)
            new_rel  = os.path.relpath(new_abs, UPLOAD_DIR)

            is_shared = src['file_path'] in shared
            ok = safe_move(old_abs, new_abs, copy=is_shared)
            action = "copy" if is_shared else "move"
            if ok:
                print(f"    [{action}] {os.path.basename(old_abs)} → sources/{new_name}")
            cur.execute(
                "UPDATE admake_sources SET file_path = ? WHERE id = ?",
                (new_rel, src['id'])
            )

        # ── Parts ─────────────────────────────────────────────
        parts = cur.execute(
            "SELECT id, part_index, file_path "
            "FROM admake_parts WHERE project_id = ? ORDER BY part_index",
            (pid,)
        ).fetchall()
        print(f"   Parts:   {len(parts)} 个")
        for part in parts:
            if not part['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, part['file_path'])
            all_db_paths.add(old_abs)

            new_name = f"Part{part['part_index'] + 1}.mp4"
            new_abs  = os.path.join(parts_d, new_name)
            new_rel  = os.path.relpath(new_abs, UPLOAD_DIR)

            ok = safe_move(old_abs, new_abs)
            if ok:
                print(f"    [move] {os.path.basename(old_abs)} → parts/{new_name}")
            cur.execute(
                "UPDATE admake_parts SET file_path = ? WHERE id = ?",
                (new_rel, part['id'])
            )

        # ── Exports ───────────────────────────────────────────
        exports = cur.execute(
            "SELECT e.id, e.file_path, e.mode "
            "FROM admake_exports e "
            "JOIN admake_parts p ON e.part_id = p.id "
            "WHERE p.project_id = ? ORDER BY e.id",
            (pid,)
        ).fetchall()
        if exports:
            print(f"   Exports: {len(exports)} 个")
        for exp in exports:
            if not exp['file_path']:
                continue
            old_abs = os.path.join(UPLOAD_DIR, exp['file_path'])
            all_db_paths.add(old_abs)

            new_name = f"{exp['mode']}_{exp['id']}.mp4"
            new_abs  = os.path.join(exports_d, new_name)
            new_rel  = os.path.relpath(new_abs, UPLOAD_DIR)

            ok = safe_move(old_abs, new_abs)
            if ok:
                print(f"    [move] {os.path.basename(old_abs)} → exports/{new_name}")
            cur.execute(
                "UPDATE admake_exports SET file_path = ? WHERE id = ?",
                (new_rel, exp['id'])
            )
        print()

    conn.commit()
    conn.close()
    print("DB 更新完成。\n")

    # ── 清理根目录孤儿文件 ────────────────────────────────────
    orphan_d = os.path.join(ADMAKE_ROOT, "orphans")
    orphans = []
    for fname in os.listdir(ADMAKE_ROOT):
        fpath = os.path.join(ADMAKE_ROOT, fname)
        if not os.path.isfile(fpath):
            continue
        if fpath not in all_db_paths:
            orphans.append(fpath)

    if orphans:
        os.makedirs(orphan_d, exist_ok=True)
        print(f"孤儿文件（无 DB 记录）: {len(orphans)} 个 → 移入 admake/orphans/")
        for fp in orphans:
            dst = os.path.join(orphan_d, os.path.basename(fp))
            safe_move(fp, dst)
    else:
        print("没有孤儿文件。")

    print("\n迁移完成！")


if __name__ == "__main__":
    main()
