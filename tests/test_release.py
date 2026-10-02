"""放行权限：换灯人/非主管不得签署最终放行；步骤必须按固定编号顺序；
计时器重置失败处置前不得放行。"""
from __future__ import annotations

from cinema import services as svc
from cinema.db import row
from cinema.services import ServiceError

from .base import (BaseCase, LAMP_SPARE_B2000, PROJ_OVER, SUP_WANG, SUP_ZHAO,
                   TECH_LI, TECH_ZHANG)


def _full_flow(case: BaseCase, projector=1, technician=TECH_ZHANG,
               spare=LAMP_SPARE_B2000):
    wo = svc.create_work_order(case.conn, case.adapter, projector, technician, "换灯")
    for step in (1, 2):
        svc.complete_step(case.conn, case.adapter, wo["id"], step)
    svc.complete_step(case.conn, case.adapter, wo["id"], 3, new_lamp_id=spare)
    svc.complete_step(case.conn, case.adapter, wo["id"], 4)
    wo = svc.complete_step(case.conn, case.adapter, wo["id"], 5,
                           photos_dir=case.photos)
    return wo


class TestReleasePermission(BaseCase):

    def test_technician_cannot_release(self):
        """非主管（技术员）签署放行 → 拒绝。"""
        wo = _full_flow(self)
        self.assertEqual(wo["status"], "pending_release")
        with self.assertRaises(ServiceError) as cm:
            svc.approve_release(self.conn, wo["id"], TECH_LI, "approved")
        self.assertEqual(cm.exception.code, "permission")

    def test_lamp_changer_cannot_self_release(self):
        """换灯人即使是主管，也不能签署本人工单的最终放行。"""
        wo = _full_flow(self, technician=SUP_WANG)   # 王主管亲自换灯
        with self.assertRaises(ServiceError) as cm:
            svc.approve_release(self.conn, wo["id"], SUP_WANG, "approved")
        self.assertEqual(cm.exception.code, "permission")
        self.assertIn("换灯人", cm.exception.message)
        # 另一名主管可以放行
        wo = svc.approve_release(self.conn, wo["id"], SUP_ZHAO, "approved")
        self.assertEqual(wo["status"], "released")

    def test_other_supervisor_can_release(self):
        wo = _full_flow(self)
        wo = svc.approve_release(self.conn, wo["id"], SUP_WANG, "approved", "放行")
        self.assertEqual(wo["status"], "released")
        proj = row(self.conn, "SELECT * FROM projectors WHERE id = 1")
        self.assertEqual(proj["status"], "idle")
        ap = row(self.conn, "SELECT * FROM approvals WHERE work_order_id = ?",
                 (wo["id"],))
        self.assertEqual(ap["approver_id"], SUP_WANG)

    def test_release_before_steps_done_rejected(self):
        wo = svc.create_work_order(self.conn, self.adapter, 1, TECH_ZHANG, "换灯")
        svc.complete_step(self.conn, self.adapter, wo["id"], 1)
        with self.assertRaises(ServiceError) as cm:
            svc.approve_release(self.conn, wo["id"], SUP_WANG, "approved")
        self.assertEqual(cm.exception.code, "state")

    def test_steps_must_follow_fixed_order(self):
        """固定编号步骤：未断电不能直接安装。"""
        wo = svc.create_work_order(self.conn, self.adapter, 1, TECH_ZHANG, "换灯")
        with self.assertRaises(ServiceError) as cm:
            svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                              new_lamp_id=LAMP_SPARE_B2000)
        self.assertEqual(cm.exception.code, "step_order")

    def test_timer_reset_failure_blocks_release_until_retried(self):
        """IMAX 厅首次计时器重置失败 → 登记异常、阻断放行；重试成功后放行。"""
        wo = svc.create_work_order(self.conn, self.adapter, PROJ_OVER, TECH_ZHANG, "换灯")
        svc.complete_step(self.conn, self.adapter, wo["id"], 1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 2)
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_SPARE_B2000)
        inc = row(self.conn, "SELECT * FROM incidents WHERE type = 'timer_reset_failed'"
                             " AND work_order_id = ?", (wo["id"],))
        self.assertIsNotNone(inc)
        svc.complete_step(self.conn, self.adapter, wo["id"], 4)
        wo = svc.complete_step(self.conn, self.adapter, wo["id"], 5,
                               photos_dir=self.photos)
        # 计时器未重置（设备小时仍是 512）→ 亮度不足，双重阻断
        self.assertEqual(wo["status"], "in_progress")
        with self.assertRaises(ServiceError):
            svc.approve_release(self.conn, wo["id"], SUP_WANG, "approved")
        # 处置：重试重置成功 → 复检合格 → 待放行
        svc.retry_timer_reset(self.conn, self.adapter, inc["id"])
        wo = svc.rescreen(self.conn, self.adapter, wo["id"], photos_dir=self.photos)
        self.assertEqual(wo["status"], "pending_release")
        wo = svc.approve_release(self.conn, wo["id"], SUP_WANG, "approved")
        self.assertEqual(wo["status"], "released")

    def test_cancel_midway_keeps_old_lamp_removed(self):
        """换灯中途取消：登记异常，旧灯保持 removed 不得复用，放映机停机。"""
        wo = svc.create_work_order(self.conn, self.adapter, 1, TECH_ZHANG, "换灯")
        svc.complete_step(self.conn, self.adapter, wo["id"], 1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 2)
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_SPARE_B2000)
        wo = svc.cancel_work_order(self.conn, wo["id"], "放映计划调整")
        self.assertEqual(wo["status"], "cancelled")
        old = row(self.conn, "SELECT * FROM lamps WHERE id = 1")
        self.assertEqual(old["status"], "removed")
        proj = row(self.conn, "SELECT * FROM projectors WHERE id = 1")
        self.assertEqual(proj["status"], "down")
        inc = row(self.conn, "SELECT * FROM incidents WHERE type = 'cancelled_midway'"
                             " AND work_order_id = ?", (wo["id"],))
        self.assertIsNotNone(inc)
