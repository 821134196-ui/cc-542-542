import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("CINEMA_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("CINEMA_DB_PATH", os.path.join(DATA_DIR, "cinema.db"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")

# 固定的换灯步骤编号（不得增删、不得跳步）
REPLACE_STEPS = [
    (1, "断电", "关闭放映机电源并挂牌上锁"),
    (2, "冷却", "等待灯泡冷却至可操作温度"),
    (3, "安装", "拆下旧灯泡、装入匹配型号的新灯泡并重置计时器"),
    (4, "对焦", "通电后调整光路焦点"),
    (5, "试映", "试映并比对更换前/后亮度"),
]

# 事故类型
INC_OUTAGE = "abnormal_outage"            # 异常熄灯
INC_RESET_FAILED = "timer_reset_failed"   # 计时器重置失败
INC_SPARE_MISMATCH = "spare_mismatch"     # 备件型号不符
INC_CANCELLED = "replacement_cancelled"   # 换灯中途取消
INC_BRIGHTNESS = "brightness_insufficient"  # 试映亮度不足
INC_SHOW_DELAY = "show_delay"             # 场次延误

INCIDENT_LABELS = {
    INC_OUTAGE: "异常熄灯",
    INC_RESET_FAILED: "计时器重置失败",
    INC_SPARE_MISMATCH: "备件型号不符",
    INC_CANCELLED: "换灯中途取消",
    INC_BRIGHTNESS: "试映亮度不足",
    INC_SHOW_DELAY: "场次延误",
}

CRITICAL_RATIO = 0.95  # 剩余寿命低于 5% 视为临界

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)
