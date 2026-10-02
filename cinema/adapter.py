"""模拟放映机适配器：放映机读数（灯泡小时、亮度、电源、计时器重置）由此返回。

真实部署时替换为对接放映机 SNMP/串口协议的适配器，接口保持不变。
亮度模型：新灯基准 14.5 fL，随点亮小时线性衰减；缺陷备件单独注入低亮度。
"""
from __future__ import annotations

NEW_LAMP_BRIGHTNESS_FL = 14.5     # 新灯基准亮度（fL）
DEGRADE_PER_HOUR_FL = 0.004       # 每小时衰减
MIN_BRIGHTNESS_FL = 14.0          # 试映合格线（DCI 2D 参考 14 fL）


class ProjectorAdapterError(Exception):
    pass


class MockProjectorAdapter:
    def __init__(self) -> None:
        # projector_id -> 设备侧状态
        self._state: dict[int, dict] = {}
        # 缺陷备件序列号：装上后亮度怎么调都不达标（试映亮度不足样例）
        self.defective_serials: set[str] = set()

    def attach(self, projector_id: int, lamp_hours: float = 0.0,
               fail_reset_times: int = 0) -> None:
        """登记一台放映机；fail_reset_times 用于注入"计时器重置失败"。"""
        self._state[projector_id] = {
            "power": True,
            "lamp_hours": float(lamp_hours),
            "fail_reset_times": int(fail_reset_times),
            "defective_lamp": False,
        }

    def _st(self, projector_id: int) -> dict:
        if projector_id not in self._state:
            raise ProjectorAdapterError(f"放映机 {projector_id} 未接入适配器")
        return self._state[projector_id]

    # ---- 读数 ----
    def read_lamp_hours(self, projector_id: int) -> float:
        return self._st(projector_id)["lamp_hours"]

    def read_brightness_fl(self, projector_id: int) -> float:
        st = self._st(projector_id)
        if st["defective_lamp"]:
            return 12.6  # 缺陷备件：怎么对焦都达不到 14 fL
        b = NEW_LAMP_BRIGHTNESS_FL - DEGRADE_PER_HOUR_FL * st["lamp_hours"]
        return round(max(b, 6.0), 1)

    def is_powered(self, projector_id: int) -> bool:
        return self._st(projector_id)["power"]

    # ---- 操作 ----
    def power_off(self, projector_id: int) -> None:
        self._st(projector_id)["power"] = False

    def power_on(self, projector_id: int) -> None:
        self._st(projector_id)["power"] = True

    def add_runtime(self, projector_id: int, hours: float) -> float:
        """放映结束后累计点亮时长，返回设备侧最新小时数。"""
        st = self._st(projector_id)
        st["lamp_hours"] = round(st["lamp_hours"] + hours, 2)
        return st["lamp_hours"]

    def reset_lamp_timer(self, projector_id: int) -> bool:
        """换新灯后重置设备计时器；可注入前 N 次失败（计时器重置失败样例）。"""
        st = self._st(projector_id)
        if st["fail_reset_times"] > 0:
            st["fail_reset_times"] -= 1
            return False
        st["lamp_hours"] = 0.0
        return True

    def note_lamp_installed(self, projector_id: int, serial: str) -> None:
        """换灯后告知适配器新灯序列号，缺陷备件会影响后续亮度读数。"""
        self._st(projector_id)["defective_lamp"] = serial in self.defective_serials
