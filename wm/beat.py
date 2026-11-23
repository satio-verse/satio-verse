"""服务端的心跳, 给看守进程 (tools/serve.py) 看: 分清"忙、慢"和"卡死".

看守起服务端时在环境变量 WM_BEAT_FILE 里给一个文件, 服务端从导入代码起每 BEAT_S 秒往里写一次
{boot, pid, phase, t, loop}:
- t: 写的这一刻 (一个小线程写的: 进程还在、还轮得到它跑);
- loop: 事件循环最近一次转到的时刻 (事件循环里每秒记一次; 被同步代码卡住时它跟着停);
- phase: 启动到哪一步了 ("载入面部动画 (A2F)"…), 起好了是 "起好了".
看守的判断 (serve.py 的 Supervisor.judge): /api/status 查不到但 loop 还在走 = 只是慢 (机器被别的程序占满、门禁在跑), 不杀;
loop 停了而 t 还在走 = 事件循环卡死了, 杀; 起得慢但 t 还在走 = 还在载入, 接着等.
(2026-10-27: 门禁跑 pytest、别的会话的测试也在跑, /api/status 要 7~9 秒才回, 看守 5 秒超时连着 3 次就当卡住杀了;
重启时 A2F 载入 67 秒, 整个启动 100~140 秒, 又被 120 秒的启动时限杀, 来回崩了十几次.)
没有看守 (没给 WM_BEAT_FILE) 时什么都不做. 这个模块只用标准库: 导入代码之前就要起来."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

BEAT_S = 2.0
READY = "起好了"  # 启动的最后一步 (wm/app.py 的 _startup 末尾写); 看守据此知道"服务端自己说起好了" (serve.py 的 judge)
_state = {"phase": "导入代码", "loop": 0.0}
_thread: threading.Thread | None = None


def phase(name: str) -> None:
    """启动到哪一步了 (写进下一次心跳)."""
    _state["phase"] = name


def read(path: Path) -> dict | None:
    """看守读心跳; 没有、正在写读到一半的返回 None."""
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _write(path: Path, boot: str) -> None:
    rec = {"boot": boot, "pid": os.getpid(), "phase": _state["phase"], "t": time.time(), "loop": _state["loop"]}
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # 看守正好在读时 Windows 不让换: 这一拍不写, 下一拍再写
    except OSError:
        pass


def start(path: str | None = None) -> bool:
    """起写心跳的小线程 (path 不给就用 WM_BEAT_FILE; 都没有 = 没有看守, 不起). 返回起了没有."""
    global _thread
    path = path or os.environ.get("WM_BEAT_FILE")
    if not path or _thread is not None:
        return False
    target, boot = Path(path), os.environ.get("WM_BOOT_ID", "")

    def run() -> None:
        while True:
            _write(target, boot)
            time.sleep(BEAT_S)

    _thread = threading.Thread(target=run, name="wm-beat", daemon=True)
    _thread.start()
    return True


def watch_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """事件循环每秒记一次时刻 (服务端启动时调一次; 循环关掉时排着的回调跟着丢掉)."""
    loop = loop or asyncio.get_running_loop()

    def tick() -> None:
        _state["loop"] = time.time()
        loop.call_later(1.0, tick)

    tick()
