"""一条本地命令启动：python run.py

- 首次运行或未带 --keep：重置并写入临界寿命样例数据
- 然后启动本地 Web 界面（默认 http://127.0.0.1:8000）
"""
import argparse
import sys

from cinema_lamp import config
from cinema_lamp.seed import seed
from cinema_lamp import web


def main(argv=None):
    ap = argparse.ArgumentParser(description="影城放映机灯泡管理系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--keep", action="store_true", help="保留现有数据，不重新播种")
    args = ap.parse_args(argv)

    import os
    need_seed = (not args.keep) or (not os.path.exists(config.DB_PATH))
    if need_seed:
        print("初始化临界寿命样例数据 …")
        seed(reset=not args.keep)
    else:
        print("保留现有数据启动 …")

    try:
        web.serve(args.host, args.port)
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
