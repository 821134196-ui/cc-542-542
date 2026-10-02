"""测试：时长累计、维修互斥（含并发）、备件匹配、放行权限，
以及拆下灯泡复用、超阈值审批、亮度不足、重置失败、中途取消、熄灯与延误。"""
import os
import tempfile
import threading
import unittest

from cinema_lamp import config
from cinema_lamp.service import (
    Service, DomainError, MaintenanceConflict, SpareMismatch,
    TimerResetFailed, BrightnessInsufficient, ReleaseNotAllowed,
    SUPERVISOR_ROLE,
)
from cinema_lamp.sim import ProjectorAdapter


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self._old_photo = config.PHOTO_DIR
        config.PHOTO_DIR = os.path.join(self.tmp.name, "photos")
        os.makedirs(config.PHOTO_DIR, exist_ok=True)
        self.adapter = ProjectorAdapter(None)
        self.svc = Service(db_path=self.db, adapter=self.adapter)

    def tearDown(self):
        config.PHOTO_DIR = self._old_photo
        self.tmp.cleanup()

    def make_hall(self, name="1号厅", lmodel="XL-4500", lumens_req=9000,
                  pmodel="DP-3000"):
        hid = self.svc.add_hall(name)
        pid = self.svc.add_projector(hid, pmodel, lmodel, lumens_req)
        return hid, pid

    def install(self, pid, serial="LP-1", model="XL-4500", lit=100.0,
                thr=1000.0, lumens=10000.0):
        return self.svc.install_lamp(pid, serial, model, lit, thr, lumens)

    def spare(self, serial="SP-1", model="XL-4500", thr=1000.0):
        return self.svc.add_spare(serial, model, thr)

    def full_ticket(self, pid, spare_serial, tech="张工"):
        """开单并完成全部五步，返回工单号（试映亮度默认 10000 合格）。"""
        tid = self.svc.open_ticket(pid, tech, spare_serial)
        for no in (1, 2, 3, 4, 5):
            self.svc.complete_step(tid, no, tech)
        self.svc.complete_ticket(tid, tech)
        return tid


class TestRuntime(CaseBase):
    def test_hours_accumulate_from_reading(self):
        _, pid = self.make_hall()
        self.install(pid, lit=100.0)
        self.adapter.add_runtime(pid, 2.5)  # 放映一场
        delta = self.svc.sync_runtime(pid)
        self.assertAlmostEqual(delta, 2.5)
        st = self.svc.hall_status()[0]
        self.assertAlmostEqual(st["lamp"]["lit_hours"], 102.5)

    def test_screen_show_accumulates_and_marks_screened(self):
        hid, pid = self.make_hall()
        self.install(pid, lit=100.0)
        sid = self.svc.schedule_show(hid, "测试片", "2099-01-01 10:00:00", 120)
        self.adapter.add_runtime(pid, 2.0)
        self.assertAlmostEqual(self.svc.screen_show(sid), 2.0)
        shows = [s for s in self.svc.list_shows() if s["id"] == sid]
        self.assertEqual(shows[0]["status"], "screened")

    def test_reading_rollback_is_timer_reset_failure(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-1", lit=500.0)
        # 非换灯场景读数回退 -> 计时器重置失败
        self.adapter._state[pid]["lit_hours"] = 10.0
        with self.assertRaises(TimerResetFailed):
            self.svc.sync_runtime(pid)
        kinds = [i["kind"] for i in self.svc.list_incidents()]
        self.assertIn(config.INC_RESET_FAILED, kinds)

    def test_coverage_forecast(self):
        _, pid = self.make_hall()
        self.install(pid, lit=970.0, thr=1000.0)  # 剩 30h
        ok, _ = self.svc.can_cover(pid, 2.0)
        self.assertTrue(ok)
        ok, reason = self.svc.can_cover(pid, 40.0)
        self.assertFalse(ok)
        st = self.svc.hall_status()[0]
        self.assertEqual(st["state"], "临界")


class TestMaintenanceMutex(CaseBase):
    def test_two_technicians_cannot_share_window(self):
        _, pid = self.make_hall()
        self.install(pid)
        self.spare(serial="SP-A")
        self.spare(serial="SP-B")
        tid1 = self.svc.open_ticket(pid, "张工", "SP-A")
        with self.assertRaises(MaintenanceConflict):
            self.svc.open_ticket(pid, "李工", "SP-B")
        # 工单1完工释放后，李工可以占用
        for no in (1, 2, 3, 4, 5):
            self.svc.complete_step(tid1, no, "张工")
        self.svc.complete_ticket(tid1, "张工")
        tid2 = self.svc.open_ticket(pid, "李工", "SP-B")
        self.assertTrue(tid2 > tid1)

    def test_cancel_releases_window(self):
        _, pid = self.make_hall()
        self.install(pid)
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        self.svc.complete_step(tid, 1, "张工")
        self.svc.cancel_ticket(tid, "张工", "客户到场，暂停维修")
        self.spare(serial="SP-2")
        tid2 = self.svc.open_ticket(pid, "李工", "SP-2")
        self.assertTrue(tid2)

    def test_lock_owner_only_can_operate(self):
        _, pid = self.make_hall()
        self.install(pid)
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        with self.assertRaises(MaintenanceConflict):
            self.svc.complete_step(tid, 1, "李工")  # 非占用人
        with self.assertRaises(MaintenanceConflict):
            self.svc.cancel_ticket(tid, "李工", "代为取消")

    def test_concurrent_open_only_one_wins(self):
        _, pid = self.make_hall()
        self.install(pid)
        for i in range(10):
            self.spare(serial=f"SP-{i}")
        winners, losers = [], []
        barrier = threading.Barrier(8)

        def attempt(i):
            barrier.wait()
            try:
                tid = self.svc.open_ticket(pid, f"技工{i}", f"SP-{i}")
                winners.append(tid)
            except MaintenanceConflict:
                losers.append(i)
            except Exception as e:  # 连接级竞争也不能损坏数据
                losers.append(i)
                self.fail(f"出现非互斥类异常: {e!r}")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 7)
        # 数据库中只剩一把锁
        import sqlite3
        conn = self.svc._conn()
        try:
            n = conn.execute("SELECT COUNT(*) c FROM maintenance_locks WHERE projector_id=?",
                             (pid,)).fetchone()["c"]
            self.assertEqual(n, 1)
        finally:
            conn.close()


class TestSpareMatching(CaseBase):
    def test_wrong_model_rejected_and_logged(self):
        _, pid = self.make_hall(lmodel="XL-4500")
        self.install(pid)
        self.spare(serial="SP-W", model="XL-9000")
        with self.assertRaises(SpareMismatch):
            self.svc.open_ticket(pid, "张工", "SP-W")
        inc = self.svc.list_incidents()
        self.assertTrue(any(i["kind"] == config.INC_SPARE_MISMATCH for i in inc))
        # 未占用窗口
        self.spare(serial="SP-OK", model="XL-4500")
        tid = self.svc.open_ticket(pid, "张工", "SP-OK")
        self.assertTrue(tid)

    def test_used_spare_cannot_be_reopened(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD", lit=990.0)
        self.spare(serial="SP-1")
        tid = self.full_ticket(pid, "SP-1", tech="张工")
        self.svc.release(tid, "王主管", SUPERVISOR_ROLE)
        with self.assertRaises(DomainError):
            self.svc.open_ticket(pid, "张工", "SP-1")  # 已出库


class TestReleasePermission(CaseBase):
    def setUp(self):
        super().setUp()
        _, self.pid = self.make_hall()
        self.install(self.pid, serial="LP-OLD", lit=990.0)
        self.spare()

    def test_cannot_release_before_complete(self):
        tid = self.svc.open_ticket(self.pid, "张工", "SP-1")
        with self.assertRaises(ReleaseNotAllowed):
            self.svc.release(tid, "王主管", SUPERVISOR_ROLE)

    def test_installer_cannot_sign_release(self):
        tid = self.full_ticket(self.pid, "SP-1", tech="张工")
        with self.assertRaises(ReleaseNotAllowed):
            self.svc.release(tid, "张工", SUPERVISOR_ROLE)

    def test_non_supervisor_role_rejected(self):
        tid = self.full_ticket(self.pid, "SP-1", tech="张工")
        with self.assertRaises(ReleaseNotAllowed):
            self.svc.release(tid, "李工", "放映员")

    def test_supervisor_other_than_installer_releases(self):
        tid = self.full_ticket(self.pid, "SP-1", tech="张工")
        rid = self.svc.release(tid, "王主管", SUPERVISOR_ROLE)
        self.assertTrue(rid)
        self.assertEqual(self.svc.get_ticket(tid)["release"]["signer"], "王主管")


class TestFixedStepsAndSwap(CaseBase):
    def test_steps_must_be_in_order(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD")
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        with self.assertRaises(DomainError):
            self.svc.complete_step(tid, 2, "张工")  # 跳过断电
        with self.assertRaises(DomainError):
            self.svc.complete_step(tid, 9, "张工")  # 非法编号
        self.svc.complete_step(tid, 1, "张工")
        with self.assertRaises(DomainError):
            self.svc.complete_step(tid, 3, "张工")  # 仍跳过冷却

    def test_removed_lamp_never_reinstalled(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD")
        self.spare()
        tid = self.full_ticket(pid, "SP-1", tech="张工")
        self.svc.release(tid, "王主管", SUPERVISOR_ROLE)
        with self.assertRaises(DomainError):
            self.install(pid, serial="LP-OLD")  # 已拆下序列号不得重新在用
        st = self.svc.hall_status()[0]
        self.assertEqual(st["lamp"]["serial_no"], "SP-1")

    def test_timer_reset_failure_keeps_old_lamp(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD", lit=800.0)
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        self.svc.complete_step(tid, 1, "张工")
        self.svc.complete_step(tid, 2, "张工")
        self.adapter.set_reset_failure(pid, True)
        with self.assertRaises(TimerResetFailed):
            self.svc.complete_step(tid, 3, "张工")
        # 旧灯仍在用、备件未出库、步骤3 仍 pending
        st = self.svc.hall_status()[0]
        self.assertEqual(st["lamp"]["serial_no"], "LP-OLD")
        self.assertAlmostEqual(st["lamp"]["lit_hours"], 800.0)
        t = self.svc.get_ticket(tid)
        self.assertEqual(t["steps"][2]["status"], "pending")
        # 排除故障后可重试
        self.adapter.set_reset_failure(pid, False)
        for no in (3, 4, 5):
            self.svc.complete_step(tid, no, "张工")
        self.svc.complete_ticket(tid, "张工")

    def test_cancel_after_install_forbidden(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD")
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        for no in (1, 2, 3):
            self.svc.complete_step(tid, no, "张工")
        with self.assertRaises(DomainError):
            self.svc.cancel_ticket(tid, "张工", "不想换了")

    def test_cancel_before_install_allowed_and_logged(self):
        _, pid = self.make_hall()
        self.install(pid, serial="LP-OLD")
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        self.svc.complete_step(tid, 1, "张工")
        self.svc.cancel_ticket(tid, "张工", "临时插场")
        self.assertEqual(self.svc.get_ticket(tid)["status"], "cancelled")
        kinds = [i["kind"] for i in self.svc.list_incidents()]
        self.assertIn(config.INC_CANCELLED, kinds)
        st = self.svc.hall_status()[0]
        self.assertEqual(st["lamp"]["serial_no"], "LP-OLD")  # 旧灯保持在用

    def test_trial_records_before_after_pair_and_photos(self):
        _, pid = self.make_hall(lumens_req=9000)
        self.install(pid, serial="LP-OLD", lit=990.0, lumens=6500.0)  # 更换前偏暗
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        for no in (1, 2, 3, 4):
            self.svc.complete_step(tid, no, "张工")
        self.svc.complete_step(tid, 5, "张工")  # 新灯默认 10000lm 合格
        t = self.svc.get_ticket(tid)
        tr = t["trials"][-1]
        self.assertFalse(tr["passed"]) if False else None
        self.assertEqual(tr["before_lumens"], 6500.0)
        self.assertEqual(tr["after_lumens"], 10000.0)
        self.assertTrue(tr["passed"])
        self.assertTrue(os.path.exists(tr["before_photo"]))
        self.assertTrue(os.path.exists(tr["after_photo"]))

    def test_brightness_insufficient_blocks_step(self):
        _, pid = self.make_hall(lumens_req=9000)
        self.install(pid, serial="LP-OLD", lumens=6000.0)
        self.spare()
        tid = self.svc.open_ticket(pid, "张工", "SP-1")
        for no in (1, 2, 3, 4):
            self.svc.complete_step(tid, no, "张工")
        # 让新灯试映亮度不达标
        self.adapter.set_lumens(pid, 7000.0)
        with self.assertRaises(BrightnessInsufficient):
            self.svc.complete_step(tid, 5, "张工")
        t = self.svc.get_ticket(tid)
        self.assertEqual(t["steps"][4]["status"], "pending")
        self.assertFalse(t["trials"][-1]["passed"])
        self.assertTrue(any(i["kind"] == config.INC_BRIGHTNESS for i in self.svc.list_incidents()))
        # 重新对焦后亮度恢复，可重试
        self.adapter.set_lumens(pid, 9500.0)
        self.svc.complete_step(tid, 5, "张工")
        self.svc.complete_ticket(tid, "张工")


class TestOverThresholdApproval(CaseBase):
    def test_over_threshold_blocks_until_supervisor_approval(self):
        _, pid = self.make_hall()
        self.install(pid, lit=1050.0, thr=1000.0)  # 已超阈值
        ok, reason = self.svc.can_cover(pid, 1.0)
        self.assertFalse(ok)
        st = self.svc.hall_status()[0]
        self.assertIn("超阈值", st["state"])
        with self.assertRaises(DomainError):
            self.svc.approve_over_threshold(pid, "李工", "放映员", "通融")
        self.svc.approve_over_threshold(pid, "王主管", SUPERVISOR_ROLE, "备件在途，临时使用一场")
        ok, reason = self.svc.can_cover(pid, 1.0)
        self.assertTrue(ok)
        st = self.svc.hall_status()[0]
        self.assertIn("已批准", st["state"])

    def test_approval_not_needed_under_threshold(self):
        _, pid = self.make_hall()
        self.install(pid, lit=500.0, thr=1000.0)
        with self.assertRaises(DomainError):
            self.svc.approve_over_threshold(pid, "王主管", SUPERVISOR_ROLE, "无需批准")


class TestOutageAndDelay(CaseBase):
    def test_outage_delays_shows_and_blocks_screening(self):
        hid, pid = self.make_hall()
        self.install(pid)
        sid = self.svc.schedule_show(hid, "悬疑夜", "2099-01-01 20:00:00", 130)
        self.svc.report_outage(pid, "放映中熄灭")
        shows = {s["id"]: s for s in self.svc.list_shows()}
        self.assertEqual(shows[sid]["status"], "delayed")
        self.assertFalse(shows[sid]["can_cover"])
        # 影厅概览中下一场覆盖判断也必须为否
        st = self.svc.hall_status()[0]
        self.assertEqual(st["state"], "熄灯")
        self.assertFalse(st["can_cover_next"])
        with self.assertRaises(DomainError):
            self.svc.screen_show(sid)
        kinds = [i["kind"] for i in self.svc.list_incidents()]
        self.assertIn(config.INC_OUTAGE, kinds)
        self.assertIn(config.INC_SHOW_DELAY, kinds)

    def test_manual_delay(self):
        hid, _ = self.make_hall()
        sid = self.svc.schedule_show(hid, "晚场", "2099-01-01 19:00:00", 100)
        self.svc.delay_show(sid, 25, "前序延误", handled_by="场务")
        shows = {s["id"]: s for s in self.svc.list_shows()}
        self.assertEqual(shows[sid]["status"], "delayed")
        self.assertEqual(shows[sid]["delay_min"], 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
