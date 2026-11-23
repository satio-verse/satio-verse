"""她按哪个时区过日子 (2026-10-27 用户: "时间可以在设置里面预设，如果不预设，就根据设备时间或者地理位置时间决定").

起因: 这台电脑的时区是 UTC-7, 主人在北京时间, 差 15 小时: 她下午跟主人说"这么晚还不睡", 睡前回顾、免打扰、小屋里的夜晚都按电脑的钟.

设置在操作面板"桌宠与工作台"页 (GET / POST /api/clock), 存在 <log_dir>/clock.json; 没存过就看 config 的 clock.tz;
都没有 (空) = 跟着电脑. 设了就给整个服务端进程换时区: 设 TZ 环境变量 (UTC 偏移, 不带夏令时) 再让 C 运行库重读
(Windows 上是 ucrtbase 的 _tzset, 别处是 time.tzset), datetime.now / fromtimestamp、日志的时间、按天分的文件都跟着变,
不用重启; 她起的子进程 (dsh 这些) 也继承. 桌宠页面 (小屋的夜晚、一天的安排) 从 /api/clock 拿偏移自己换算 (desktop/web/tz.js).
地理位置没用: 按 IP 猜要把 IP 发给别人, 走代理时还会猜成代理那边; Windows 自己的"自动设置时区"开着时电脑的时区就是按位置定的.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# 面板下拉框里的 (值, 说明). 值写成 "UTC+8" 这样; 不在这里的偏移 (比如 UTC+5:45) 也认, 只是下拉框里没有
ZONES = [("", "跟着电脑"), ("UTC+8", "北京、上海、香港、台北、新加坡"), ("UTC+9", "东京、首尔"), ("UTC+7", "曼谷、河内、雅加达"),
         ("UTC+5:30", "新德里"), ("UTC+4", "迪拜"), ("UTC+3", "莫斯科"), ("UTC+2", "开罗、东欧 (冬令时)"),
         ("UTC+1", "柏林、巴黎 (冬令时)"), ("UTC+0", "伦敦 (冬令时)"), ("UTC-3", "圣保罗"), ("UTC-5", "纽约 (冬令时)"),
         ("UTC-6", "芝加哥 (冬令时)"), ("UTC-7", "丹佛、凤凰城"), ("UTC-8", "洛杉矶 (冬令时)"), ("UTC+10", "悉尼 (冬令时)"),
         ("UTC+12", "奥克兰 (冬令时)")]
_TZ = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)
_OURS = "_WM_CLOCK_TZ"  # 记一下 TZ 是不是我们设的 (换回"跟着电脑"时只拿掉我们设的)


def parse(tz: str) -> int | None:
    """"UTC+8" / "+08:00" / "UTC-5:30" -> 东边多少分钟 (480); 空串 -> None (跟着电脑). 不认识的抛 ValueError."""
    tz = str(tz or "").strip()
    if not tz:
        return None
    m = _TZ.match(tz)
    if not m or int(m.group(2)) > 14 or int(m.group(3) or 0) >= 60:
        raise ValueError(f"认不出时区「{tz[:20]}」: 写成 UTC+8、UTC-5:30 这样")
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return minutes if m.group(1) == "+" else -minutes


def name(offset: int) -> str:
    """480 -> "UTC+8"; 330 -> "UTC+5:30"."""
    sign, m = ("+" if offset >= 0 else "-"), abs(offset)
    return f"UTC{sign}{m // 60}" + (f":{m % 60:02d}" if m % 60 else "")


def _reload() -> None:
    if hasattr(time, "tzset"):
        time.tzset()
    elif sys.platform == "win32":
        ctypes.cdll.ucrtbase._tzset()


def apply(tz: str) -> None:
    """给这个进程换时区 (马上生效). 空串: 换回电脑的 (只拿掉我们自己设的 TZ)."""
    offset = parse(tz)
    if offset is None:
        if os.environ.pop(_OURS, None) is not None:
            os.environ.pop("TZ", None)
            _reload()
        return
    m = abs(offset)
    # C 运行库的写法: 名字 + 往西为正的偏移 (东八区是 WMT-08:00), 不写夏令时的名字就没有夏令时
    os.environ["TZ"] = f"WMT{'-' if offset >= 0 else '+'}{m // 60:02d}:{m % 60:02d}"
    os.environ[_OURS] = name(offset)
    _reload()


def device() -> dict:
    """电脑自己的时区 (不管 TZ 设没设): Windows 上问系统, 别处按进程起来时的."""
    if sys.platform == "win32":
        class _ST(ctypes.Structure):
            _fields_ = [(f, ctypes.c_ushort) for f in ("y", "mo", "dow", "d", "h", "mi", "s", "ms")]

        class _TZI(ctypes.Structure):
            _fields_ = [("bias", ctypes.c_long), ("std_name", ctypes.c_wchar * 32), ("std_date", _ST),
                        ("std_bias", ctypes.c_long), ("dst_name", ctypes.c_wchar * 32), ("dst_date", _ST),
                        ("dst_bias", ctypes.c_long)]
        tzi = _TZI()
        kind = ctypes.windll.kernel32.GetTimeZoneInformation(ctypes.byref(tzi))
        bias = tzi.bias + (tzi.dst_bias if kind == 2 else tzi.std_bias if kind == 1 else 0)
        return {"offset_min": -bias, "zone": tzi.dst_name if kind == 2 else tzi.std_name}
    return {"offset_min": -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone) // 60,
            "zone": time.tzname[0]}


def state(tz: str) -> dict:
    """面板和桌宠页面看的: 设的什么、现在按哪个偏移过、电脑自己是哪个时区、她现在几点、下拉框的选项."""
    dev = device()
    offset = parse(tz)
    now = datetime.now()
    return {"tz": name(offset) if offset is not None else "", "offset_min": dev["offset_min"] if offset is None else offset,
            "device": {**dev, "name": name(dev["offset_min"])}, "now": now.strftime("%Y-%m-%d %H:%M"),
            "zones": [{"value": v, "label": label} for v, label in ZONES]}


class Clock:
    """存在 <log_dir>/clock.json 的设置 (面板改的); 没存过用 config 的 clock.tz."""

    def __init__(self, path: Path, default: str = ""):
        self.path, self.tz = path, str(default or "")
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("tz"), str):
                self.tz = saved["tz"]
        except (OSError, ValueError):
            pass
        try:
            parse(self.tz)
        except ValueError:
            self.tz = ""  # 文件被改坏了: 先跟着电脑

    def start(self) -> None:
        apply(self.tz)

    def set(self, tz: str) -> dict:
        """面板改了: 认得出才存, 存了马上换. 返回 state."""
        offset = parse(tz)
        self.tz = name(offset) if offset is not None else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tz": self.tz}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        apply(self.tz)
        return state(self.tz)

    def state(self) -> dict:
        return state(self.tz)
