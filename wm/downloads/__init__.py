"""下载队列 (2026-11-11 用户定的第一个"服务型插件"; 知识库《六个工具类插件对照与取舍》第 1 节、《底层终端（插件宿主）方案》
第六节第 2 步). 参考 DSH 的 DLS 插件 (MIT) 的做法 —— 常驻队列、断点存盘、所有会话看得到、只有发起的能停 —— 没有拿它的代码.

为什么要有: 以前她要下文件只能跑命令, 回合一结束就断; "下载先问你"靠每个对话自己记得. 这里把它做成机制:
- 入队只是登记, 马上返回编号 (D1、D2 …); 你在卡片上点头之前一个字节都不传, 完全授权档也一样 (闸在队列里, 不在工具权限里);
- 卡片写名称、来源、大小、许可 (她查不到的写"未知"); 卡片上没列的来源不连 —— 不自动换镜像, 跳转到没列的域名先停下再问你;
  实际大小比卡片上写的大, 也先停下再问;
- 只下公网的 http / https (每一跳都查), 只写进下载目录, 下完不运行、不解压; 有磁盘余量和单个文件大小的上限;
- HTTP Range 断点续传 (.part + 检查点), 有 SHA-256 的必须对上 (对不上删掉、标失败); 同一个位置同一个来源不下两次;
- 所有会话看得到队列, 只有发起的会话 (和你) 能暂停、继续、取消; 下完回到发起的会话报一声.
不做多线程分段 (那是在绕源站的限速), 不带 aria2 (GPL).

下到哪 (2026-11-16 用户定的): 桌宠文件夹 (装好的版本是安装文件夹, 开发树是项目根目录) 下面的 download-queue 子文件夹, 见 default_dir();
写不进去就明确报出来 (dir-unwritable), 不换地方、不要管理员权限; 旧位置 downloads/queue 里的东西不删不搬, 只报告 (legacy_report).

怎么跑的 (独立进程, 不在 8790 里):
  宿主 (8790, wm/agent/downloads.py) --本机 HTTP + 令牌--> 下载服务 (python -m wm.downloads serve)
                                                            ^ 小看守 (python -m wm.downloads watch, 用 wm/services.py 的状态机)
- 下载服务有自己的数据目录 (<家>/serve/downloads, 在守门的保护名单里: 她的读写工具碰不到队列记录和令牌)、一个只绑本机的端口、
  一枚每次启动新生成的令牌; 她和页面都经宿主转给它, 宿主才分得清是哪个会话、是不是你点的头.
- 按需启动: 有活 (排着的、在下的、没回报的) 或者宿主刚叫过它才开着; 闲 2 分钟自己退出, 小看守跟着退. 挂了小看守按
  wm/services.py 的规矩拉起来 (10 分钟挂 5 次就不再拉). 重启 8790 不影响正在下的.
- 以后挂到"服务型插件"下面时: 清单里的 service.cmd 就是 serve 这条命令, health 是 /health, start 是 on-demand; 看守换成总看守,
  这里的 watch 就不用了.

文件: rules.py (地址、路径的规矩) · text.py (卡片和回报的字) · transfer.py (传一个文件) · queue.py (队列、状态机、存盘) ·
daemon.py (本机接口、小看守) · client.py (宿主这边: 找到、叫醒、转发).
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import ROOT

SERVICE = "wm-downloads"
PORT = 8789                 # 默认端口 (downloads.port); 被占了小看守会另挑一个, 宿主照 daemon.json 找
IDLE_EXIT_S = 120.0         # 没活、没人叫这么久, 下载服务自己退出
WAKE_S = 90.0               # 宿主叫过它以后这么久之内算"有人要用"
TERMINAL = ("done", "failed", "cancelled", "denied")
STATES = ("awaiting", "queued", "running", "paused") + TERMINAL
STATE_TEXT = {"awaiting": "等主人点头", "queued": "排队中", "running": "在下", "paused": "暂停了", "done": "下完了",
              "failed": "没成", "cancelled": "取消了", "denied": "主人没同意"}


class DownloadError(Exception):
    """队列不给做的事. code 是给程序认的 (not-found / not-owner / bad-state / disk-full / url-invalid / private-address /
    dest-invalid / dest-in-use / dest-exists / too-large / too-many / bad-args / service-down / dir-unwritable), 话是给人和她看的."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Limits:
    max_file_bytes: int = 16 << 30        # 单个文件最多多大 (downloads.max_file_gb)
    reserve_bytes: int = 2 << 30          # 下完以后盘上至少还剩多少 (downloads.reserve_gb)
    unknown_ok_bytes: int = 256 << 20     # 卡片上大小写"未知"的, 实际超过这么多再问一次 (downloads.unknown_ok_mb)
    max_active: int = 2                   # 同时下几个 (每个一条连接)
    max_waiting: int = 8                  # 一个会话最多几张等点头的卡片 (免得刷屏)
    max_open: int = 200                   # 没结束的任务最多几个
    keep_records: int = 200               # 结束了的记录最多留几条 (再多删最老的, 文件不动)
    retry_delays: tuple = (2.0, 5.0, 15.0, 30.0, 60.0)  # 一个地址连着失败, 每次隔多久再试; 试完换下一个地址
    read_timeout_s: float = 60.0          # 这么久一个字节都没来算断了 (接着续传)
    checkpoint_s: float = 1.0             # 多久落一次检查点 (刷盘 + 记下已经稳妥的字节数): 被杀、断电最多重下这一截
    mark_of_web: bool = True              # 下好的文件带上"来自网络"的标记 (和浏览器下的一样; 双击运行时 Windows 会提醒)

    @classmethod
    def from_config(cls, conf: dict | None) -> "Limits":
        conf = conf or {}
        out = cls()
        for key, attr, unit in (("max_file_gb", "max_file_bytes", 1 << 30), ("reserve_gb", "reserve_bytes", 1 << 30),
                                ("unknown_ok_mb", "unknown_ok_bytes", 1 << 20)):
            if isinstance(conf.get(key), (int, float)) and not isinstance(conf.get(key), bool) and conf[key] >= 0:
                setattr(out, attr, int(conf[key] * unit))
        for key in ("max_active", "max_waiting", "max_open", "keep_records"):
            if type(conf.get(key)) is int and conf[key] > 0:
                setattr(out, key, conf[key])
        if isinstance(conf.get("mark_of_web"), bool):
            out.mark_of_web = conf["mark_of_web"]
        return out


def home_for(agent_home: Path | str) -> Path:
    """下载服务的数据目录: 工作台的家旁边的 serve/downloads (真的那份是 ~/.worldmodel/serve/downloads —— serve 整个目录在
    守门的固定名单和默认黑名单里, 她的读写工具、带这个路径的命令都碰不到; 测试、试跑换了工作台的家, 这里跟着换)."""
    return Path(agent_home).parent / "serve" / "downloads"


def real_home() -> Path:
    return Path.home() / ".worldmodel" / "serve" / "downloads"


FOLDER = "download-queue"                       # 下载队列在桌宠文件夹下面的子文件夹 (2026-11-16 用户定的)
LEGACY_REL = Path("downloads") / "queue"        # 2026-11-11 到 11-16 的默认位置. 新位置不能放进 downloads/ 里:
#                                                 安装包卸载时整个删 {app}\downloads (可选组件没下完的部分), 下好的文件会跟着没
PROBE = ".wm-write-probe-"                      # 试写用的小文件的名字开头 (万一残留, 卡片、状态里都不列它)


def default_dir(root: Path | str | None = None) -> Path:
    """默认的下载文件夹: <桌宠文件夹>/download-queue. 桌宠文件夹就是 ROOT —— 装好的版本里是安装文件夹 (默认或安装时指定的),
    开发树里是项目根目录, 整个文件夹拷到别处直接跑就是拷到的位置; 只看代码文件在哪, 不查注册表、不读 %APPDATA%."""
    return Path(root or ROOT) / FOLDER


def dir_for(conf: dict | None, root: Path | str | None = None) -> Path:
    """下载到哪: downloads.dir 配了就用 (绝对路径照用, 相对路径从桌宠文件夹算); 没配 (空、只有空格、不是字符串) 是 default_dir()."""
    base = Path(root or ROOT)
    value = (conf or {}).get("dir")
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return default_dir(base)
    p = Path(raw)
    return p if p.is_absolute() else base / p


def _same_or_inside(path: Path, parent: Path) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(parent))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def check_writable(root: Path | str) -> str:
    """下载文件夹写得进去吗 (真试: 建文件夹、建一个小文件、写一个字节、删掉). 能写返回 ""; 不能返回一句原因 (操作系统自己的话).
    不换别的地方、不提权 —— 只告诉人. (只读文件夹上 mkdir(exist_ok) 和 disk_usage 都不报错, 光看这两样要到传输时才发现.)"""
    p = Path(root)
    if p.drive and not p.is_absolute():
        return f"「{p}」带了盘符却不是完整路径"
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f"{PROBE}{os.getpid()}-{os.urandom(3).hex()}"
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"0")
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(probe)
    except OSError as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return ""


def unwritable_text(root: Path | str, reason: str) -> str:
    return (f"下载文件夹 {root} 写不进去（{reason}）。没有换到别的地方，也不会去要管理员权限；请主人把 config/local.json 的 "
            f"downloads.dir 改到能写的文件夹，或者给这个文件夹写权限，然后重新排。别改用命令行自己去下。")


def legacy_report(root: Path | str, base: Path | str | None = None, cap: int = 2000) -> dict:
    """旧位置 (<桌宠文件夹>/downloads/queue) 里还有没有东西: 只数 (最多看 cap 个条目), 不读内容、不动. 没有、是空的、就是现在的
    下载文件夹、或者在现在的下载文件夹里面: 返回 {}. 有: {dir, files, bytes, more (数到上限停了)}."""
    old = Path(base or ROOT) / LEGACY_REL
    if not old.is_dir() or _same_or_inside(old, Path(root)) or _same_or_inside(Path(root), old):
        return {}
    files = size = seen = 0
    more = False
    stack = [old]
    while stack and not more:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    seen += 1
                    if seen > cap:
                        more = True
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            files += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return {"dir": str(old), "files": files, "bytes": size, "more": more} if files else {}


def legacy_text(legacy: dict) -> str:
    if not legacy:
        return ""
    more = "多" if legacy.get("more") else ""
    return (f"旧位置 {legacy['dir']} 里还有 {legacy['files']}{more} 个文件（{human_size(legacy['bytes'])}），没有动也没有搬；"
            f"下载位置已经改到别处，要搬请主人点头。")


def read_json(path: Path, tries: int = 4, wait: float = 0.05) -> dict:
    """读一个 JSON 对象. 文件不在返回 {}. 文件在却读不出来 (Windows 上写的那边正在"换"它时, 这边打开会报 PermissionError;
    或者读到空的 / 不是对象) 隔 wait 秒再读, 最多 tries 次: 写的人用的是"写临时文件再换", 读到的要么整份旧的、要么整份新的,
    读不出来只能是撞上了换的那一瞬 (2026-11-11 评审时在这台机器上量过: 一边反复换、一边反复读, 读的 171/33,975 次出错或读空).
    读不出来就当没有, 不能当成"看守不在了"这类结论的依据 —— 所以多读几次."""
    path = Path(path)
    for attempt in range(max(1, tries)):
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            got = None
        if isinstance(got, dict):
            return got
        if attempt + 1 < tries:
            time.sleep(wait)
    return {}


def write_json(path: Path, obj, tries: int = 6) -> None:
    """先写旁边的临时文件再换过去: 读的人看不到写了一半的. Windows 上目标文件正被别的进程开着读时, 换不了 (PermissionError),
    稍等再换, 最多 tries 次; 还不行就抛出来 (调用的人自己决定怎么办: 任务记录记一笔存储出错、看守记一笔接着跑)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(max(1, tries)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= tries:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.02 * (attempt + 1))


def summary(home: Path) -> dict:
    """下载服务每次状态变了都写一份小结 (<家>/summary.json): active 在排、在下的, awaiting 等点头的, unreported 结束了还没
    回报的. 服务没开着的时候宿主和看守也读得到 (用来决定要不要把它叫起来)."""
    got = read_json(Path(home) / "summary.json")
    return {k: int(got.get(k) or 0) for k in ("active", "awaiting", "unreported")}


def wake(home: Path) -> None:
    """宿主要用下载服务了: 留个记号, 看守看到就把它开起来 (WAKE_S 之内都算数)."""
    path = Path(home) / "wake"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="ascii")


def wanted(home: Path, now: float | None = None) -> bool:
    """该不该开着: 有在排、在下的, 或者宿主刚叫过. 等点头的不算 (你点了宿主会叫它); 结束了还没回报的也不算 —— 记录都在文件里,
    宿主回来 (它看着 summary 里的 unreported) 会叫醒下载服务再取, 不用服务一直挂着等宿主 (宿主要是一直不回来就白挂了)."""
    home = Path(home)
    s = summary(home)
    if s["active"]:
        return True
    try:
        return (now or time.time()) - (home / "wake").stat().st_mtime < WAKE_S
    except OSError:
        return False


def human_size(n) -> str:
    if n is None or n < 0:
        return "未知"
    n = float(n)
    for unit in ("字节", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} 字节" if unit == "字节" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} GB"


def human_time(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
