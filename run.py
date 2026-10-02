#!/usr/bin/env python3
"""一条命令启动：python3 run.py
建库（SQLite）→ 写入含临界寿命样例的种子数据 → 启动本地服务。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cinema.server import create_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="影城放映机灯泡管理系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/cinema.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reset", action="store_true", help="删除现有数据库重新初始化")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)

    server = create_server(args.db, args.photos, args.port)
    print(f"▶ 影城放映机灯泡管理系统  http://127.0.0.1:{args.port}")
    print(f"  数据库 {args.db} ｜ 亮度照片 {args.photos}/")
    print("  样例：2 号厅灯泡 487/500h（临界），IMAX 厅 512/500h（超阈值）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
