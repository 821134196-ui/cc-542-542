"""维修互斥：两名技术员不能同时占用同一放映机维修窗口。"""
from __future__ import annotations

from cinema import services as svc
from cinema.services import ServiceError

from .base import BaseCase, TECH_LI, TECH_ZHANG


class TestMaintenanceMutex(BaseCase):

    def test_same_projector_second_technician_rejected(self):
        svc.open_window(self.conn, 1, TECH_ZHANG)
        with self.assertRaises(ServiceError) as cm:
            svc.open_window(self.conn, 1, TECH_LI)
        self.assertEqual(cm.exception.code, "mutex")
        self.assertIn("张建国", cm.exception.message)

    def test_different_projectors_ok(self):
        svc.open_window(self.conn, 1, TECH_ZHANG)
        svc.open_window(self.conn, 2, TECH_LI)  # 不抛异常即通过

    def test_window_reusable_after_close(self):
        svc.open_window(self.conn, 1, TECH_ZHANG)
        svc.close_window(self.conn, 1)
        svc.open_window(self.conn, 1, TECH_LI)  # 关闭后可再占用

    def test_work_order_holds_window(self):
        """开工单即占用窗口；另一技术员对同机开工单被拒。"""
        svc.create_work_order(self.conn, self.adapter, 1, TECH_ZHANG, "换灯")
        with self.assertRaises(ServiceError) as cm:
            svc.create_work_order(self.conn, self.adapter, 1, TECH_LI, "换灯")
        self.assertTrue(cm.exception.code in ("mutex", "state"))
