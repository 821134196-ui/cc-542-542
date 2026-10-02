"""初始化样例数据：包含临界寿命、超阈值、异常熄灯等样例。"""
import os
from datetime import datetime, timedelta

from . import config
from .service import Service
from .sim import ProjectorAdapter

BASE = datetime.now().replace(second=0, microsecond=0)


def _t(hours_ahead, minute_offset=0, day_offset=0):
    d = BASE + timedelta(days=day_offset, hours=hours_ahead, minutes=minute_offset)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def seed(reset=True):
    if reset and os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    adapter_path = os.path.join(config.DATA_DIR, "adapter_state.json")
    if reset and os.path.exists(adapter_path):
        os.remove(adapter_path)
    config.ensure_dirs()

    adapter = ProjectorAdapter(adapter_path)
    svc = Service(adapter=adapter)

    # 影厅 + 放映机（型号 / 要求灯泡型号 / 要求亮度）
    halls = [
        # 名称,         放映机型号,    灯泡型号,    要求亮度, (灯泡序列号, 已点小时, 阈值, 当前亮度)
        ("1号厅", "DP-3000", "XL-4500", 9000,
         ("LP-1001", 979.0, 1000.0, 7000.0)),   # 临界（21h 剩余），且已偏暗
        ("2号厅", "DP-3000", "XL-4500", 9000,
         ("LP-2001", 1052.0, 1000.0, 6100.0)),  # 超阈值，待放映主管批准
        ("3号厅", "DP-2200", "XL-3000", 8000,
         ("LP-3001", 320.0, 800.0, 10200.0)),   # 正常
        ("4号厅", "DP-2200", "XL-3000", 8000,
         ("LP-4001", 610.0, 800.0, 9800.0)),    # 正常 -> 将模拟异常熄灯
        ("5号厅", "DP-1500", "XL-2100", 7500,
         ("LP-5001", 778.0, 800.0, 7800.0)),    # 临界（22h 剩余），短片可覆盖
    ]
    projector_ids = []
    for name, pmodel, lmodel, lumens_req, (serial, lit, thr, cur_lum) in halls:
        hid = svc.add_hall(name)
        pid = svc.add_projector(hid, pmodel, lmodel, lumens_req)
        svc.install_lamp(pid, serial, lmodel, lit, thr, lumens=cur_lum)
        projector_ids.append(pid)

    # 备件：匹配件 + 一个型号不符件
    svc.add_spare("SP-XL4500-A", "XL-4500", 1000.0)
    svc.add_spare("SP-XL4500-B", "XL-4500", 1000.0)
    svc.add_spare("SP-XL3000-A", "XL-3000", 800.0)
    svc.add_spare("SP-XL2100-A", "XL-2100", 800.0)
    svc.add_spare("SP-WRONG-01", "XL-9000", 1500.0)  # 型号不符，开单时会被拒

    # 场次（开始时间相对当前时刻，保证“下一场”始终有值）
    shows = [
        # hall 下标, 片名, 距现在小时, 分钟, 时长(分)
        (0, "星际远航", 3, 0, 150),    # 1号厅 临界但本场可覆盖
        (0, "深夜末场", 6, 30, 165),
        (1, "城市之光", 2, 30, 120),   # 2号厅 超阈值待批准
        (2, "动画嘉年华", 4, 0, 100),
        (3, "悬疑夜", 5, 0, 130),      # 4号厅 熄灯 -> 延误
        (4, "短片集锦", 3, 15, 90),
    ]
    for hidx, title, ahead, mins, dur in shows:
        svc.schedule_show(hidx + 1, title, _t(ahead, mins), dur)

    # 1号厅灯泡只剩 21h；追加一场 5 小时连映，演示“无法覆盖整场”
    svc.schedule_show(1, "连映马拉松", _t(8), 300)

    # 4号厅模拟异常熄灯 -> 未来场次自动转延误
    svc.report_outage(projector_ids[3], "放映中灯泡异常熄灭，无焦糊味，待检修",
                      handled_by="值班经理")

    print("样例数据已初始化：")
    print("  1号厅 临界寿命（21h，亮度偏低）；2号厅 超阈值待主管批准")
    print("  3号厅 正常；4号厅 异常熄灯（场次已延误）；5号厅 临界（短片可覆盖）")
    print("  备件含型号不符样例 SP-WRONG-01")
    return svc
