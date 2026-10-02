"""业务规则层：寿命预测、覆盖判断、换灯工单五步、维修互斥、试映、审批放行、事故处置。"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import config, db as dbmod
from .photos import render_brightness_png
from .sim import ProjectorAdapter, TimerResetFault

SUPERVISOR_ROLE = "放映主管"


class DomainError(Exception):
    """业务规则拒绝。"""


class MaintenanceConflict(DomainError):
    """两名技术员不能同时占用同一放映机维修窗口。"""


class SpareMismatch(DomainError):
    """备件型号与放映机要求不符。"""


class OverThresholdNeedsApproval(DomainError):
    """灯泡累计小时已超厂家阈值，需放映主管批准临时使用。"""


class TimerResetFailed(DomainError):
    """换灯后计时器重置失败。"""


class BrightnessInsufficient(DomainError):
    """试映亮度不足，不得放行。"""


class ReleaseNotAllowed(DomainError):
    """放行签署不合规（换灯人自签 / 非主管 / 工单未完成）。"""


def _now(now):
    return datetime.now() if now is None else now


class Service:
    def __init__(self, db_path=None, adapter=None, now=None):
        self.db_path = db_path or config.DB_PATH
        self.now = now
        self.adapter = adapter or ProjectorAdapter(
            os.path.join(os.path.dirname(self.db_path), "adapter_state.json")
        )
        dbmod.init_db(self.db_path)
        os.makedirs(config.PHOTO_DIR, exist_ok=True)

    def _conn(self):
        return dbmod.connect(self.db_path)

    @contextmanager
    def _db(self):
        conn = dbmod.connect(self.db_path)
        try:
            with dbmod.tx(conn):
                yield conn
        finally:
            conn.close()

    def _ts(self):
        return _now(self.now).strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- 基础数据
    def add_hall(self, name):
        with self._db() as c:
            cur = c.execute("INSERT INTO halls(name) VALUES(?)", (name,))
            return cur.lastrowid

    def add_projector(self, hall_id, model, required_lamp_model, required_lumens=8000):
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO projectors(hall_id, model, required_lamp_model, required_lumens)"
                " VALUES(?,?,?,?)",
                (hall_id, model, required_lamp_model, required_lumens),
            )
            return cur.lastrowid

    def add_spare(self, serial_no, model, threshold_hours):
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO spares(serial_no, model, threshold_hours) VALUES(?,?,?)",
                (serial_no, model, threshold_hours),
            )
            return cur.lastrowid

    def install_lamp(self, projector_id, serial_no, model, lit_hours, threshold_hours,
                     lumens=10000.0):
        """登记在用灯泡（仅初始建档用；序列号不得是已拆下的灯泡）。"""
        with self._db() as c:
            row = c.execute("SELECT id, status FROM lamps WHERE serial_no=?", (serial_no,)).fetchone()
            if row and row["status"] == "removed":
                # 硬性规则：已拆下的灯泡不得重新标为在用
                raise DomainError(f"灯泡 {serial_no} 已拆下，不得重新标为在用")
            if row:
                raise DomainError(f"灯泡序列号 {serial_no} 已存在")
            cur = c.execute(
                "INSERT INTO lamps(projector_id, serial_no, model, lit_hours, threshold_hours,"
                " status, installed_at) VALUES(?,?,?,?,?, 'installed', ?)",
                (projector_id, serial_no, model, lit_hours, threshold_hours, self._ts()),
            )
            lamp_id = cur.lastrowid
        self.adapter.register(projector_id, serial_no, lit_hours, lumens)
        return lamp_id

    def schedule_show(self, hall_id, title, start_ts, duration_min):
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO shows(hall_id, title, start_ts, duration_min) VALUES(?,?,?,?)",
                (hall_id, title, start_ts, duration_min),
            )
            return cur.lastrowid

    # ---------------------------------------------------------------- 读数与工时
    def reading(self, projector_id):
        return self.adapter.get_reading(projector_id)

    def sync_runtime(self, projector_id):
        """从模拟适配器拉取读数并累计到在用灯泡。

        返回本次新增点亮小时。读数回退（非换灯场景）按计时器异常登记事故。
        """
        err = None
        delta = 0.0
        with self._db() as c:
            lamp = c.execute(
                "SELECT * FROM lamps WHERE projector_id=? AND status='installed'",
                (projector_id,),
            ).fetchone()
            if not lamp:
                raise DomainError("该放映机无在用灯泡")
            r = self.adapter.get_reading(projector_id)
            delta = r["lit_hours"] - lamp["lit_hours"]
            if delta < -1e-6 and r["serial_no"] == lamp["serial_no"]:
                self._incident(c, config.INC_RESET_FAILED, projector_id=projector_id,
                               detail=f"读数回退：库内 {lamp['lit_hours']}h，适配器 {r['lit_hours']}h")
                err = TimerResetFailed("计时器读数异常回退，疑似重置失败")
            elif delta > 0:
                c.execute("UPDATE lamps SET lit_hours=? WHERE id=?",
                          (r["lit_hours"], lamp["id"]))
        if err:
            raise err
        return max(0.0, delta)

    def screen_show(self, show_id):
        """放映结束：累计工时；熄灯中的放映机不得放映，转延误处置。"""
        outage = False
        with self._db() as c:
            show = c.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
            proj = c.execute("SELECT * FROM projectors WHERE hall_id=?",
                             (show["hall_id"],)).fetchone()
            try:
                r = self.adapter.get_reading(proj["id"])
            except KeyError:
                r = {"outage": False}
            if r.get("outage"):
                self._incident(c, config.INC_SHOW_DELAY, hall_id=show["hall_id"],
                               projector_id=proj["id"], show_id=show_id,
                               detail=f"《{show['title']}》因异常熄灯无法放映")
                c.execute("UPDATE shows SET status='delayed' WHERE id=?", (show_id,))
                outage = True
        if outage:
            raise DomainError("放映机异常熄灯，场次转延误处置")
        with self._db() as c:
            show = c.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
            proj = c.execute("SELECT * FROM projectors WHERE hall_id=?",
                             (show["hall_id"],)).fetchone()
            delta = self.sync_runtime(proj["id"])
            c.execute("UPDATE shows SET status='screened' WHERE id=?", (show_id,))
        return delta

    # ---------------------------------------------------------------- 寿命预测
    @staticmethod
    def forecast(lamp):
        threshold = lamp["threshold_hours"]
        lit = lamp["lit_hours"]
        remaining = threshold - lit
        ratio = lit / threshold if threshold else 1.0
        return {
            "remaining_hours": remaining,
            "used_ratio": ratio,
            "critical": ratio >= config.CRITICAL_RATIO and remaining > 0,
            "over_threshold": remaining <= 0,
        }

    def _active_approval(self, c, projector_id):
        row = c.execute(
            "SELECT * FROM approvals WHERE projector_id=? AND active=1 AND usable_until>?"
            " ORDER BY id DESC LIMIT 1",
            (projector_id, self._ts()),
        ).fetchone()
        return row

    def hall_status(self):
        """影厅列表：并列下一场开始时间与剩余寿命。"""
        out = []
        with self._db() as c:
            halls = c.execute("SELECT * FROM halls ORDER BY id").fetchall()
            for h in halls:
                proj = c.execute("SELECT * FROM projectors WHERE hall_id=?", (h["id"],)).fetchone()
                item = {"hall": dict(h), "projector": dict(proj) if proj else None,
                        "lamp": None, "forecast": None, "next_show": None,
                        "can_cover_next": None, "state": "无设备", "approval": None}
                if not proj:
                    out.append(item)
                    continue
                lamp = c.execute(
                    "SELECT * FROM lamps WHERE projector_id=? AND status='installed'",
                    (proj["id"],),
                ).fetchone()
                r = None
                try:
                    r = self.adapter.get_reading(proj["id"])
                except KeyError:
                    r = {"outage": False, "lumens": 0.0, "lit_hours": 0.0}
                show = c.execute(
                    "SELECT * FROM shows WHERE hall_id=? AND status IN ('scheduled','delayed')"
                    " AND start_ts>=? ORDER BY start_ts LIMIT 1",
                    (h["id"], self._ts()),
                ).fetchone()
                approval = self._active_approval(c, proj["id"])
                if lamp:
                    item["lamp"] = dict(lamp)
                    item["forecast"] = fc = self.forecast(lamp)
                    dur_h = show["duration_min"] / 60.0 if show else None
                    if show:
                        item["next_show"] = dict(show)
                        item["can_cover_next"] = (
                            not r.get("outage")
                            and (fc["remaining_hours"] >= dur_h or bool(approval))
                        )
                    if r.get("outage"):
                        item["state"] = "熄灯"
                    elif fc["over_threshold"]:
                        item["state"] = "超阈值" + ("（已批准）" if approval else "（待批准）")
                    elif fc["critical"]:
                        item["state"] = "临界"
                    else:
                        item["state"] = "正常"
                if approval:
                    item["approval"] = dict(approval)
                out.append(item)
        return out

    def can_cover(self, projector_id, duration_hours):
        """灯泡能否覆盖整场；超阈值时仅在主管批准有效期内放行。"""
        with self._db() as c:
            lamp = c.execute(
                "SELECT * FROM lamps WHERE projector_id=? AND status='installed'",
                (projector_id,),
            ).fetchone()
            try:
                r = self.adapter.get_reading(projector_id)
            except KeyError:
                r = {"outage": False, "lumens": 0.0}
            if r.get("outage"):
                return False, "异常熄灯"
            if not lamp:
                return False, "无在用灯泡"
            fc = self.forecast(lamp)
            if fc["remaining_hours"] >= duration_hours:
                return True, "寿命可覆盖整场"
            if self._active_approval(c, projector_id):
                return True, "超阈值但持主管批准，临时使用"
            return False, "剩余寿命不足且无主管批准"

    # ---------------------------------------------------------------- 事故
    def _incident(self, c, kind, detail, hall_id=None, projector_id=None,
                  ticket_id=None, show_id=None):
        cur = c.execute(
            "INSERT INTO incidents(kind, hall_id, projector_id, ticket_id, show_id,"
            " detail, created_at) VALUES(?,?,?,?,?,?,?)",
            (kind, hall_id, projector_id, ticket_id, show_id, detail, self._ts()),
        )
        return cur.lastrowid

    def report_outage(self, projector_id, detail, handled_by=None):
        """异常熄灯处置：登记事故并将未开场次转延误。"""
        self.adapter.trigger_outage(projector_id, True)
        with self._db() as c:
            proj = c.execute("SELECT * FROM projectors WHERE id=?", (projector_id,)).fetchone()
            inc_id = self._incident(c, config.INC_OUTAGE, detail=detail,
                                    hall_id=proj["hall_id"], projector_id=projector_id)
            shows = c.execute(
                "SELECT * FROM shows WHERE hall_id=? AND status='scheduled' AND start_ts>=?",
                (proj["hall_id"], self._ts()),
            ).fetchall()
            for s in shows:
                self._incident(c, config.INC_SHOW_DELAY, hall_id=proj["hall_id"],
                               projector_id=projector_id, show_id=s["id"],
                               detail=f"受异常熄灯影响，《{s['title']}》延误")
                c.execute("UPDATE shows SET status='delayed' WHERE id=?", (s["id"],))
            c.execute("UPDATE incidents SET handled_by=? WHERE id=?",
                      (handled_by, inc_id))
            return inc_id

    def delay_show(self, show_id, delay_min, reason, handled_by=None):
        """场次延误处置。"""
        with self._db() as c:
            show = c.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
            c.execute("UPDATE shows SET status='delayed', delay_min=? WHERE id=?",
                      (delay_min, show_id))
            inc_id = self._incident(c, config.INC_SHOW_DELAY, hall_id=show["hall_id"],
                                    show_id=show_id,
                                    detail=f"《{show['title']}》延误 {delay_min:g} 分钟：{reason}")
            c.execute("UPDATE incidents SET handled_by=? WHERE id=?", (handled_by, inc_id))
            return inc_id

    def resolve_incident(self, incident_id, resolution, handled_by):
        with self._db() as c:
            c.execute(
                "UPDATE incidents SET resolution=?, handled_by=?, handled_at=? WHERE id=?",
                (resolution, handled_by, self._ts(), incident_id),
            )

    def list_incidents(self, unresolved_only=False):
        q = "SELECT * FROM incidents"
        if unresolved_only:
            q += " WHERE resolution IS NULL"
        q += " ORDER BY id DESC"
        with self._db() as c:
            return [dict(r) for r in c.execute(q).fetchall()]

    # ---------------------------------------------------------------- 换灯工单
    def open_ticket(self, projector_id, technician, spare_serial):
        """开单并占用维修窗口（互斥锁）；拍摄更换前亮度照片。"""
        with self._db() as c:
            proj = c.execute("SELECT * FROM projectors WHERE id=?", (projector_id,)).fetchone()
            if not proj:
                raise DomainError("放映机不存在")
            spare = c.execute("SELECT * FROM spares WHERE serial_no=? AND used=0",
                              (spare_serial,)).fetchone()
            if not spare:
                raise DomainError(f"备件 {spare_serial} 不存在或已使用")
            # 备件型号必须与放映机要求一致；事故记录先落库再拒绝
            if spare["model"] != proj["required_lamp_model"]:
                self._incident(c, config.INC_SPARE_MISMATCH, hall_id=proj["hall_id"],
                               projector_id=projector_id,
                               detail=f"备件 {spare_serial} 型号 {spare['model']} 与要求的 "
                                      f"{proj['required_lamp_model']} 不符，已退回备件库")
                mismatch = SpareMismatch(
                    f"备件型号 {spare['model']} 与放映机要求 {proj['required_lamp_model']} 不符")
            else:
                mismatch = None
        if mismatch:
            raise mismatch

        with self._db() as c:
            proj = c.execute("SELECT * FROM projectors WHERE id=?", (projector_id,)).fetchone()
            spare = c.execute("SELECT * FROM spares WHERE id=?", (spare["id"],)).fetchone()
            reading = self.adapter.get_reading(projector_id)
            cur = c.execute(
                "INSERT INTO tickets(projector_id, hall_id, technician, spare_id,"
                " before_lumens, status, created_at, started_at)"
                " VALUES(?,?,?,?,?, 'in_progress', ?, ?)",
                (projector_id, proj["hall_id"], technician, spare["id"],
                 reading["lumens"], self._ts(), self._ts()),
            )
            ticket_id = cur.lastrowid
            try:
                c.execute(
                    "INSERT INTO maintenance_locks(projector_id, ticket_id, technician, since)"
                    " VALUES(?,?,?,?)",
                    (projector_id, ticket_id, technician, self._ts()),
                )
            except sqlite3.IntegrityError:
                row = c.execute(
                    "SELECT t.technician FROM maintenance_locks ml"
                    " JOIN tickets t ON t.id=ml.ticket_id WHERE ml.projector_id=?",
                    (projector_id,),
                ).fetchone()
                other = row["technician"] if row else "另一技术员"
                raise MaintenanceConflict(
                    f"放映机维修窗口已被 {other} 占用，{technician} 不能同时占用")
            for no, _, _ in config.REPLACE_STEPS:
                c.execute(
                    "INSERT INTO ticket_steps(ticket_id, step_no, status) VALUES(?,?,'pending')",
                    (ticket_id, no),
                )
            photo = os.path.join(config.PHOTO_DIR, f"ticket_{ticket_id}_before.png")
            render_brightness_png(photo, reading["lumens"],
                                  max_lumens=proj["required_lumens"] * 1.3,
                                  label="before")
            if reading.get("outage"):
                self._incident(c, config.INC_OUTAGE, hall_id=proj["hall_id"],
                               projector_id=projector_id, ticket_id=ticket_id,
                               detail="带异常熄灯故障开单维修")
        return ticket_id

    def _locked_ticket(self, c, ticket_id):
        t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not t:
            raise DomainError("工单不存在")
        if t["status"] != "in_progress":
            raise DomainError(f"工单状态为 {t['status']}，不可继续操作")
        return t

    def complete_step(self, ticket_id, step_no, technician, note=""):
        """按固定编号顺序完成换灯步骤，不得跳步。"""
        valid = {no for no, _, _ in config.REPLACE_STEPS}
        if step_no not in valid:
            raise DomainError(f"非法步骤编号 {step_no}")
        pending_error = None
        with self._db() as c:
            t = self._locked_ticket(c, ticket_id)
            self._require_lock_owner(c, t, technician)
            steps = {r["step_no"]: r for r in c.execute(
                "SELECT * FROM ticket_steps WHERE ticket_id=? ORDER BY step_no", (ticket_id,))}
            if steps[step_no]["status"] == "done":
                raise DomainError(f"步骤 {step_no} 已完成")
            for prev in range(1, step_no):
                if steps[prev]["status"] != "done":
                    raise DomainError(f"步骤未按顺序执行：请先完成步骤 {prev}")

            try:
                if step_no == 3:
                    self._do_install(c, t, note)
                elif step_no == 5:
                    self._do_trial(c, t, note)
                else:
                    c.execute(
                        "UPDATE ticket_steps SET status='done', done_at=?, done_by=?, note=?"
                        " WHERE ticket_id=? AND step_no=?",
                        (self._ts(), technician, note, ticket_id, step_no),
                    )
            except (TimerResetFailed, BrightnessInsufficient) as e:
                # 事故/失败试映记录保留提交，步骤保持未完成以便重试
                pending_error = e
        if pending_error:
            raise pending_error

    def _require_lock_owner(self, c, ticket, technician):
        lock = c.execute("SELECT * FROM maintenance_locks WHERE projector_id=?",
                         (ticket["projector_id"],)).fetchone()
        if not lock or lock["ticket_id"] != ticket["id"]:
            raise MaintenanceConflict("维修窗口锁丢失或不属于本工单")
        if lock["technician"] != technician:
            raise MaintenanceConflict(
                f"维修窗口由 {lock['technician']} 占用，{technician} 不能操作")

    def _do_install(self, c, ticket, note):
        """步骤 3 安装：型号已在开单时校验；计时器重置失败则灯泡不拆不换。"""
        proj = c.execute("SELECT * FROM projectors WHERE id=?",
                         (ticket["projector_id"],)).fetchone()
        spare = c.execute("SELECT * FROM spares WHERE id=?", (ticket["spare_id"],)).fetchone()
        old = c.execute(
            "SELECT * FROM lamps WHERE projector_id=? AND status='installed'",
            (proj["id"],),
        ).fetchone()
        try:
            # 先与放映机物理侧交互：重置失败时抛错，数据库不动旧灯泡
            self.adapter.swap_lamp(proj["id"], spare["serial_no"], reset_hours=0.0,
                                   lumens=10000.0)
        except TimerResetFault:
            self._incident(c, config.INC_RESET_FAILED, hall_id=proj["hall_id"],
                           projector_id=proj["id"], ticket_id=ticket["id"],
                           detail="新灯泡安装后计时器重置失败，旧灯泡保持在用，需重试重置或取消工单")
            c.execute(
                "UPDATE ticket_steps SET note=? WHERE ticket_id=? AND step_no=3",
                ("计时器重置失败，旧灯泡保持在用", ticket["id"]),
            )
            raise TimerResetFailed("计时器重置失败：请重试步骤 3，或取消工单")

        # 物理侧成功 → 原子地：旧灯拆下（且永不复用）、新灯在用、备件出库
        c.execute(
            "UPDATE lamps SET status='removed', removed_at=?, projector_id=NULL WHERE id=?",
            (self._ts(), old["id"]),
        )
        cur = c.execute(
            "INSERT INTO lamps(projector_id, serial_no, model, lit_hours, threshold_hours,"
            " status, installed_at) VALUES(?,?,?,0,?, 'installed', ?)",
            (proj["id"], spare["serial_no"], spare["model"],
             spare["threshold_hours"], self._ts()),
        )
        new_lamp_id = cur.lastrowid
        c.execute("UPDATE spares SET used=1 WHERE id=?", (spare["id"],))
        c.execute("UPDATE tickets SET new_lamp_id=? WHERE id=?", (new_lamp_id, ticket["id"]))
        c.execute(
            "UPDATE ticket_steps SET status='done', done_at=?, done_by=?, note=?"
            " WHERE ticket_id=? AND step_no=3",
            (self._ts(), ticket["technician"], note, ticket["id"]),
        )

    def _do_trial(self, c, ticket, note):
        """步骤 5 试映：前后亮度成对记录、拍照；不足则不得通过。"""
        proj = c.execute("SELECT * FROM projectors WHERE id=?",
                         (ticket["projector_id"],)).fetchone()
        r = self.adapter.get_reading(proj["id"])
        after = r["lumens"]
        required = proj["required_lumens"]
        passed = after >= required
        n = c.execute("SELECT COUNT(*) n FROM trials WHERE ticket_id=?",
                      (ticket["id"],)).fetchone()["n"] + 1
        before_photo = os.path.join(config.PHOTO_DIR, f"ticket_{ticket['id']}_before.png")
        after_photo = os.path.join(config.PHOTO_DIR, f"ticket_{ticket['id']}_trial{n}_after.png")
        render_brightness_png(after_photo, after, max_lumens=required * 1.3)
        c.execute(
            "INSERT INTO trials(ticket_id, before_lumens, after_lumens, required_lumens,"
            " passed, before_photo, after_photo, tested_by, tested_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ticket["id"], ticket["before_lumens"], after, required,
             1 if passed else 0, before_photo, after_photo,
             ticket["technician"], self._ts()),
        )
        if not passed:
            self._incident(c, config.INC_BRIGHTNESS, hall_id=ticket["hall_id"],
                           projector_id=proj["id"], ticket_id=ticket["id"],
                           detail=f"试映亮度 {after:g} lm 低于要求 {required:g} lm，"
                                  f"需重新对焦/调试后重试，不得放行")
            raise BrightnessInsufficient(
                f"试映亮度不足：{after:g} < {required:g} lm，请调试后重试步骤 5")
        c.execute(
            "UPDATE ticket_steps SET status='done', done_at=?, done_by=?, note=?"
            " WHERE ticket_id=? AND step_no=5",
            (self._ts(), ticket["technician"], note, ticket["id"]),
        )

    def cancel_ticket(self, ticket_id, technician, note):
        """换灯中途取消：只允许在旧灯拆下之前（步骤 1/2 或步骤 3 重置失败时）。"""
        with self._db() as c:
            t = self._locked_ticket(c, ticket_id)
            self._require_lock_owner(c, t, technician)
            install = c.execute(
                "SELECT status FROM ticket_steps WHERE ticket_id=? AND step_no=3",
                (ticket_id,),
            ).fetchone()
            if install["status"] == "done":
                raise DomainError("新灯泡已安装完成，不能中途取消；请完成试映与放行")
            c.execute(
                "UPDATE tickets SET status='cancelled', cancelled_at=?, cancel_note=? WHERE id=?",
                (self._ts(), note, ticket_id),
            )
            c.execute("DELETE FROM maintenance_locks WHERE projector_id=?",
                      (t["projector_id"],))
            self._incident(c, config.INC_CANCELLED, hall_id=t["hall_id"],
                           projector_id=t["projector_id"], ticket_id=ticket_id,
                           detail=f"换灯中途取消：{note}（旧灯泡保持在用）")

    def complete_ticket(self, ticket_id, technician):
        """五步全部完成后完工并释放维修窗口；随后须由他人放行。"""
        with self._db() as c:
            t = self._locked_ticket(c, ticket_id)
            self._require_lock_owner(c, t, technician)
            pending = c.execute(
                "SELECT step_no FROM ticket_steps WHERE ticket_id=? AND status='pending'",
                (ticket_id,),
            ).fetchall()
            if pending:
                raise DomainError("仍有步骤未完成，不能完工")
            c.execute(
                "UPDATE tickets SET status='completed', completed_at=? WHERE id=?",
                (self._ts(), ticket_id),
            )
            c.execute("DELETE FROM maintenance_locks WHERE projector_id=?",
                      (t["projector_id"],))
            # 维修完成意味着熄灯故障已排除
            self.adapter.trigger_outage(t["projector_id"], False)
            inc = c.execute(
                "SELECT id FROM incidents WHERE projector_id=? AND kind=? AND resolution IS NULL",
                (t["projector_id"], config.INC_OUTAGE),
            ).fetchall()
            for row in inc:
                c.execute("UPDATE incidents SET resolution=?, handled_by=?, handled_at=? WHERE id=?",
                          (f"工单 {ticket_id} 换灯修复", technician, self._ts(), row["id"]))

    def get_ticket(self, ticket_id):
        with self._db() as c:
            t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not t:
                return None
            d = dict(t)
            d["steps"] = [dict(r) for r in c.execute(
                "SELECT * FROM ticket_steps WHERE ticket_id=? ORDER BY step_no", (ticket_id,))]
            d["trials"] = [dict(r) for r in c.execute(
                "SELECT * FROM trials WHERE ticket_id=? ORDER BY id", (ticket_id,))]
            rel = c.execute("SELECT * FROM releases WHERE ticket_id=?", (ticket_id,)).fetchone()
            d["release"] = dict(rel) if rel else None
            return d

    def list_tickets(self):
        with self._db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM tickets ORDER BY id DESC").fetchall()]

    # ---------------------------------------------------------------- 试映与放行
    def approve_over_threshold(self, projector_id, approver, approver_role, reason,
                               usable_hours=24):
        """超阈值设备临时使用：必须由放映主管批准。"""
        if approver_role != SUPERVISOR_ROLE:
            raise DomainError("仅放映主管可批准超阈值设备临时使用")
        with self._db() as c:
            lamp = c.execute(
                "SELECT * FROM lamps WHERE projector_id=? AND status='installed'",
                (projector_id,),
            ).fetchone()
            fc = self.forecast(lamp)
            if not fc["over_threshold"]:
                raise DomainError("设备未超阈值，无需特批")
            until = (_now(self.now) + timedelta(hours=usable_hours)).strftime("%Y-%m-%d %H:%M:%S")
            cur = c.execute(
                "INSERT INTO approvals(projector_id, approver, approver_role, reason,"
                " approved_at, usable_until) VALUES(?,?,?,?,?,?)",
                (projector_id, approver, approver_role, reason, self._ts(), until),
            )
            return cur.lastrowid

    def release(self, ticket_id, signer, signer_role, note=""):
        """最终放行：工单须完工；换灯人不能签署；须放映主管签署。"""
        with self._db() as c:
            t = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not t:
                raise DomainError("工单不存在")
            if t["status"] != "completed":
                raise ReleaseNotAllowed("工单未完工，不能放行")
            if signer == t["technician"]:
                raise ReleaseNotAllowed(
                    f"{signer} 是换灯人，不能签署最终放行（须由他人会签）")
            if signer_role != SUPERVISOR_ROLE:
                raise ReleaseNotAllowed("最终放行须由放映主管签署")
            trial = c.execute(
                "SELECT * FROM trials WHERE ticket_id=? AND passed=1 ORDER BY id DESC LIMIT 1",
                (ticket_id,),
            ).fetchone()
            if not trial:
                raise ReleaseNotAllowed("无亮度合格的试映记录，不能放行")
            cur = c.execute(
                "INSERT INTO releases(ticket_id, signer, signer_role, note, signed_at)"
                " VALUES(?,?,?,?,?)",
                (ticket_id, signer, signer_role, note, self._ts()),
            )
            return cur.lastrowid

    # ---------------------------------------------------------------- 场次影响 / 台账
    def list_shows(self):
        with self._db() as c:
            rows = c.execute(
                "SELECT s.*, h.name hall_name FROM shows s JOIN halls h ON h.id=s.hall_id"
                " ORDER BY s.start_ts").fetchall()
            result = []
            for r in rows:
                d = dict(r)
                proj = c.execute("SELECT * FROM projectors WHERE hall_id=?",
                                 (r["hall_id"],)).fetchone()
                d["can_cover"], d["cover_reason"] = self.can_cover(
                    proj["id"], r["duration_min"] / 60.0)
                result.append(d)
            return result

    def ledger(self):
        """设备台账。"""
        with self._db() as c:
            rows = c.execute(
                "SELECT h.name hall_name, p.model projector_model, p.required_lamp_model,"
                " p.required_lumens, l.serial_no, l.model lamp_model, l.lit_hours,"
                " l.threshold_hours, l.status, l.installed_at"
                " FROM halls h JOIN projectors p ON p.hall_id=h.id"
                " LEFT JOIN lamps l ON l.projector_id=p.id AND l.status='installed'"
                " ORDER BY h.id").fetchall()
            return [dict(r) for r in rows]
