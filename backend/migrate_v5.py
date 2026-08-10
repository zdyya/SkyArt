#!/usr/bin/env python3
"""
迁移 v5：admake_projects 表增加 ai_model 列。
"""
import os
import sqlite3

DATA_DIR = os.path.expanduser("~/Library/Application Support/SkyArtDemo/skyart-data")
DB_PATH  = os.path.join(DATA_DIR, "skyart.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cols = [row[1] for row in cur.execute("PRAGMA table_info(admake_projects)").fetchall()]
    if "ai_model" in cols:
        print("ai_model 列已存在，跳过")
        conn.close()
        return

    cur.execute(
        "ALTER TABLE admake_projects ADD COLUMN ai_model TEXT NOT NULL DEFAULT 'gemini-2.5-flash'"
    )

    conn.commit()
    conn.close()
    print("已添加 ai_model 列，默认值 gemini-2.5-flash")
    print("迁移完毕！")


if __name__ == "__main__":
    main()
