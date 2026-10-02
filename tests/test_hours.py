"""时长累计：场次完成后累计点亮小时、剩余寿命与覆盖判断联动、超阈值审批。"""
from __future__ import annotations

from cinema import services as svc
from cinema.db import row
from cinema.services import ServiceError

from .base import (BaseCase, HALL_CRITICAL, PROJ_CRITICAL, PROJ_OVER,
                   SUP_WANG, TECH_ZHANG)


class TestHoursAccumulation(BaseCase):

    def test_complete_show_accumulates_hours(self):
        """完成一场 125 分钟的场次 → 灯泡累计 +2.08h，设备读数回写台账。"""
        before = svc.current_lamp(self.conn, 1)["cumulative_hours"]
        r = svc.complete_show(self.conn, self.adapter, show_id=1)
        self.assertAlmostEqual(r["added_hours"], round(125 / 60, 2))
        after = svc.current_lamp(self.conn, 1)["cumulative_hours"]
        self.assertAlmostEqual(after, before + round(125 / 60, 2), places=2)
        # 台账与设备读数一致
        self.assertAlmostEqual(after, self.adapter.read_lamp_hours(1))

    def test_completed_show_cannot_accumulate_twice(self):
        svc.complete_show(self.conn, self.adapter, show_id=1)
        with self.assertRaises(ServiceError):
            svc.complete_show(self.conn, self.adapter, show_id=1)

    def test_remaining_life_decreases_with_accumulation(self):
        lamp = svc.current_lamp(self.conn, PROJ_CRITICAL)
        remaining0 = lamp["threshold_hours"] - lamp["cumulative_hours"]  # 13h
        svc.complete_show(self.conn, self.adapter, show_id=4)  # 2 号厅第一场 125min
        lamp = svc.current_lamp(self.conn, PROJ_CRITICAL)
        remaining1 = lamp["threshold_hours"] - lamp["cumulative_hours"]
        self.assertAlmostEqual(remaining0 - remaining1, round(125 / 60, 2), places=2)

    def test_coverage_verdicts(self):
        """临界厅：普通场可覆盖；超长场无法覆盖。"""
        ok = svc.coverage_for_show(self.conn, HALL_CRITICAL, show_id=4)  # 125min
        self.assertEqual(ok["verdict"], "ok")
        # 排一场 900 分钟的马拉松场 → 剩余 780 分钟不够
        self.conn.execute(
            "INSERT INTO shows (hall_id, title, start_time, duration_min)"
            " VALUES (?, '马拉松连映', '2026-10-02 23:00:00', 900)", (HALL_CRITICAL,))
        self.conn.commit()
        long_id = row(self.conn, "SELECT id FROM shows WHERE title = '马拉松连映'")["id"]
        bad = svc.coverage_for_show(self.conn, HALL_CRITICAL, long_id)
        self.assertEqual(bad["verdict"], "insufficient")

    def test_over_threshold_requires_supervisor_override(self):
        """超阈值设备：未批准 → over_threshold；技术员批准被拒；主管批准 → override。"""
        v = svc.coverage_for_show(self.conn, 3, show_id=7)  # IMAX 厅第一场
        self.assertEqual(v["verdict"], "over_threshold")
        with self.assertRaises(ServiceError) as cm:
            svc.grant_override(self.conn, PROJ_OVER, TECH_ZHANG, "顶一场")
        self.assertEqual(cm.exception.code, "permission")
        svc.grant_override(self.conn, PROJ_OVER, SUP_WANG, "黄金场临时使用")
        v = svc.coverage_for_show(self.conn, 3, show_id=7)
        self.assertEqual(v["verdict"], "override")

    def test_life_prediction(self):
        """寿命预测：2 号厅剩余 13h，按排期（125+148+102min）可放 3 场。"""
        p = svc.life_prediction(self.conn, PROJ_CRITICAL)
        self.assertEqual(p["remaining_hours"], 13.0)
        self.assertEqual(p["shows_left"], 3)
        self.assertIsNone(p["eta"])
