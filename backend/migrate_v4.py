#!/usr/bin/env python3
"""
迁移 v4：admake_exports 表增加 project_id 列。
"""
import os
import sqlite3

DATA_DIR = os.path.expanduser("~/Library/Application Support/SkyArtDemo/skyart-data")
DB_PATH  = os.path.join(DATA_DIR, "skyart.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cols = [row[1] for row in cur.execute("PRAGMA table_info(admake_exports)").fetchall()]
    if "project_id" in cols:
        print("project_id 列已存在，跳过")
        conn.close()
        return

    cur.execute(
        "ALTER TABLE admake_exports ADD COLUMN project_id INTEGER "
        "REFERENCES admake_projects(id) ON DELETE CASCADE"
    )

    rows = cur.execute("SELECT id, part_id FROM admake_exports").fetchall()
    for export_id, part_id in rows:
        part_row = cur.execute(
            "SELECT project_id FROM admake_parts WHERE id = ?", (part_id,)
        ).fetchone()
        if part_row:
            cur.execute(
                "UPDATE admake_exports SET project_id = ? WHERE id = ?",
                (part_row[0], export_id),
            )

    conn.commit()
    conn.close()
    print(f"已添加 project_id 列，回填了 {len(rows)} 条旧记录")
    print("迁移完毕！")


if __name__ == "__main__":
    main()
