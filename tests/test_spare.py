"""备件匹配：型号不符登记异常并拒绝；已拆下的灯泡不得重新标为在用。"""
from __future__ import annotations

from cinema import services as svc
from cinema.db import row
from cinema.services import ServiceError

from .base import (BaseCase, LAMP_DEFECTIVE, LAMP_SPARE_B2000,
                   LAMP_WRONG_MODEL, TECH_ZHANG)


class TestSpareMatching(BaseCase):

    def _wo_with_steps_12(self, projector=1):
        wo = svc.create_work_order(self.conn, self.adapter, projector, TECH_ZHANG, "换灯")
        svc.complete_step(self.conn, self.adapter, wo["id"], 1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 2)
        return wo

    def test_wrong_model_rejected_and_incident_created(self):
        """备件型号不符 → 拒绝安装并登记 spare_mismatch 异常。"""
        wo = self._wo_with_steps_12(projector=1)  # 要求 LMP-B2000
        with self.assertRaises(ServiceError) as cm:
            svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                              new_lamp_id=LAMP_WRONG_MODEL)  # LMP-X999
        self.assertEqual(cm.exception.code, "spare_mismatch")
        inc = row(self.conn, "SELECT * FROM incidents WHERE type = 'spare_mismatch'"
                             " AND work_order_id = ?", (wo["id"],))
        self.assertIsNotNone(inc)
        self.assertEqual(inc["status"], "open")
        # 步骤 3 未完成，可换正确备件重试
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_SPARE_B2000)

    def test_correct_spare_installs(self):
        """型号匹配的备件：旧灯 removed、新灯 in_use、工单记录新旧灯。"""
        wo = self._wo_with_steps_12(projector=1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_SPARE_B2000)
        old = row(self.conn, "SELECT * FROM lamps WHERE id = 1")
        new = row(self.conn, "SELECT * FROM lamps WHERE id = ?", (LAMP_SPARE_B2000,))
        self.assertEqual(old["status"], "removed")
        self.assertEqual(new["status"], "in_use")
        wo2 = svc.get_work_order(self.conn, wo["id"])
        self.assertEqual(wo2["old_lamp_id"], 1)
        self.assertEqual(wo2["new_lamp_id"], LAMP_SPARE_B2000)

    def test_removed_lamp_cannot_be_reused(self):
        """已拆下的灯泡不得重新标为在用。"""
        wo = self._wo_with_steps_12(projector=1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_SPARE_B2000)
        svc.complete_step(self.conn, self.adapter, wo["id"], 4)
        svc.complete_step(self.conn, self.adapter, wo["id"], 5,
                          photos_dir=self.photos)
        svc.approve_release(self.conn, wo["id"], approver_id=3, decision="approved")
        # 旧灯 id=1 已 removed：新工单里尝试装回 → 拒绝
        wo2 = svc.create_work_order(self.conn, self.adapter, 1, TECH_ZHANG, "再次换灯")
        svc.complete_step(self.conn, self.adapter, wo2["id"], 1)
        svc.complete_step(self.conn, self.adapter, wo2["id"], 2)
        with self.assertRaises(ServiceError) as cm:
            svc.complete_step(self.conn, self.adapter, wo2["id"], 3, new_lamp_id=1)
        self.assertEqual(cm.exception.code, "lamp_not_spare")

    def test_defective_spare_fails_screening(self):
        """缺陷备件：试映亮度不足 → 登记异常且工单不得放行。"""
        wo = self._wo_with_steps_12(projector=1)
        svc.complete_step(self.conn, self.adapter, wo["id"], 3,
                          new_lamp_id=LAMP_DEFECTIVE)
        svc.complete_step(self.conn, self.adapter, wo["id"], 4)
        wo = svc.complete_step(self.conn, self.adapter, wo["id"], 5,
                               photos_dir=self.photos)
        self.assertEqual(wo["status"], "in_progress")  # 未推进到待放行
        inc = row(self.conn, "SELECT * FROM incidents"
                             " WHERE type = 'insufficient_brightness'"
                             " AND work_order_id = ?", (wo["id"],))
        self.assertIsNotNone(inc)
        with self.assertRaises(ServiceError) as cm:
            svc.approve_release(self.conn, wo["id"], approver_id=3, decision="approved")
        self.assertEqual(cm.exception.code, "state")
