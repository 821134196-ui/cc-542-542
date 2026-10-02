"""种子数据：4 个影厅，含临界寿命样例（2 号厅 487/500h）与超阈值样例（3 号厅）。

适配器同步注入：3 号厅放映机首次计时器重置会失败（计时器重置失败样例）；
备件 LMP-B2000-DEF01 为缺陷备件（试映亮度不足样例）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .adapter import MockProjectorAdapter
from .db import now_iso

DEFECTIVE_SERIALS = {"B2000-DEF01"}  # 缺陷备件（试映亮度不足样例）


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def seed(conn, adapter: MockProjectorAdapter) -> None:
    now = datetime.now()

    users = [
        (1, "张建国", "technician"),
        (2, "李文秀", "technician"),
        (3, "王正纲", "supervisor"),   # 放映主管
        (4, "赵敏",   "supervisor"),   # 放映主管
    ]
    conn.executemany("INSERT INTO users (id, name, role) VALUES (?,?,?)", users)

    halls = [(1, "1 号厅"), (2, "2 号厅"), (3, "IMAX 厅"), (4, "4 号厅")]
    conn.executemany("INSERT INTO halls (id, name) VALUES (?,?)", halls)

    # (id, hall_id, 放映机型号, 要求灯泡型号)
    projectors = [
        (1, 1, "Barco SP4K-15",   "LMP-B2000"),
        (2, 2, "Christie CP4325", "LMP-C4500"),
        (3, 3, "Barco SP4K-40B",  "LMP-B2000"),
        (4, 4, "NEC NC2041L",     "LMP-N1800"),
    ]
    conn.executemany(
        "INSERT INTO projectors (id, hall_id, model, lamp_model) VALUES (?,?,?,?)",
        projectors)

    # (id, 序列号, 型号, 阈值h, 累计h, 状态) —— 2 号厅为临界寿命样例，IMAX 厅超阈值
    lamps = [
        (1, "B2000-2024-0117", "LMP-B2000", 500, 120, "in_use"),   # 1 号厅 正常
        (2, "C4500-2023-0882", "LMP-C4500", 500, 487, "in_use"),   # 2 号厅 临界！
        (3, "B2000-2022-0543", "LMP-B2000", 500, 512, "in_use"),   # IMAX 厅 超阈值
        (4, "N1800-2024-0320", "LMP-N1800", 400,  60, "in_use"),   # 4 号厅 正常
        # 备件
        (5, "B2000-2025-0001", "LMP-B2000", 500,   0, "spare"),
        (6, "B2000-DEF01",     "LMP-B2000", 500,   0, "spare"),    # 缺陷备件
        (7, "C4500-2025-0002", "LMP-C4500", 500,   0, "spare"),
        (8, "N1800-2025-0003", "LMP-N1800", 400,   0, "spare"),
        (9, "X999-OTHER",      "LMP-X999",  600,   0, "spare"),    # 型号不符备件
    ]
    conn.executemany(
        "INSERT INTO lamps (id, serial, model, threshold_hours, cumulative_hours, status)"
        " VALUES (?,?,?,?,?,?)", lamps)

    ts = now_iso()
    for pid, lid in [(1, 1), (2, 2), (3, 3), (4, 4)]:
        conn.execute(
            "INSERT INTO installations (projector_id, lamp_id, installed_at)"
            " VALUES (?,?,?)", (pid, lid, ts))

    # 今日场次：每厅 3 场
    movies = ["星河摆渡人", "夜航西飞", "胶片侦探", "夏日重现"]
    sid = 0
    for hall_id in (1, 2, 3, 4):
        for k in range(3):
            sid += 1
            start = now.replace(minute=0, second=0) + timedelta(hours=1 + k * 3)
            conn.execute(
                "INSERT INTO shows (id, hall_id, title, start_time, duration_min)"
                " VALUES (?,?,?,?,?)",
                (sid, hall_id, movies[(hall_id + k) % 4], _ts(start),
                 [125, 148, 102][k]))

    # 适配器设备侧状态与台账一致；3 号厅注入首次重置失败
    adapter.attach(1, lamp_hours=120)
    adapter.attach(2, lamp_hours=487)
    adapter.attach(3, lamp_hours=512, fail_reset_times=1)
    adapter.attach(4, lamp_hours=60)
    adapter.defective_serials.update(DEFECTIVE_SERIALS)
    conn.commit()
