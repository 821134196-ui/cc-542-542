"""模拟放映机适配器：按序列号返回读数（点亮小时、亮度、在线状态）。

适配器读数代表放映机物理侧的真实值；业务侧用工单/场次后同步读数来累计时长。
状态持久化在本地 JSON，进程重启不丢失；测试可传 path=None 使用内存态。
"""
import json
import os
import threading


class ProjectorAdapter:
    """模拟适配器。可注入：运行小时漂移、亮度、熄灯故障、重置失败。"""

    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()
        # projector_id -> 读数
        self._state = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._state = {int(k): v for k, v in json.load(f).items()}

    # ---- 测试/种子用的控制面 ----
    def register(self, projector_id, serial_no, lit_hours=0.0, lumens=10000.0):
        with self._lock:
            self._state[projector_id] = {
                "serial_no": serial_no,
                "lit_hours": float(lit_hours),
                "lumens": float(lumens),
                "online": True,
                "outage": False,
                "reset_will_fail": False,
            }
            self._flush()

    def add_runtime(self, projector_id, hours, lumens_drop=0.0):
        """模拟放映一场：点亮小时增加，亮度随老化略降。"""
        with self._lock:
            s = self._require(projector_id)
            s["lit_hours"] += float(hours)
            s["lumens"] = max(0.0, s["lumens"] - lumens_drop)
            self._flush()
            return dict(s)

    def set_lumens(self, projector_id, lumens):
        with self._lock:
            self._require(projector_id)["lumens"] = float(lumens)
            self._flush()

    def trigger_outage(self, projector_id, on=True):
        """模拟异常熄灯。"""
        with self._lock:
            s = self._require(projector_id)
            s["outage"] = on
            s["online"] = not on
            self._flush()

    def set_reset_failure(self, projector_id, on=True):
        """让下一次计时器重置失败。"""
        with self._lock:
            self._require(projector_id)["reset_will_fail"] = on
            self._flush()

    def swap_lamp(self, projector_id, new_serial, reset_hours=0.0, lumens=10000.0):
        """物理换灯完成后由 service 调用；若置位了重置失败则抛出。"""
        with self._lock:
            s = self._require(projector_id)
            if s.pop("reset_will_fail", False):
                self._flush()
                raise TimerResetFault(projector_id)
            s["serial_no"] = new_serial
            s["lit_hours"] = float(reset_hours)
            s["lumens"] = float(lumens)
            s["outage"] = False
            s["online"] = True
            self._flush()

    # ---- 放映机只读数据面（生产代码走这里）----
    def get_reading(self, projector_id):
        with self._lock:
            s = self._require(projector_id)
            reading = dict(s)
            reading.pop("reset_will_fail", None)  # 数据面不暴露测试钩子
            return reading

    def _require(self, projector_id):
        if projector_id not in self._state:
            raise KeyError(f"适配器未注册放映机 {projector_id}")
        return self._state[projector_id]

    def _flush(self):
        if not self._path:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)


class TimerResetFault(RuntimeError):
    """放映机计时器重置失败（模拟适配器硬件返回）。"""

    def __init__(self, projector_id):
        super().__init__(f"放映机 {projector_id} 计时器重置失败")
        self.projector_id = projector_id
