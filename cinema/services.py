"""业务规则服务层。

规则一览：
- 时长累计：场次完成后把放映时长累计到灯泡（读数以模拟适配器为准回写台账）。
- 覆盖判断：排场后判断灯泡剩余寿命能否覆盖整场。
- 维修互斥：同一放映机同一时间只允许一名技术员占用维修窗口（DB 部分唯一索引兜底）。
- 备件匹配：新灯型号必须与放映机要求一致，且必须是 spare 状态；
  已拆下（removed/failed/scrapped）的灯泡不得重新标为在用。
- 换灯五步骤固定编号：1 断电 2 冷却 3 安装 4 对焦 5 试映，必须按序完成。
- 试映亮度不足 / 计时器重置失败：产生异常，未处置前工单不得放行。
- 超阈值设备：必须由放映主管批准方可临时使用。
- 放行审批：审批人必须是放映主管，且不能是换灯人本人。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .adapter import MIN_BRIGHTNESS_FL, MockProjectorAdapter
from .db import STEP_NAMES, now_iso, row, rows
from .photos import save_brightness_photo

TIGHT_MARGIN_MIN = 30          # 覆盖余量小于 30 分钟视为"临界"
OVERRIDE_HOURS = 24            # 超阈值临时使用批准默认有效期


class ServiceError(Exception):
    """业务规则拒绝；code 供 API 映射 HTTP 状态。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 基础查询

def _must(table: str, conn, rid: int) -> dict:
    r = row(conn, f"SELECT * FROM {table} WHERE id = ?", (rid,))
    if not r:
        raise ServiceError("not_found", f"{table} #{rid} 不存在")
    return r


def _user(conn, uid: int) -> dict:
    return _must("users", conn, uid)


def current_lamp(conn, projector_id: int) -> dict | None:
    return row(conn, """
        SELECT l.* FROM lamps l
        JOIN installations i ON i.lamp_id = l.id
        WHERE i.projector_id = ? AND i.removed_at IS NULL
        ORDER BY i.installed_at DESC LIMIT 1
    """, (projector_id,))


def active_override(conn, projector_id: int) -> dict | None:
    return row(conn, """
        SELECT * FROM overrides WHERE projector_id = ? AND expires_at > ?
        ORDER BY expires_at DESC LIMIT 1
    """, (projector_id, now_iso()))


# ---------------------------------------------------------------- 时长累计 / 覆盖 / 寿命预测

def complete_show(conn, adapter: MockProjectorAdapter, show_id: int) -> dict:
    """场次结束：按场长累计灯泡点亮小时（时长累计），设备读数回写台账。"""
    show = _must("shows", conn, show_id)
    if show["status"] == "completed":
        raise ServiceError("state", f"场次 #{show_id} 已完成，不能重复累计")
    if show["status"] == "cancelled":
        raise ServiceError("state", f"场次 #{show_id} 已取消")
    proj = row(conn, "SELECT * FROM projectors WHERE hall_id = ?", (show["hall_id"],))
    lamp = current_lamp(conn, proj["id"]) if proj else None
    if not lamp:
        raise ServiceError("state", "该影厅无在用灯泡，无法累计")
    hours = round(show["duration_min"] / 60.0, 2)
    device_hours = adapter.add_runtime(proj["id"], hours)
    with conn:
        conn.execute("UPDATE lamps SET cumulative_hours = ? WHERE id = ?",
                     (device_hours, lamp["id"]))
        conn.execute("UPDATE shows SET status = 'completed' WHERE id = ?", (show_id,))
    return {"show_id": show_id, "added_hours": hours,
            "lamp_id": lamp["id"], "cumulative_hours": device_hours}


def coverage_for_show(conn, hall_id: int, show_id: int) -> dict:
    """排场后判断灯泡能否覆盖整场。"""
    show = _must("shows", conn, show_id)
    proj = row(conn, "SELECT * FROM projectors WHERE hall_id = ?", (hall_id,))
    lamp = current_lamp(conn, proj["id"]) if proj else None
    if not lamp:
        return {"verdict": "no_lamp", "label": "无在用灯泡", "show_id": show_id}
    remaining_min = (lamp["threshold_hours"] - lamp["cumulative_hours"]) * 60
    need = show["duration_min"]
    if lamp["cumulative_hours"] >= lamp["threshold_hours"]:
        if active_override(conn, proj["id"]):
            return {"verdict": "override", "label": "超阈值·主管批准临时使用",
                    "show_id": show_id, "remaining_min": round(remaining_min)}
        return {"verdict": "over_threshold", "label": "超阈值·需放映主管批准",
                "show_id": show_id, "remaining_min": round(remaining_min)}
    if remaining_min >= need + TIGHT_MARGIN_MIN:
        verdict, label = "ok", "可覆盖"
    elif remaining_min >= need:
        verdict, label = "tight", "临界·余量不足30分钟"
    else:
        verdict, label = "insufficient", "无法覆盖整场"
    return {"verdict": verdict, "label": label, "show_id": show_id,
            "remaining_min": round(remaining_min), "need_min": need}


def life_prediction(conn, projector_id: int) -> dict:
    """寿命预测：剩余小时、按当前排期还能放几场、预计触阈时间。"""
    proj = _must("projectors", conn, projector_id)
    lamp = current_lamp(conn, projector_id)
    if not lamp:
        return {"projector_id": projector_id, "lamp": None,
                "remaining_hours": None, "shows_left": 0, "eta": None}
    remaining = lamp["threshold_hours"] - lamp["cumulative_hours"]
    upcoming = rows(conn, """
        SELECT * FROM shows WHERE hall_id = ? AND status IN ('scheduled','delayed')
        ORDER BY start_time
    """, (proj["hall_id"],))
    budget = remaining
    shows_left = 0
    eta = None
    for s in upcoming:
        dur = s["duration_min"] / 60.0
        if budget - dur < 0:
            start = datetime.fromisoformat(s["start_time"])
            eta = (start + timedelta(hours=max(budget, 0))).isoformat(sep=" ")
            break
        budget -= dur
        shows_left += 1
    return {"projector_id": projector_id, "lamp": lamp,
            "remaining_hours": round(remaining, 1),
            "shows_left": shows_left, "eta": eta}


# ---------------------------------------------------------------- 维修窗口互斥

def open_window(conn, projector_id: int, technician_id: int) -> int:
    """占用维修窗口；同一放映机已被占用时拒绝（两名技术员不能同时占用）。"""
    _must("projectors", conn, projector_id)
    _user(conn, technician_id)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO maintenance_windows (projector_id, technician_id, opened_at)"
                " VALUES (?,?,?)", (projector_id, technician_id, now_iso()))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        holder = row(conn, """
            SELECT u.name FROM maintenance_windows w JOIN users u ON u.id = w.technician_id
            WHERE w.projector_id = ? AND w.closed_at IS NULL
        """, (projector_id,))
        who = holder["name"] if holder else "其他技术员"
        raise ServiceError("mutex", f"放映机 #{projector_id} 维修窗口已被 {who} 占用")


def close_window(conn, projector_id: int) -> None:
    with conn:
        conn.execute("""
            UPDATE maintenance_windows SET closed_at = ?
            WHERE projector_id = ? AND closed_at IS NULL
        """, (now_iso(), projector_id))


# ---------------------------------------------------------------- 换灯工单

def create_work_order(conn, adapter: MockProjectorAdapter,
                      projector_id: int, technician_id: int, reason: str = "") -> dict:
    """开工单 = 占用维修窗口 + 生成固定编号步骤 1..5，并记录换灯前亮度。"""
    tech = _user(conn, technician_id)
    if tech["role"] not in ("technician", "supervisor"):
        raise ServiceError("permission", "只有技术员或放映主管可以开换灯工单")
    proj = _must("projectors", conn, projector_id)
    if proj["status"] == "maintenance":
        raise ServiceError("state", "该放映机已在维修中")
    open_window(conn, projector_id, technician_id)
    brightness_before = adapter.read_brightness_fl(projector_id)
    old = current_lamp(conn, projector_id)
    try:
        with conn:
            cur = conn.execute("""
                INSERT INTO work_orders (projector_id, technician_id, reason, status,
                                         old_lamp_id, brightness_before, created_at)
                VALUES (?,?,?, 'in_progress', ?,?,?)
            """, (projector_id, technician_id, reason,
                  old["id"] if old else None, brightness_before, now_iso()))
            wo_id = cur.lastrowid
            for no, name in STEP_NAMES.items():
                conn.execute("INSERT INTO wo_steps (work_order_id, step_no, name)"
                             " VALUES (?,?,?)", (wo_id, no, name))
            conn.execute("UPDATE projectors SET status = 'maintenance' WHERE id = ?",
                         (projector_id,))
    except Exception:
        close_window(conn, projector_id)
        raise
    return get_work_order(conn, wo_id)


def get_work_order(conn, wo_id: int) -> dict:
    wo = _must("work_orders", conn, wo_id)
    wo["steps"] = rows(conn, "SELECT * FROM wo_steps WHERE work_order_id = ?"
                             " ORDER BY step_no", (wo_id,))
    wo["technician"] = row(conn, "SELECT id, name, role FROM users WHERE id = ?",
                           (wo["technician_id"],))
    wo["screenings"] = rows(conn, "SELECT * FROM screenings WHERE work_order_id = ?"
                                  " ORDER BY id", (wo_id,))
    wo["open_incidents"] = rows(conn, "SELECT * FROM incidents WHERE work_order_id = ?"
                                      " AND status = 'open'", (wo_id,))
    return wo


def complete_step(conn, adapter: MockProjectorAdapter, wo_id: int, step_no: int,
                  note: str = "", new_lamp_id: int | None = None,
                  photos_dir: str = "data/photos") -> dict:
    """按固定编号顺序完成步骤；步骤 3 触发安装，步骤 5 触发试映。"""
    wo = _must("work_orders", conn, wo_id)
    if wo["status"] != "in_progress":
        raise ServiceError("state", f"工单 #{wo_id} 当前状态 {wo['status']}，不能执行步骤")
    if step_no not in STEP_NAMES:
        raise ServiceError("state", f"步骤编号必须是 1..5，收到 {step_no}")
    steps = {s["step_no"]: s for s in rows(
        conn, "SELECT * FROM wo_steps WHERE work_order_id = ?", (wo_id,))}
    if steps[step_no]["done"]:
        raise ServiceError("state", f"步骤 {step_no}（{STEP_NAMES[step_no]}）已完成")
    for prev in range(1, step_no):
        if not steps[prev]["done"]:
            raise ServiceError("step_order",
                               f"请先完成步骤 {prev}（{STEP_NAMES[prev]}）")

    if step_no == 1:
        adapter.power_off(wo["projector_id"])
    elif step_no == 3:
        if new_lamp_id is None:
            raise ServiceError("state", "步骤 3（安装）必须指定新灯泡")
        _install_lamp(conn, adapter, wo, new_lamp_id)
    elif step_no == 5:
        _run_screening(conn, adapter, wo, photos_dir)

    with conn:
        conn.execute("UPDATE wo_steps SET done = 1, done_at = ?, note = ?"
                     " WHERE work_order_id = ? AND step_no = ?",
                     (now_iso(), note, wo_id, step_no))
        if step_no == 5:
            ok = row(conn, "SELECT 1 AS x FROM screenings WHERE work_order_id = ?"
                           " AND result = 'pass'", (wo_id,))
            blocked = row(conn, "SELECT 1 AS x FROM incidents WHERE work_order_id = ?"
                                " AND status = 'open' AND type IN"
                                " ('timer_reset_failed','insufficient_brightness')", (wo_id,))
            if ok and not blocked:
                conn.execute("UPDATE work_orders SET status = 'pending_release'"
                             " WHERE id = ?", (wo_id,))
    return get_work_order(conn, wo_id)


def _install_lamp(conn, adapter: MockProjectorAdapter, wo: dict, new_lamp_id: int) -> None:
    """步骤 3：备件匹配 + 拆旧装新 + 重置设备计时器。"""
    proj = _must("projectors", conn, wo["projector_id"])
    lamp = _must("lamps", conn, new_lamp_id)
    if lamp["model"] != proj["lamp_model"]:
        _add_incident(conn, "spare_mismatch", proj["id"], wo["id"], None,
                      f"备件 {lamp['serial']}（{lamp['model']}）与机型要求 "
                      f"{proj['lamp_model']} 不符")
        raise ServiceError("spare_mismatch",
                           f"备件型号不符：{lamp['model']} ≠ {proj['lamp_model']}，已登记异常")
    if lamp["status"] != "spare":
        raise ServiceError("lamp_not_spare",
                           f"灯泡 {lamp['serial']} 状态为 {lamp['status']}，"
                           "已拆下的灯泡不得重新标为在用")
    ts = now_iso()
    with conn:
        # 拆旧灯：状态置 removed，此后不得再回到 in_use
        if wo["old_lamp_id"]:
            conn.execute("""
                UPDATE installations SET removed_at = ?
                WHERE projector_id = ? AND lamp_id = ? AND removed_at IS NULL
            """, (ts, proj["id"], wo["old_lamp_id"]))
            conn.execute("UPDATE lamps SET status = 'removed' WHERE id = ?",
                         (wo["old_lamp_id"],))
        conn.execute("""
            INSERT INTO installations (projector_id, lamp_id, work_order_id, installed_at)
            VALUES (?,?,?,?)
        """, (proj["id"], new_lamp_id, wo["id"], ts))
        conn.execute("UPDATE lamps SET status = 'in_use', cumulative_hours = 0 WHERE id = ?",
                     (new_lamp_id,))
        conn.execute("UPDATE work_orders SET new_lamp_id = ? WHERE id = ?",
                     (new_lamp_id, wo["id"]))
    adapter.note_lamp_installed(proj["id"], lamp["serial"])
    # 设备计时器重置失败 → 登记异常，处置（重试）前工单不得放行
    if not adapter.reset_lamp_timer(proj["id"]):
        _add_incident(conn, "timer_reset_failed", proj["id"], wo["id"], None,
                      f"放映机 #{proj['id']} 灯泡计时器重置失败，需重试或人工校准")


def _run_screening(conn, adapter: MockProjectorAdapter, wo: dict, photos_dir: str) -> None:
    """步骤 5：试映，前后亮度成对记录并各拍一张本地照片。"""
    adapter.power_on(wo["projector_id"])
    after = adapter.read_brightness_fl(wo["projector_id"])
    before = wo["brightness_before"] if wo["brightness_before"] is not None else after
    result = "pass" if after >= MIN_BRIGHTNESS_FL else "insufficient"
    with conn:
        cur = conn.execute("""
            INSERT INTO screenings (work_order_id, brightness_before, brightness_after,
                                    result, created_at)
            VALUES (?,?,?,?,?)
        """, (wo["id"], before, after, result, now_iso()))
        sid = cur.lastrowid
        pb = save_brightness_photo(photos_dir, f"screen_{sid}_before", before, "换灯前")
        pa = save_brightness_photo(photos_dir, f"screen_{sid}_after", after, "换灯后")
        conn.execute("UPDATE screenings SET photo_before = ?, photo_after = ? WHERE id = ?",
                     (pb, pa, sid))
    if result == "insufficient":
        _add_incident(conn, "insufficient_brightness", wo["projector_id"], wo["id"], None,
                      f"试映亮度 {after:.1f} fL 低于合格线 {MIN_BRIGHTNESS_FL:.1f} fL")
    else:
        # 复检合格 → 自动处置该工单的"试映亮度不足"异常
        with conn:
            conn.execute("""
                UPDATE incidents SET status = 'resolved', resolution = '复检合格', resolved_at = ?
                WHERE work_order_id = ? AND type = 'insufficient_brightness' AND status = 'open'
            """, (now_iso(), wo["id"]))


def rescreen(conn, adapter: MockProjectorAdapter, wo_id: int,
             photos_dir: str = "data/photos") -> dict:
    """试映亮度不足的处置：重新对焦后复检。合格则自动关闭异常并推进待放行。"""
    wo = _must("work_orders", conn, wo_id)
    if wo["status"] != "in_progress":
        raise ServiceError("state", f"工单 #{wo_id} 当前状态 {wo['status']}，不能复检")
    step5 = row(conn, "SELECT * FROM wo_steps WHERE work_order_id = ? AND step_no = 5",
                (wo_id,))
    if not step5 or not step5["done"]:
        raise ServiceError("state", "请先完成步骤 5（试映）再复检")
    passed = row(conn, "SELECT 1 AS x FROM screenings WHERE work_order_id = ?"
                       " AND result = 'pass'", (wo_id,))
    if passed:
        raise ServiceError("state", "该工单已有合格试映，无需复检")
    _run_screening(conn, adapter, wo, photos_dir)
    _maybe_pending_release(conn, wo_id)
    return get_work_order(conn, wo_id)


def cancel_work_order(conn, wo_id: int, reason: str) -> dict:
    """换灯中途取消：登记异常；已拆下的旧灯保持 removed，不得恢复在用。"""
    wo = _must("work_orders", conn, wo_id)
    if wo["status"] in ("released", "cancelled"):
        raise ServiceError("state", f"工单 #{wo_id} 已{wo['status']}，不能取消")
    with conn:
        conn.execute("UPDATE work_orders SET status = 'cancelled', cancel_reason = ?,"
                     " closed_at = ? WHERE id = ?", (reason, now_iso(), wo_id))
        conn.execute("UPDATE projectors SET status = 'down' WHERE id = ?",
                     (wo["projector_id"],))
    close_window(conn, wo["projector_id"])
    _add_incident(conn, "cancelled_midway", wo["projector_id"], wo_id, None,
                  f"换灯中途取消：{reason}；旧灯已拆下不得复用，需重新开工单完成换灯")
    return get_work_order(conn, wo_id)


# ---------------------------------------------------------------- 放行审批 / 超阈值批准

def approve_release(conn, wo_id: int, approver_id: int,
                    decision: str, note: str = "") -> dict:
    """最终放行：审批人必须是放映主管，且不能是换灯人本人。"""
    wo = _must("work_orders", conn, wo_id)
    approver = _user(conn, approver_id)
    if approver["role"] != "supervisor":
        raise ServiceError("permission", "只有放映主管可以签署最终放行")
    if approver_id == wo["technician_id"]:
        raise ServiceError("permission", "换灯人不能签署本人工单的最终放行")
    if wo["status"] != "pending_release":
        raise ServiceError("state", "工单尚未完成全部换灯步骤或存在未处置异常")
    if decision not in ("approved", "rejected"):
        raise ServiceError("state", "decision 必须是 approved / rejected")
    with conn:
        conn.execute("INSERT INTO approvals (work_order_id, approver_id, decision, note,"
                     " created_at) VALUES (?,?,?,?,?)",
                     (wo_id, approver_id, decision, note, now_iso()))
        if decision == "approved":
            conn.execute("UPDATE work_orders SET status = 'released', closed_at = ?"
                         " WHERE id = ?", (now_iso(), wo_id))
            conn.execute("UPDATE projectors SET status = 'idle' WHERE id = ?",
                         (wo["projector_id"],))
        else:
            conn.execute("UPDATE work_orders SET status = 'in_progress' WHERE id = ?",
                         (wo_id,))
    if decision == "approved":
        close_window(conn, wo["projector_id"])
    return get_work_order(conn, wo_id)


def grant_override(conn, projector_id: int, supervisor_id: int,
                   reason: str, hours: int = OVERRIDE_HOURS) -> dict:
    """超阈值设备临时使用：必须由放映主管批准。"""
    sup = _user(conn, supervisor_id)
    if sup["role"] != "supervisor":
        raise ServiceError("permission", "超阈值临时使用必须由放映主管批准")
    _must("projectors", conn, projector_id)
    expires = (datetime.now() + timedelta(hours=hours)).replace(microsecond=0)
    with conn:
        cur = conn.execute("""
            INSERT INTO overrides (projector_id, supervisor_id, reason, expires_at, created_at)
            VALUES (?,?,?,?,?)
        """, (projector_id, supervisor_id, reason,
              expires.isoformat(sep=" "), now_iso()))
    return row(conn, "SELECT * FROM overrides WHERE id = ?", (cur.lastrowid,))


# ---------------------------------------------------------------- 异常处置

def _add_incident(conn, itype: str, projector_id, work_order_id, show_id,
                  detail: str) -> int:
    with conn:
        cur = conn.execute("""
            INSERT INTO incidents (type, projector_id, work_order_id, show_id, detail, created_at)
            VALUES (?,?,?,?,?,?)
        """, (itype, projector_id, work_order_id, show_id, detail, now_iso()))
    return cur.lastrowid


def report_lamp_failure(conn, adapter: MockProjectorAdapter,
                        projector_id: int, detail: str = "") -> dict:
    """异常熄灯：灯泡置 failed（不得复用），放映机停机，临近场次标记受影响。"""
    proj = _must("projectors", conn, projector_id)
    lamp = current_lamp(conn, projector_id)
    ts = now_iso()
    with conn:
        if lamp:
            conn.execute("UPDATE installations SET removed_at = ?"
                         " WHERE projector_id = ? AND lamp_id = ? AND removed_at IS NULL",
                         (ts, projector_id, lamp["id"]))
            conn.execute("UPDATE lamps SET status = 'failed' WHERE id = ?", (lamp["id"],))
        conn.execute("UPDATE projectors SET status = 'down' WHERE id = ?", (projector_id,))
    iid = _add_incident(conn, "lamp_failure", projector_id, None, None,
                        detail or "放映中异常熄灯")
    soon = (datetime.now() + timedelta(hours=24)).isoformat(sep=" ")
    affected = rows(conn, """
        SELECT * FROM shows WHERE hall_id = ? AND status = 'scheduled' AND start_time < ?
    """, (proj["hall_id"], soon))
    with conn:
        for s in affected:
            conn.execute("INSERT INTO show_impacts (show_id, cause, ref_id, impact, created_at)"
                         " VALUES (?,?,?,?,?)",
                         (s["id"], "lamp_failure", iid, "at_risk", ts))
    return {"incident_id": iid, "failed_lamp": lamp, "affected_shows": affected}


def retry_timer_reset(conn, adapter: MockProjectorAdapter, incident_id: int) -> dict:
    """计时器重置失败的处置：重试设备重置，成功则关闭异常。"""
    inc = _must("incidents", conn, incident_id)
    if inc["type"] != "timer_reset_failed":
        raise ServiceError("state", "该异常不是计时器重置失败")
    if inc["status"] == "resolved":
        raise ServiceError("state", "该异常已处置")
    if not adapter.reset_lamp_timer(inc["projector_id"]):
        raise ServiceError("state", "设备计时器重置再次失败，请人工校准")
    with conn:
        conn.execute("UPDATE incidents SET status = 'resolved',"
                     " resolution = '重试重置成功', resolved_at = ? WHERE id = ?",
                     (now_iso(), incident_id))
    _maybe_pending_release(conn, inc["work_order_id"])
    return row(conn, "SELECT * FROM incidents WHERE id = ?", (incident_id,))


def resolve_incident(conn, incident_id: int, resolution: str) -> dict:
    """通用处置闭环（备件型号不符、中途取消、场次延误等）。"""
    inc = _must("incidents", conn, incident_id)
    if inc["status"] == "resolved":
        raise ServiceError("state", "该异常已处置")
    with conn:
        conn.execute("UPDATE incidents SET status = 'resolved', resolution = ?,"
                     " resolved_at = ? WHERE id = ?", (resolution, now_iso(), incident_id))
    if inc["work_order_id"]:
        _maybe_pending_release(conn, inc["work_order_id"])
    return row(conn, "SELECT * FROM incidents WHERE id = ?", (incident_id,))


def _maybe_pending_release(conn, wo_id: int | None) -> None:
    """异常处置后，若工单五步骤齐全且试映合格，则推进到待放行。"""
    if not wo_id:
        return
    wo = row(conn, "SELECT * FROM work_orders WHERE id = ?", (wo_id,))
    if not wo or wo["status"] != "in_progress":
        return
    undone = row(conn, "SELECT 1 AS x FROM wo_steps WHERE work_order_id = ? AND done = 0",
                 (wo_id,))
    passed = row(conn, "SELECT 1 AS x FROM screenings WHERE work_order_id = ?"
                       " AND result = 'pass'", (wo_id,))
    blocked = row(conn, "SELECT 1 AS x FROM incidents WHERE work_order_id = ?"
                        " AND status = 'open' AND type IN"
                        " ('timer_reset_failed','insufficient_brightness')", (wo_id,))
    if not undone and passed and not blocked:
        with conn:
            conn.execute("UPDATE work_orders SET status = 'pending_release' WHERE id = ?",
                         (wo_id,))


# ---------------------------------------------------------------- 场次延误

def delay_show(conn, show_id: int, minutes: int, reason: str) -> dict:
    """场次延误：场次置 delayed，登记异常与场次影响。"""
    show = _must("shows", conn, show_id)
    if show["status"] in ("completed", "cancelled"):
        raise ServiceError("state", f"场次 #{show_id} 已{show['status']}")
    if minutes <= 0:
        raise ServiceError("state", "延误分钟数必须为正")
    iid = _add_incident(conn, "show_delay", None, None, show_id,
                        f"《{show['title']}》延误 {minutes} 分钟：{reason}")
    with conn:
        conn.execute("UPDATE shows SET status = 'delayed', delay_min = delay_min + ?"
                     " WHERE id = ?", (minutes, show_id))
        conn.execute("INSERT INTO show_impacts (show_id, cause, ref_id, impact, delay_min,"
                     " created_at) VALUES (?,?,?,?,?,?)",
                     (show_id, "show_delay", iid, "delayed", minutes, now_iso()))
    return row(conn, "SELECT * FROM shows WHERE id = ?", (show_id,))


# ---------------------------------------------------------------- 台账 / 总览

def hall_overview(conn) -> list[dict]:
    """影厅列表：并列下一场开始时间与灯泡剩余寿命。"""
    out = []
    for h in rows(conn, "SELECT * FROM halls ORDER BY id"):
        proj = row(conn, "SELECT * FROM projectors WHERE hall_id = ?", (h["id"],))
        lamp = current_lamp(conn, proj["id"]) if proj else None
        nxt = row(conn, """
            SELECT * FROM shows WHERE hall_id = ? AND status IN ('scheduled','delayed')
            ORDER BY start_time LIMIT 1
        """, (h["id"],))
        entry = {"hall": h, "projector": proj, "lamp": lamp, "next_show": nxt,
                 "coverage": None, "override": None}
        if lamp:
            entry["remaining_hours"] = round(
                lamp["threshold_hours"] - lamp["cumulative_hours"], 1)
            entry["life_pct"] = round(100 * lamp["cumulative_hours"]
                                      / lamp["threshold_hours"], 1)
        if proj:
            entry["override"] = active_override(conn, proj["id"])
        if nxt:
            entry["coverage"] = coverage_for_show(conn, h["id"], nxt["id"])
        out.append(entry)
    return out


def equipment_ledger(conn) -> list[dict]:
    """设备台账：每台放映机及其在用灯泡的型号、序列号、累计小时、阈值。"""
    out = []
    for p in rows(conn, """
            SELECT p.*, h.name AS hall_name FROM projectors p
            JOIN halls h ON h.id = p.hall_id ORDER BY p.id"""):
        lamp = current_lamp(conn, p["id"])
        spares = rows(conn, "SELECT * FROM lamps WHERE status = 'spare'"
                            " AND model = ? ORDER BY serial", (p["lamp_model"],))
        out.append({"projector": p, "lamp": lamp, "spares": spares,
                    "prediction": life_prediction(conn, p["id"])})
    return out
