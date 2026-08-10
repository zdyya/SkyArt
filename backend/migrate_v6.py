#!/usr/bin/env python3
"""
迁移 v6：将 admake_projects 中 ai_model = 'gemini-2.5-flash' 更新为 'gemini-3.5-flash'。
"""
import os
import sqlite3

DATA_DIR = os.path.expanduser("~/Library/Application Support/SkyArtDemo/skyart-data")
DB_PATH  = os.path.join(DATA_DIR, "skyart.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cur.execute(
        "UPDATE admake_projects SET ai_model = 'gemini-3.5-flash' WHERE ai_model = 'gemini-2.5-flash'"
    )
    updated = cur.rowcount
    cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='admake_projects'"
    )

    conn.commit()
    conn.close()
    print(f"已将 {updated} 条记录的 ai_model 更新为 gemini-3.5-flash")
    print("迁移完毕！")


if __name__ == "__main__":
    main()
