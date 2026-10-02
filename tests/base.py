"""测试公共装置：独立临时库 + 模拟适配器 + 种子数据。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cinema.adapter import MockProjectorAdapter          # noqa: E402
from cinema.db import connect, init_db                   # noqa: E402
from cinema.seed import seed                             # noqa: E402

# 种子里的关键 id
TECH_ZHANG, TECH_LI = 1, 2
SUP_WANG, SUP_ZHAO = 3, 4
HALL_CRITICAL = 2          # 2 号厅：487/500h 临界
PROJ_CRITICAL = 2
PROJ_OVER = 3              # IMAX 厅：512/500h 超阈值，首次计时器重置失败
LAMP_DEFECTIVE = 6         # B2000-DEF01 缺陷备件
LAMP_SPARE_B2000 = 5       # B2000-2025-0001 合格备件
LAMP_WRONG_MODEL = 9       # LMP-X999 型号不符备件


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.photos = os.path.join(self.tmp.name, "photos")
        self.conn = connect(os.path.join(self.tmp.name, "test.db"))
        init_db(self.conn)
        self.adapter = MockProjectorAdapter()
        seed(self.conn, self.adapter)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
