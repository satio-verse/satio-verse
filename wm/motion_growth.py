"""动作成长 (docs/roadmap/motion-growth.md) 的服务端: 她日常里记下的动作失败片段、她换上过的每一版动作参数、换上与退回、
什么时候练 (用户允许、电脑空闲时才跑 tools/motion_growth.py).

数据在用户数据区 (卸载不删, 安装目录的 models/motion 升级时会被覆盖): 默认 ~/.worldmodel/motion-growth/ (配置 motion.growth.data_dir
可以改; 没设又改了 trace.log_dir 的测试、自检跟着 <log_dir>/motion-growth/, 不写进用户真的数据), 和认知的经历 (wm/cognition/store.py)
一个规矩:
  faults.jsonl         失败片段 (小屋 desktop/web/room-growth.js 发来的, 服务端补上收到的时间和代码版本)
  exposure.jsonl       每类本事做了几次 (按参数版本分, 算失败率)
  params/v0001.json …  她换上过的每一版: 调了哪几个参数、调成多少、为什么、数字、验收清单 (acceptance)、验证时的运行栈指纹、
                       适用形象 (scope); 旧的都留着
  active.json          每一槽现在用哪一版: 基础槽位 (所有形象共用) + 每个形象一槽 (键是 VRM 内容摘要, 文件名只用来显示).
                       没有这个文件 = 出厂参数 models/motion/params.json
  candidates/k0001.json …  验证过、还没换上的候选 (适用范围的验收、用户同意以后才换上)
  changes.jsonl        每次换上、退回、停用: 换了什么、为什么、数字, 她要说的那一句
  state.json           开关 (auto: 空闲时自己练, 默认关)、说过的话、在跑的那一次
  runs/<时间>/         每次练的记录;  reports/<问题>.md  调参救不了的问题报告 (给开发, 带复现命令)

出厂参数变了 (开发重调过、升级装了新版): 她那一版是在旧的出厂参数上调的, 先停用 (那一槽退回), 等下次空闲重新验证.
代码变了 (行动层改过): 照旧用着, 记一笔"要重新验证", 下次空闲先验证, 不过就退回. 重新验证用"原验收清单 ∪ 现在的强制检查",
验收只能变多不能悄悄变少; 没存验收清单的旧版本标成"待补验" (tools/motion_growth.py).
重置 (reset): 默认只退回出厂参数 (停掉正在练的, 历史留着); history=True 连历史一起清, 代次 (gen) 加一, 小屋重置之前攒着没发的那些
之后也不收. 认知那边的总入口 (/api/experience reset, scope 里有 growth) 在服务端进程里直接调 reset().

接口 (只认本机页面, 查 Host 和 Origin, 同 /ws):
  POST /api/motion/faults   {records, exposure, session, params}  小屋发来的
  GET  /api/motion/growth   状态 (设置面板、自检看)
  POST /api/motion/growth   {tick, busy, avatar, params} 小屋每 5 分钟问一次 (要不要开始练、有没有要说的、这个形象该用哪一版参数;
                            busy: 她在对话) / {auto: true|false} 设置里的开关 / {stop: true} 停下正在练的 /
                            {rollback: "factory" | "base" | "v0002" | true (上一版), avatar 或 slot} 一键退回那一槽 /
                            {reset: true, confirm: "reset", history?: true} 重置 (默认只退回出厂参数, history 连历史一起清) /
                            {ack: 编号} 那句话说过了
  /motiondb/params.json     有换上的版本时: 出厂参数叠上她那一版 (app.py 的 motiondb_file 调 params_payload)
命令行: python -m wm.motion_growth where | status [--avatar x.vrm] | rollback [版本] [--avatar x.vrm] | watch | reset --yes [--history]
       (都可以带 --config / --set)
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DIR = "~/.worldmodel/motion-growth"
FAMILIES = ("carry", "sit", "walk", "stand", "arms")
GATE_VERSION = 3  # 3: 实际轨迹/片段覆盖, 不能以多个重复 seed 代替覆盖证据
_KIND = re.compile(r"^[a-z][a-z.]{0,31}$")
MAX_RECORDS = 100          # 一次最多收这么多条
MAX_PER_DAY = 600          # 一天最多存这么多条失败片段
ROTATE_BYTES = 20 << 20    # faults.jsonl 超过 20 MB 换一个文件
# 代码版本: 这些文件的内容 (动作、接触、平滑、痛觉、检测、参数表、抓持、小屋姿势层). 变了就要重新验证她那一版
CODE_FILES = ("web/locomotion.js", "web/contact.js", "web/pose-filter.js", "web/mm.js", "web/pain.js", "web/kinematics.js",
              "web/motion-params.js", "web/motion-faults.js", "web/physics/grip.js", "web/physics/rigid-scene.js",
              "desktop/web/room-poses.js", "desktop/web/room-sip-exit.js", "web/cup-pick.js", "web/cup-handoff.js", "web/lmm.js", "web/plain-state.js", "web/hand.js", "web/avatar-vrm.js",
              "web/physics/liquid.js", "web/physics/rigid.js", "web/physics/body-hit.js", "web/nav.js", "web/reach-safety.js", "web/grasp-transform.js", "web/grasp-controller.js", "web/scene.js", "web/scene-colliders.js", "web/rom.js",
              "desktop/web/room-life.js", "desktop/web/room-props.js", "desktop/web/room-grasps.js", "desktop/web/room-acts.js", "desktop/web/room-afford.js",
              "desktop/web/room-body.js", "desktop/web/room-needs.js", "web/body-collision-parts.js", "desktop/web/room-liquid.js", "desktop/web/room-rigid.js",
              "desktop/web/room-scene-config.js", "desktop/web/room-scene-state.js",
              "desktop/web/room-action-requests.js", "desktop/web/room-device-actions.js", "desktop/web/room-consumption.js", "desktop/web/room-coffee-policy.js",
              "web/kinematics-core.js", "web/posture.js", "web/arbiter.js", "web/spring-center.js", "web/app.js")
# 换上以后盯着: 这一类本事做了至少这么多次才判断; 失败率比换上前高到 RATIO 倍、单边检验 p < ALPHA 就自动退回
WATCH_MIN = 20
WATCH_RATIO = 1.3
WATCH_ALPHA = 0.05


def _root() -> Path:
    from .config import ROOT
    return ROOT


def data_dir(cfg: dict) -> Path:
    """规矩和她的经历一样, 用认知那边的公用函数 (只有一份): 设了 motion.growth.data_dir 就用它; 测试、自检改了 trace.log_dir 的
    跟着 <log_dir>/motion-growth; 不然 ~/.worldmodel/motion-growth."""
    from .cognition.store import user_dir
    g = ((cfg.get("motion") or {}).get("growth") or {})
    return user_dir(cfg, Path(DEFAULT_DIR).name, str(g.get("data_dir") or ""))


def session_busy(sessions, channels=None) -> str | None:
    """她在忙 (不练): 有一轮对话没说完 (操控电脑也在一轮里)、在听你说话; QQ 那边转来的电脑任务单在做 (sent / running).
    sessions: 连着的本人会话 (app.py 的 SESSIONS), channels: 渠道 (ChannelManager, 有 tasks)."""
    for s in list(sessions or []):
        cur = getattr(s, "current", None)
        if cur is not None and not getattr(cur, "finalized", True):
            return "她在和你说话 (这一轮还没说完)"
        if getattr(s, "listening", False):
            return "她在听你说话"
    tasks = getattr(channels, "tasks", None) or {}
    if any(isinstance(t, dict) and t.get("state") in ("sent", "running") for t in tasks.values()):
        return "她在做电脑任务单"
    return None


def factory_path(cfg: dict) -> Path:
    from .config import resolve_path
    return resolve_path((cfg.get("motion") or {}).get("locomotion_dir", "models/motion")) / "params.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def code_version(root: Path | None = None) -> str:
    """动作执行依赖的内容摘要; 缓存含目录、路径、大小、纳秒修改时间."""
    return files_digest(root or _root(), CODE_FILES)


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _append(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _lines(p: Path):
    if not p.exists():
        return
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# -- 空闲判断 ----------------------------------------------------------------------------------
def idle_gate(min_idle_s: float = 600, cpu_max: float | None = 30.0) -> dict:
    """电脑空不空闲 (Windows): 键鼠多久没动、有没有全屏 / 演示 / 游戏 (D3D 全屏)、麦克风和摄像头有没有在用 (在通话)、
    插没插电、CPU 忙不忙. 返回 {ok, why: [没通过的原因], ...读数}. 别的系统一律不通过."""
    out: dict = {"ok": False, "why": []}
    if sys.platform != "win32":
        out["why"].append("不是 Windows")
        return out
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        out["idle_s"] = round(((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000, 1)
        if out["idle_s"] < min_idle_s:
            out["why"].append(f"键鼠 {out['idle_s']:.0f} 秒前还在动 (要空闲 {min_idle_s:.0f} 秒)")
    # 1 没人 / 5 能弹通知 / 6 刚登录的安静时间 / 7 Windows 应用: 算空闲; 2 忙 (全屏) / 3 D3D 全屏 (游戏) / 4 演示: 不算
    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        out["notify"] = state.value
        if state.value in (2, 3, 4):
            out["why"].append({2: "有全屏程序", 3: "在玩游戏 (全屏)", 4: "在演示"}[state.value])

    class POWER(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    pw = POWER()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pw)):
        out["ac"] = pw.ACLineStatus != 0
        if pw.ACLineStatus == 0:
            out["why"].append("没插电")
    busy = capture_in_use()
    out["capture"] = busy
    if busy:
        out["why"].append("麦克风或摄像头在用 (在通话?): " + ", ".join(busy[:3]))
    if cpu_max is not None:
        out["cpu"] = cpu_percent()
        if out["cpu"] is not None and out["cpu"] > cpu_max:
            out["why"].append(f"CPU 占用 {out['cpu']:.0f}% (要低于 {cpu_max:.0f}%)")
    out["ok"] = not out["why"]
    return out


def capture_in_use(exclude=("wmpet",)) -> list[str]:
    """正在用麦克风、摄像头的程序 (隐私设置里 LastUsedTimeStop = 0 的); 桌宠自己 (她在听你说话) 不算."""
    import winreg
    out = []
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
    for cap in ("microphone", "webcam"):
        for sub in ("", "\\NonPackaged"):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{cap}{sub}")
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    if name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(key, name) as k:
                            start, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
                            stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
                    except OSError:
                        continue
                    if start and not stop and not any(x in name.lower() for x in exclude):
                        out.append(f"{cap}:{name.split('#')[-1]}")
    return out


def cpu_percent(sample_s: float = 1.0) -> float | None:
    """整机 CPU 占用 (GetSystemTimes 两次取样)."""
    from ctypes import wintypes

    def times():
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None
        v = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime  # noqa: E731
        return v(idle), v(kern), v(user)

    a = times()
    time.sleep(sample_s)
    b = times()
    if not a or not b:
        return None
    idle, total = b[0] - a[0], (b[1] - a[1]) + (b[2] - a[2])  # 内核时间里含空闲
    return round(100 * (1 - idle / total), 1) if total > 0 else None


# -- 统计 ------------------------------------------------------------------------------------
def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ B(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = min(max(p, 1e-12), 1 - 1e-12)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p) + (n - i) * math.log1p(-p)
             for i in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def worse(n_post: int, e_post: float, n_pre: int, e_pre: float) -> dict:
    """换上以后失败率是不是变高了: 条件泊松检验 (两段的失败次数之和固定, 落在换上以后的比例按曝光分).
    返回 {rate_pre, rate_post, ratio, p, worse}."""
    rp = n_pre / e_pre if e_pre else None
    rq = n_post / e_post if e_post else None
    out = {"rate_pre": rp, "rate_post": rq, "ratio": None, "p": None, "worse": False}
    if not e_pre or not e_post or e_post < WATCH_MIN or e_pre < WATCH_MIN:
        return out
    out["ratio"] = (rq + 0.5 / e_post) / (rp + 0.5 / e_pre)  # 各加半次, 0 次时也有个比值
    out["p"] = binom_sf(n_post, n_post + n_pre, e_post / (e_post + e_pre))
    out["worse"] = out["ratio"] > WATCH_RATIO and out["p"] < WATCH_ALPHA
    return out


# 运行栈指纹: 评估器、场景、打分 (变了以后旧的验证结果不能直接复用)
EVAL_FILES = ("web/motion-eval.js", "web/scene-tasks.js", "web/scenes/room.json", "web/motion-score.js", "web/motion-growth-probe.js",
              "web/motion-growth-coverage.js", "tools/motion_eval.mjs", "tools/tune_motion.py", "tools/motion_growth.py", "wm/motion_growth.py")
_digest_cache: dict = {}
_file_digest_cache: dict = {}


def files_digest(root: Path, files) -> str:
    """一组文件内容的摘要. 大动作库逐文件流式读且缓存; 增删/换目录也会失效."""
    root = root.resolve()
    files = tuple(sorted(files))
    stamp = []
    for f in files:
        p = root / f
        st = p.stat() if p.is_file() else None
        stamp.append((f, st.st_mtime_ns if st else None, st.st_size if st else None))
    stamp = tuple(stamp)
    key = (str(root), files)
    if _digest_cache.get(key, (None,))[0] != stamp:
        h = hashlib.sha256()
        for f, modified, size in stamp:
            p = root / f
            fk = (str(root), f, modified, size)
            if fk not in _file_digest_cache:
                fh = hashlib.sha256()
                if modified is not None:
                    with p.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1 << 20), b""):
                            fh.update(chunk)
                _file_digest_cache[fk] = fh.hexdigest() if modified is not None else "missing"
            h.update(f.encode() + b"\0" + _file_digest_cache[fk].encode() + b"\0")
        _digest_cache[key] = (stamp, h.hexdigest()[:12])
    return _digest_cache[key][1]


def motion_assets(cfg: dict) -> dict:
    """/motiondb 真正使用的目录级 json/bin 清单. 不递归训练目录/日志, params.json 由 factory 域单独管."""
    root = factory_path(cfg).parent.resolve()
    files = sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".json", ".bin") and p.name != "params.json") if root.is_dir() else []
    return {"root": str(root), "files": files, "sha": files_digest(root, files)}


def avatar_dir(cfg: dict) -> Path:
    from .config import resolve_path
    return resolve_path((cfg.get("avatar") or {}).get("dir", "models/vrm"))


_avatar_cache: dict = {}


def avatar_sha(cfg: dict, name: str | None) -> str | None:
    """形象的身份 = VRM 文件内容的摘要 (文件名只用来显示; 同名换了内容就是另一个身体). 没有这个文件返回 None."""
    if not name or not re.fullmatch(r"[\w.\-]+\.vrm", name, re.ASCII):
        return None
    p = avatar_dir(cfg) / name
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key not in _avatar_cache:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _avatar_cache[key] = "a" + h.hexdigest()[:11]
    return _avatar_cache[key]


def default_avatar(cfg: dict) -> str | None:
    """服务端的默认形象 (和 /api/avatars 一样: 配置的那个, 没有就目录里第一个)."""
    d = avatar_dir(cfg)
    names = sorted(p.name for p in d.glob("*.vrm")) if d.is_dir() else []
    want = (cfg.get("avatar") or {}).get("default")
    return want if want in names else (names[0] if names else None)


# -- 主体 ------------------------------------------------------------------------------------
GLOBAL = "global"   # 共享的基础槽位: 所有形象都用 (没有自己那一版的形象用它)


class Growth:
    """参数按槽位生效: 基础槽位 (global) + 每个形象一个槽位 (键是 VRM 内容摘要). 一个形象有自己那一版就用它, 没有就用基础那一版,
    基础也没有就是出厂参数. 每一版带着它的验收清单 (acceptance: 失败场景、守门场景、验证种子、门槛、适用形象) 和验证时的运行栈指纹.
    换了模型内容 (摘要变了) 自然就找不到自己那一槽, 回到基础参数."""

    def __init__(self, cfg: dict, root: Path | None = None):
        self.cfg = cfg
        self.dir = data_dir(cfg)
        self.root = root or _root()
        self.factory = factory_path(cfg)
        self.runner = None  # 这个服务端起的那一次 (subprocess.Popen)
        self.busy = lambda: None  # 她在不在忙 (对话、电脑任务单; register 时换成看会话的), 忙就不练、在练的停

    # 文件
    @property
    def state(self) -> dict:
        return _read_json(self.dir / "state.json", {}) or {}

    def _save_state(self, **kw) -> dict:
        st = self.state
        st.update(kw)
        _write_json(self.dir / "state.json", st)
        return st

    @property
    def gen(self) -> int:
        """删除代次: 每重置一次加一. 小屋发来的旧代次的数据不收 (重置之前攒着没发的那些)."""
        return int(self.state.get("gen") or 0)

    def factory_doc(self) -> dict:
        return _read_json(self.factory, {}) or {}

    def factory_sha(self) -> str:
        try:
            return _sha(self.factory.read_bytes())
        except OSError:
            return "none"

    def fingerprint(self, avatar: str | None = None) -> dict:
        """现在的运行栈: 动作代码、评估器和场景、出厂参数、形象 (给了的话)."""
        out = {"code": code_version(self.root), "eval": files_digest(self.root, EVAL_FILES), "factory": self.factory_sha(),
               "assets": motion_assets(self.cfg)}
        if avatar:
            out["avatar"] = {"file": avatar, "sha": avatar_sha(self.cfg, avatar)}
        return out

    def baseline(self) -> dict:
        """验收实际依赖的基础槽位. 仅换上/退回参数不改变代码指纹, 也必须重核候选."""
        return {"version": self.active_version(GLOBAL),
                "values": {**(self.full_values("factory") or {}), **(self.full_values("base") or {})}}

    def version_fingerprint(self, doc: dict) -> dict:
        """在用版本的完整适用范围指纹; 旧版没有范围时仍核对公用执行栈."""
        avatars = [a["file"] for a in (doc.get("scope") or {}).get("avatars") or [] if a.get("file")]
        avatars = avatars or (doc.get("acceptance") or {}).get("avatars") or []
        return {a: self.fingerprint(a) for a in avatars} if avatars else {"*": self.fingerprint()}

    def stack_changed(self, doc: dict) -> bool:
        return (doc.get("validated_fingerprint") or doc.get("fingerprint")) != self.version_fingerprint(doc)

    def versions(self) -> list[str]:
        d = self.dir / "params"
        return sorted(p.stem for p in d.glob("v[0-9][0-9][0-9][0-9].json")) if d.is_dir() else []

    def version(self, v: str) -> dict | None:
        return _read_json(self.dir / "params" / f"{v}.json") if re.fullmatch(r"v\d{4}", v or "") else None

    # 槽位
    def slots(self) -> dict:
        """{槽位: {version, since, prev, family, file}}. 旧格式的 active.json ({version, ...}) 当成基础槽位."""
        a = _read_json(self.dir / "active.json") or {}
        if "version" in a and "slots" not in a:
            a = {"slots": {GLOBAL: a}}
        return {k: v for k, v in (a.get("slots") or {}).items() if isinstance(v, dict) and self.version(v.get("version", ""))}

    def _save_slots(self, slots: dict) -> None:
        if slots:
            _write_json(self.dir / "active.json", {"slots": slots})
        else:
            (self.dir / "active.json").unlink(missing_ok=True)

    def slot_of(self, avatar: str | None) -> str | None:
        """形象文件名 -> 槽位 (VRM 内容摘要); 没有这个文件返回 None."""
        return avatar_sha(self.cfg, avatar)

    def active(self, slot: str = GLOBAL) -> dict | None:
        return self.slots().get(slot)

    def active_version(self, slot: str = GLOBAL) -> str:
        a = self.active(slot)
        return a["version"] if a else ("factory" if slot == GLOBAL else "base")

    def effective(self, avatar: str | None) -> tuple[str, str]:
        """这个形象现在用哪一槽、哪一版: 有自己的就用自己的, 没有就用基础的."""
        sha = self.slot_of(avatar)
        s = self.slots()
        if sha and sha in s:
            return sha, s[sha]["version"]
        return GLOBAL, self.active_version(GLOBAL)

    def changes(self) -> list[dict]:
        return list(_lines(self.dir / "changes.jsonl"))

    def _change(self, action: str, version: str, prev: str, family: str | None, why: str, numbers=None, line: str | None = None,
                slot: str = GLOBAL, avatar: str | None = None) -> dict:
        n = len(self.changes()) + 1
        c = {"id": f"c{n:04d}", "at": _now(), "action": action, "version": version, "from": prev, "family": family, "why": why,
             "numbers": numbers or {}, "line": line, "slot": slot, "avatar": avatar}
        _append(self.dir / "changes.jsonl", c)
        log.info("动作成长: %s %s -> %s (%s, %s)", action, prev, version, avatar or slot, why)
        return c

    def _revalidate(self) -> list[dict]:
        r = self.state.get("revalidate")
        if isinstance(r, str):  # 旧格式: 一个版本号, 基础槽位
            r = [{"version": r, "slot": GLOBAL}]
        return list(r or [])

    def _mark_revalidate(self, version: str, slot: str, why: str) -> None:
        r = [x for x in self._revalidate() if x.get("version") != version]
        r.append({"version": version, "slot": slot, "why": why, "at": _now()})
        self._save_state(revalidate=r)

    def _clear_revalidate(self, version: str) -> None:
        self._save_state(revalidate=[x for x in self._revalidate() if x.get("version") != version])

    def _seen(self, avatar) -> None:
        """小屋发来的形象 (文件名) 记下来: 没见过的 (按内容摘要) 要先体检 (needs_work)."""
        f = avatar.get("file") if isinstance(avatar, dict) else avatar
        sha = self.slot_of(f) if f else None
        if not sha:
            return
        seen = dict(self.state.get("avatars_seen") or {})
        if sha not in seen:
            seen[sha] = {"file": f, "first": _now()}
            self._save_state(avatars_seen=seen)

    def profile(self, sha: str) -> dict | None:
        """形象的体检档案 (tools/motion_growth.py 的 calibrate 写的): 身体比例、基础参数下各种毛病的基线、运行栈指纹."""
        return _read_json(self.dir / "avatars" / f"{sha}.json") if re.fullmatch(r"a[0-9a-f]{11}", sha or "") else None

    def unprofiled(self) -> list[dict]:
        """见过、还没体检的形象: [{sha, file}]."""
        return [{"sha": k, **v} for k, v in (self.state.get("avatars_seen") or {}).items() if not self.profile(k)]

    def pending_contract(self) -> list[str]:
        """旧验收算法/没有清单的版本仍可读, 但不当成满足过现在的覆盖要求, 要补验."""
        return sorted({e["version"] for e in self.slots().values()
                       if ((self.version(e["version"]) or {}).get("acceptance") or {}).get("gate_version") != GATE_VERSION})

    # 参数
    def check_stale(self) -> str | None:
        """在用的版本是不是在别的出厂参数上调的: 是的话那一槽停用 (回到基础 / 出厂), 记下要重新验证, 返回原因."""
        s, why = self.slots(), None
        sha = self.factory_sha()
        for slot, e in list(s.items()):
            doc = self.version(e["version"]) or {}
            if doc.get("base", {}).get("factory_sha") == sha:
                continue
            why = f"出厂参数 ({self.factory.name}) 变了, {e['version']} 是在旧的上面调的, 先停用, 下次空闲重新验证"
            del s[slot]
            self._mark_revalidate(e["version"], slot, why)
            self._change("suspend", "factory" if slot == GLOBAL else "base", e["version"], doc.get("family"), why, slot=slot,
                         avatar=e.get("file"))
        if why:
            self._save_slots(s)
        return why

    def full_values(self, version: str) -> dict | None:
        """这一版的完整参数 (页面不重新载入就换上时用). factory: 出厂值加上代码里的默认值 (从最早那一版的 before 取);
        base: 基础槽位现在用的那一版."""
        if version == "base":
            return self.full_values(self.active_version(GLOBAL))
        if version != "factory":
            doc = self.version(version)
            return dict(doc.get("full") or doc.get("values") or {}) if doc else None
        fac = dict(self.factory_doc().get("values") or {})
        sha = self.factory_sha()
        for v in self.versions():
            d = self.version(v) or {}
            if d.get("base", {}).get("factory_sha") == sha and d.get("base", {}).get("version") in ("factory", "base") and d.get("before"):
                return {**d["before"], **fac}
        return fac

    def params_payload(self) -> dict | None:
        """/motiondb/params.json (所有形象都读这一份): 基础槽位有换上的版本时, 出厂参数叠上它; 出厂参数时 None (照常给文件).
        形象自己那一版不在这里, 小屋按形象问 (avatar_params). 配置 motion.growth.overlay = false 时也是 None."""
        if not self._g("overlay", True):
            return None
        self.check_stale()
        a = self.active(GLOBAL)
        if not a:
            return None
        doc = self.version(a["version"])
        fac = self.factory_doc()
        values = {**(fac.get("values") or {}), **(doc.get("full") or doc.get("values") or {})}
        return {**fac, "values": values, "growth": {"version": a["version"], "keys": doc.get("keys", []), "since": a.get("since"),
                                                     "family": doc.get("family"), "factory": fac.get("source")}}

    def avatar_params(self, avatar: str | None) -> dict:
        """这个形象现在该用的完整参数: {slot, version, values}."""
        self.check_stale()
        slot, ver = self.effective(avatar)
        return {"slot": slot, "version": ver, "values": self.full_values(ver)}

    def adopt(self, doc: dict) -> dict:
        """换上一版 (tools/motion_growth.py 验证过才调). doc: {keys, values, full, before, family, problem, why, numbers, line, run,
        acceptance (验收清单), fingerprint, scope: {avatars: [{file, sha}]} (不给 = 基础槽位)}."""
        avatars = (doc.get("scope") or {}).get("avatars") or []
        targets = [(a["sha"], a.get("file")) for a in avatars if a.get("sha")] or [(GLOBAL, None)]
        s = self.slots()
        prev = {slot: (s[slot]["version"] if slot in s else ("factory" if slot == GLOBAL else "base")) for slot, _ in targets}
        vs = self.versions()
        v = f"v{(int(vs[-1][1:]) + 1 if vs else 1):04d}"
        first = targets[0][0]
        doc = {**doc, "version": v, "date": _now(),
               "base": {"factory_sha": self.factory_sha(), "code": code_version(self.root), **(doc.get("base") or {}),
                        "version": prev[first]}}
        _write_json(self.dir / "params" / f"{v}.json", doc)
        for slot, file in targets:
            s[slot] = {"version": v, "since": _now(), "prev": prev[slot], "family": doc.get("family"), "file": file}
        self._save_slots(s)
        c = None
        for slot, file in targets:
            c = self._change("adopt", v, prev[slot], doc.get("family"), doc.get("why", ""), doc.get("numbers"), doc.get("line"),
                             slot=slot, avatar=file)
        self._clear_revalidate(v)
        return {"version": v, "change": c, "slots": [t[0] for t in targets]}

    def rollback(self, to: str | bool | None = True, why: str = "用户在设置里点了退回", auto: bool = False, slot: str = GLOBAL) -> dict:
        """退回: to = 'factory' / 'base' / 'v0002' / True (这一槽的上一版). slot: 哪一槽 (基础, 或者形象的摘要)."""
        s = self.slots()
        cur = self.active_version(slot)
        none = "factory" if slot == GLOBAL else "base"
        if to is True or to is None:
            to = (s.get(slot) or {}).get("prev", none)
        if to in ("factory", "base"):
            to = none
        elif not self.version(str(to)):
            raise ValueError(f"没有这一版: {to}")
        if to == cur:
            return {"version": cur, "change": None}
        file = (s.get(slot) or {}).get("file")
        if to == none:
            s.pop(slot, None)
        else:
            s[slot] = {"version": to, "since": _now(), "prev": cur, "family": (self.version(to) or {}).get("family"), "file": file}
        self._save_slots(s)
        fam = (self.version(cur) or {}).get("family") if cur not in ("factory", "base") else None
        line = _line(fam, "back") if auto else None
        c = self._change("auto-rollback" if auto else "rollback", to, cur, fam, why, line=line, slot=slot, avatar=file)
        return {"version": to, "change": c}

    # 候选: 验证过、还没换上的 (要等适用范围的验收做完、用户同意才换上)
    def candidates(self) -> list[dict]:
        d = self.dir / "candidates"
        return [x for x in (_read_json(p) for p in sorted(d.glob("k[0-9][0-9][0-9][0-9].json"))) if x] if d.is_dir() else []

    def add_candidate(self, doc: dict) -> dict:
        n = len(self.candidates()) + 1
        doc = {**doc, "id": f"k{n:04d}", "added": _now(), "status": doc.get("status") or "pending"}
        _write_json(self.dir / "candidates" / f"{doc['id']}.json", doc)
        return doc

    def candidate_stale(self, c: dict) -> bool:
        """最近一次核对以后运行栈 (代码、评估器、出厂参数、模型) 变了: "核对过了"已经不作数, 换上前要重核 (candidate-adopt 也会拦)."""
        last = (c.get("checks") or [None])[-1]
        if not last or c.get("status") == "adopted":
            return False
        return ((last.get("contract") or {}).get("gate_version") != GATE_VERSION or last.get("baseline") != self.baseline() or
                last.get("values") != c.get("values") or
                last.get("fingerprint") != {a["file"]: self.fingerprint(a["file"]) for a in (c.get("scope") or {}).get("avatars") or []})

    def update_candidate(self, cid: str, **kw) -> dict:
        p = self.dir / "candidates" / f"{cid}.json"
        doc = _read_json(p)
        if not doc:
            raise ValueError(f"没有这个候选: {cid}")
        doc.update(kw, updated=_now())
        _write_json(p, doc)
        return doc

    # 重置
    HISTORY = ("faults*.jsonl", "exposure.jsonl", "problems.json", "changes.jsonl", "params/", "candidates/", "runs/", "reports/")

    def reset(self, why: str = "重置小窝学习", history: bool = False) -> dict:
        """重置. 默认只退回出厂参数: 停掉正在练的, 所有槽位 (基础和每个形象) 回到出厂, 要重新验证的清掉;
        毛病记录、曝光、她换上过的每一版、候选、练习记录、问题报告、换上退回的记录都留着 (评审第 6 节: 退回出厂参数不等于删除历史训练记录).
        history=True: 连这些历史一起清, 删除代次 (gen) 加一 —— 重置之前小屋攒着没发的那些, 之后也不收.
        两种都保留用户的设置 (空闲时自己练的开关). 仓库 logs/motion-growth/ 里的实验记录副本不在这里删 (那是开发的实验数据).
        返回 {gen, history, cleared: [...], kept: [...]}, 设置页照着如实列."""
        stopped = self.stop("重置小窝学习")
        slots = self.slots()
        cleared = []
        if stopped:
            cleared.append("正在练的那一次 (停了)")
        if slots:
            cleared.append("在用的参数: " + ", ".join(f"{e.get('file') or '基础'} {e['version']}" for e in slots.values()) + " (回到出厂)")
        (self.dir / "active.json").unlink(missing_ok=True)
        (self.dir / "runner.json").unlink(missing_ok=True)
        st = self.state
        kept = ["空闲时自己练的开关 (用户设置)", "仓库 logs/motion-growth/ 的实验记录副本 (开发的实验数据)"]
        if not history:
            have = [g for g in self.HISTORY if (list(self.dir.glob(g)) if "*" in g else (self.dir / g.rstrip("/")).exists())]
            kept += [f"历史: {g}" for g in have]
            self._save_state(revalidate=[], validated_code={}, tried={}, reset_at=_now(), reset_why=why)
            self._change("reset", "factory", "-", None, f"{why} (只退回出厂参数, 历史留着)")
            return {"gen": self.gen, "history": False, "cleared": cleared, "kept": kept}
        for p in list(self.dir.glob("faults*.jsonl")) + [self.dir / n for n in ("exposure.jsonl", "problems.json", "changes.jsonl")]:
            if p.exists():
                p.unlink()
                cleared.append(p.name)
        for d in ("params", "candidates", "runs", "reports"):
            if (self.dir / d).is_dir():
                shutil.rmtree(self.dir / d, ignore_errors=True)
                cleared.append(d + "/")
        gen = int(st.get("gen") or 0) + 1
        _write_json(self.dir / "state.json", {"auto": bool(st.get("auto")), "gen": gen, "reset_at": _now(), "reset_why": why})
        self._change("reset", "factory", "-", None, f"{why} (连历史一起清)")
        return {"gen": gen, "history": True, "cleared": cleared, "kept": kept}

    # 收数据
    def ingest(self, body: dict) -> dict:
        """小屋发来的失败片段和曝光. 返回 {stored, dropped, gen}. 旧代次的 (重置之前攒的) 整批不收."""
        if not isinstance(body, dict):
            raise ValueError("要一个 JSON 对象")
        recs = body.get("records") or []
        if not isinstance(recs, list) or len(recs) > MAX_RECORDS:
            raise ValueError(f"records 要是不超过 {MAX_RECORDS} 条的列表")
        gen = self.gen
        try:
            sent_gen = int(body.get("gen") or 0)
        except (TypeError, ValueError):
            sent_gen = 0
        if sent_gen < gen:
            return {"stored": 0, "dropped": len(recs), "gen": gen, "stale": True}
        params = str(body.get("params") or "factory")[:16]
        code = code_version(self.root)
        today = datetime.now().strftime("%Y-%m-%d")
        st = self.state
        day = st.get("day") if st.get("day", {}).get("date") == today else {"date": today, "n": 0}
        stored = dropped = 0
        f = self.dir / "faults.jsonl"
        if f.exists() and f.stat().st_size > ROTATE_BYTES:
            f.rename(f.with_name(f"faults-{datetime.now():%Y%m%d-%H%M%S}.jsonl"))
        for r in recs:
            if not isinstance(r, dict) or not _KIND.match(str(r.get("kind", ""))) or r.get("cls") not in ("motion", "env", None):
                dropped += 1
                continue
            if day["n"] >= MAX_PER_DAY:
                dropped += 1
                continue
            r = dict(r)
            r["ring"] = (r.get("ring") or [])[:45]
            line = json.dumps(r, ensure_ascii=False)
            if len(line) > 48_000:
                r.pop("ring", None)
            r.update(got=_now(), code=code, params=str(r.get("params") or params)[:16], gen=gen)
            _append(f, r)
            day["n"] += 1
            stored += 1
        self._seen(body.get("avatar"))
        exp = body.get("exposure") or {}
        if isinstance(exp, dict):
            exp = {k: int(v) for k, v in exp.items() if k in FAMILIES and isinstance(v, (int, float)) and 0 < v <= 10000}
            if exp:
                av = body.get("avatar") if isinstance(body.get("avatar"), dict) else {}
                _append(self.dir / "exposure.jsonl", {"at": _now(), "params": params, "code": code, "gen": gen,
                                                      "avatar": str(av.get("file") or "")[:64] or None,
                                                      "session": str(body.get("session") or "")[:32], "exposure": exp})
        self._save_state(day=day)
        return {"stored": stored, "dropped": dropped, "gen": gen}

    def faults(self, days: float = 30) -> list[dict]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        out = []
        for p in sorted(self.dir.glob("faults*.jsonl")):
            out += [r for r in _lines(p) if r.get("got", "") >= since]
        return out

    def rates(self, since: str | None = None, until: str | None = None) -> dict:
        """按参数版本、本事: {版本: {本事: {exposure, motion, env}}}. 版本号全局唯一, 形象那一槽的版本自然只有那个形象的数据."""
        out: dict = {}
        ok = lambda t: (not since or t >= since) and (not until or t < until)  # noqa: E731
        for e in _lines(self.dir / "exposure.jsonl"):
            if not ok(e.get("at", "")):
                continue
            for fam, n in (e.get("exposure") or {}).items():
                out.setdefault(e.get("params", "factory"), {}).setdefault(fam, {"exposure": 0, "motion": 0, "env": 0})["exposure"] += n
        for r in self.faults(days=3650):
            if not ok(r.get("got", "")) or r.get("cls") not in ("motion", "env"):
                continue
            fam = r.get("family") or "stand"
            out.setdefault(r.get("params", "factory"), {}).setdefault(fam, {"exposure": 0, "motion": 0, "env": 0})[r["cls"]] += 1
        return out

    def watch(self) -> dict:
        """换上以后盯着日常的失败率 (每一槽各盯各的): 这一类本事 (和全部本事加起来) 比换上前明显变差就自动退回那一槽."""
        out = {}
        for slot, a in self.slots().items():
            prev, since = a.get("prev", "factory"), a.get("since")
            before = (datetime.fromisoformat(since) - timedelta(days=30)).isoformat(timespec="seconds") if since else None
            post = self.rates(since=since).get(a["version"], {})
            pre_key = self.active_version(GLOBAL) if prev == "base" else prev
            pre = self.rates(since=before, until=since).get(pre_key, {})
            fam = a.get("family")
            tot = lambda d: (sum(x["motion"] for x in d.values()), sum(x["exposure"] for x in d.values()))  # noqa: E731
            checks = {}
            if fam:
                q, p = post.get(fam, {}), pre.get(fam, {})
                checks[fam] = worse(q.get("motion", 0), q.get("exposure", 0), p.get("motion", 0), p.get("exposure", 0))
            (nq, eq), (np_, ep) = tot(post), tot(pre)
            checks["all"] = worse(nq, eq, np_, ep)
            bad = [k for k, c in checks.items() if c["worse"]]
            res = {"version": a["version"], "checked": True, "checks": checks, "rolled_back": False}
            if bad:
                c = checks[bad[0]]
                why = (f"换上 {a['version']} 以后日常里 {bad[0]} 的动作失败率 {c['rate_post']:.3f} 次/次, 换上前 {c['rate_pre']:.3f} "
                       f"({c['ratio']:.2f} 倍, p = {c['p']:.3f}), 自动退回")
                self.rollback(prev, why=why, auto=True, slot=slot)
                res.update(rolled_back=True, why=why)
            out[slot] = res
        self._save_state(last_watch=_now())
        g = out.get(GLOBAL)
        return {"slots": out, **(g or {"version": "factory", "checked": False}), "rolled_back": any(x["rolled_back"] for x in out.values())}

    # 说一句
    def announce(self, avatar: str | None = None) -> dict | None:
        """还没说过的那一句: 基础槽位的, 或者这个形象自己那一槽的 (别的形象的不说)."""
        said = set(self.state.get("announced") or [])
        mine = self.slot_of(avatar)
        for c in reversed(self.changes()):
            if c.get("line") and c["id"] not in said and c.get("slot", GLOBAL) in (GLOBAL, mine):
                return {"id": c["id"], "line": c["line"], "action": c["action"], "version": c["version"]}
        return None

    def ack(self, cid: str) -> None:
        said = list(self.state.get("announced") or [])
        if cid not in said:
            said.append(cid)
            self._save_state(announced=said[-50:])

    # 练
    def runner_alive(self) -> dict | None:
        r = _read_json(self.dir / "runner.json")
        if not r:
            return None
        return r if _pid_alive(int(r.get("pid", 0))) else None

    def needs_work(self) -> str | None:
        """有没有要练的: 要重新验证的版本 (出厂参数变了停用的、代码变了的、没存验收清单的)、上次练以后新记的动作缺陷."""
        st = self.state
        if r := self._revalidate():
            return f"重新验证 {r[0]['version']}"
        if p := self.pending_contract():
            return f"补验收清单 {p[0]}"
        if u := self.unprofiled():
            return f"新形象 {u[0]['file']} 先体检"
        code = code_version(self.root)
        for e in self.slots().values():
            doc = self.version(e["version"]) or {}
            if doc.get("base", {}).get("code") != code and (st.get("validated_code") or {}).get(e["version"]) != code:
                return f"代码变了, 重新验证 {e['version']}"
            if self.stack_changed(doc):
                return f"运行栈变了 (评估器/动作库/形象等), 重新验证 {e['version']}"
        last = (st.get("last_run") or {}).get("at", "")
        n = sum(1 for r in self.faults(days=30) if r.get("cls") == "motion" and r.get("got", "") > last)
        return f"新记了 {n} 条动作缺陷" if n >= int(self._g("min_new", 5)) else None

    def _g(self, k, default):
        return ((self.cfg.get("motion") or {}).get("growth") or {}).get(k, default)

    def can_run(self) -> str | None:
        """这台电脑上练不练得了: 要 node 和几份工具 (评估在无界面浏览器里跑). 练不了返回原因.
        装好的版本 (installer/worldmodel.iss) 现在只带了 serve.py 几个工具、没带 node, 练不了; 记失败片段、换上、退回不受影响."""
        need = ["tools/motion_growth.py", "tools/tune_motion.py", "tools/motion_eval.mjs", "web/motion-growth-probe.js"]
        miss = [f for f in need if not (self.root / f).exists()]
        if miss:
            return "缺 " + ", ".join(miss)
        if not shutil.which("node"):
            return "这台电脑上没有 node"
        return None

    def tick(self, gate=idle_gate, page_busy: bool = False) -> dict:
        """小屋每 5 分钟问一次: 开关开着、没在练、她不忙、有要练的、电脑空闲、离上次开始够久 -> 起 tools/motion_growth.py run.
        page_busy: 小屋那边说她在忙 (对话栏开着、在说话)."""
        st = self.state
        out = {"auto": bool(st.get("auto")), "started": False}
        if not st.get("auto"):
            return out
        if (why := self.busy() or ("她在和你说话" if page_busy else None)) is not None:
            out["why"] = why
            return out
        if (why := self.can_run()) is not None:
            out["why"] = f"这台电脑上练不了: {why}"
            return out
        if self.runner_alive():
            out["running"] = True
            return out
        last = (st.get("last_start") or "")
        if last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=float(self._g("min_gap_h", 2))):
            out["why"] = "离上次开始不到两小时"
            return out
        work = self.needs_work()
        if not work:
            out["why"] = "没有要练的"
            return out
        g = gate(float(self._g("idle_s", 600)))
        out["gate"] = g
        if not g["ok"]:
            out["why"] = "; ".join(g["why"])
            return out
        self.start(work)
        out["started"] = True
        return out

    def start(self, why: str) -> None:
        tool = self.root / "tools" / "motion_growth.py"
        (self.dir / "runs").mkdir(parents=True, exist_ok=True)
        logf = (self.dir / "runs" / f"runner-{datetime.now():%Y%m%d-%H%M%S}.log").open("w", encoding="utf-8")
        flags = 0
        if sys.platform == "win32":  # 低一档的优先级 (子进程: 评估用的浏览器、node 跟着低), 不开窗口
            flags = 0x00004000 | 0x08000000 | 0x00000200  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        self.runner = subprocess.Popen([sys.executable, str(tool), "run", "--data-dir", str(self.dir)], cwd=self.root,
                                       stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
        _write_json(self.dir / "runner.json", {"pid": self.runner.pid, "started": _now(), "why": why})
        self._save_state(last_start=_now())
        log.info("动作成长: 空闲了, 开始练 (%s), 进程 %d", why, self.runner.pid)

    def stop(self, why: str = "用户点了停") -> bool:
        r = self.runner_alive()
        if not r:
            return False
        _kill_tree(int(r["pid"]))
        (self.dir / "runner.json").unlink(missing_ok=True)
        self._save_state(last_stop={"at": _now(), "why": why})
        log.info("动作成长: 停下了 (%s)", why)
        return True

    def status(self, avatar: str | None = None) -> dict:
        st = self.state
        s = self.slots()
        faults = self.faults(days=30)
        by: dict = {}
        for r in faults:
            k = f"{r.get('kind')}|{r.get('cls')}"
            by[k] = by.get(k, 0) + 1
        reports = sorted(p.name for p in (self.dir / "reports").glob("*.md")) if (self.dir / "reports").is_dir() else []
        last = next((c for c in reversed(self.changes()) if c.get("action") != "switch"), None)  # 开关的记录不算"最近换了什么"
        g = s.get(GLOBAL)
        cands = [{**{k: c.get(k) for k in ("id", "status", "problem", "scope", "source", "added", "note")},
                  "stale": self.candidate_stale(c)} for c in self.candidates()]
        out = {"dir": str(self.dir), "auto": bool(st.get("auto")), "active": g["version"] if g else "factory",
               "since": g.get("since") if g else None, "versions": self.versions(), "running": self.runner_alive(),
               "slots": {k: {**v, "slot": k} for k, v in s.items()},
               "last_run": st.get("last_run"), "last_stop": st.get("last_stop"), "revalidate": self._revalidate(),
               "pending_contract": self.pending_contract(), "candidates": cands, "gen": self.gen,
               "avatars_seen": self.state.get("avatars_seen") or {}, "unprofiled": [u["file"] for u in self.unprofiled()],
               "faults_30d": by, "reports": reports, "last_change": last, "announce": self.announce(avatar),
               "code": code_version(self.root), "factory_sha": self.factory_sha(), "can_run": self.can_run() is None,
               "cannot_run": self.can_run()}
        if avatar:
            slot, ver = self.effective(avatar)
            out["avatar"] = {"file": avatar, "slot": slot, "version": ver}
        return out


def _line(family: str | None, which: str) -> str:
    """她说的那一句. which: better (毛病出的次数少了) / lighter (次数没少, 只是程度轻了: 超阈的量少了) / back (退回).
    说准: 次数没少就不说"练好了"."""
    lines = {"carry": ("我最近把拿杯子练稳了", "拿东西时手一顿的毛病轻一点了, 还没完全好", "前两天练的拿东西的法子不太对, 我先换回原来的了"),
             "sit": ("坐下站起比以前顺了", "坐下站起没那么别扭了, 还没完全好", "新练的坐法不太舒服, 先换回原来的了"),
             "walk": ("走路脚不打滑了", "走路脚打滑轻一点了, 还没完全好", "新练的走法不太稳, 先换回原来的了"),
             "stand": ("站着比以前稳了", "站着晃得轻一点了, 还没完全好", "新练的站姿不太对, 先换回原来的了"),
             "arms": ("手臂不会老撞到身上了", "手臂撞到身上轻一点了, 还没完全好", "新练的手上动作不太对, 先换回原来的了")}
    good, light, back = lines.get(family or "", ("最近动作练顺了一点", "有个动作的毛病轻一点了, 还没完全好", "新练的动作不太对, 先换回原来的了"))
    return {"better": good, "lighter": light}.get(which, back)


def line_for(family: str | None, which: str = "better") -> str:
    return _line(family, which)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# -- HTTP ----------------------------------------------------------------------------------
KEY = None    # app 里放 Growth 的键 (aiohttp 的 AppKey, register 时建)
_TASK = None


def register(app, cfg: dict, refusal, busy=None) -> None:
    """挂接口 (app.py 调). refusal(request): 来源不对时返回原因 (app.py 的 ws_refusal); busy(): 她在忙时返回原因 (session_busy)."""
    import asyncio

    from aiohttp import web
    global KEY, _TASK
    KEY = KEY or web.AppKey("motion_growth", Growth)
    _TASK = _TASK or web.AppKey("motion_growth_watch", asyncio.Task)
    app[KEY] = growth = Growth(cfg)
    if busy is not None:
        growth.busy = busy

    async def body_of(request):
        try:
            b = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="要 JSON")
        if not isinstance(b, dict):
            raise web.HTTPBadRequest(text="要一个 JSON 对象")
        return b

    async def api_faults(request):
        if (why := refusal(request)) is not None:
            raise web.HTTPForbidden(text=why)
        if (request.content_length or 0) > 2 << 20:
            raise web.HTTPRequestEntityTooLarge(max_size=2 << 20, actual_size=request.content_length)
        try:
            return web.json_response(growth.ingest(await body_of(request)))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def api_growth(request):
        if (why := refusal(request)) is not None:
            raise web.HTTPForbidden(text=why)
        if request.method == "GET":
            return web.json_response(growth.status(request.query.get("avatar") or None))
        b = await body_of(request)
        out: dict = {}
        if "auto" in b:
            growth._save_state(auto=bool(b["auto"]))
            if not b["auto"]:
                growth.stop("用户关了开关")
            growth._change("switch", growth.active_version(), growth.active_version(), None, f"用户{'打开' if b['auto'] else '关了'}空闲时自己练")
        if b.get("stop"):
            out["stopped"] = growth.stop()
        avatar = str(b.get("avatar") or "")[:64] or default_avatar(cfg)
        if b.get("rollback"):
            slot = str(b.get("slot") or "") or (growth.slot_of(b["avatar"]) if b.get("avatar") else GLOBAL) or GLOBAL
            try:
                out["rollback"] = growth.rollback(b["rollback"] if isinstance(b["rollback"], str) else True, slot=slot)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc))
        if b.get("reset"):
            if b.get("confirm") != "reset":  # 要明说 (设置页确认以后带上), 防止误点、别的页面顺手发
                raise web.HTTPBadRequest(text='重置要带 {"confirm": "reset"}')
            out["reset"] = growth.reset(str(b.get("why") or "重置小窝学习")[:80], history=b.get("history") is True)
        if b.get("ack"):
            growth.ack(str(b["ack"])[:16])
        if b.get("tick"):
            out["tick"] = await asyncio.get_running_loop().run_in_executor(  # 空闲判断要取样 1 秒 CPU
                None, lambda: growth.tick(page_busy=bool(b.get("busy"))))
        p = growth.avatar_params(avatar)
        out.update(auto=bool(growth.state.get("auto")), announce=growth.announce(avatar), active=growth.active_version(),
                   gen=growth.gen, avatar={"file": avatar, "slot": p["slot"], "version": p["version"]})
        if b.get("tick") and str(b.get("params") or "") != p["version"]:
            out["params"] = {"version": p["version"], "values": p["values"]}
        return web.json_response(out)

    app.router.add_post("/api/motion/faults", api_faults)
    app.router.add_get("/api/motion/growth", api_growth)
    app.router.add_post("/api/motion/growth", api_growth)

    async def watch_loop(app_):
        n = 0
        while True:
            await asyncio.sleep(10)
            n += 1
            try:
                # 在练的时候她忙起来了 (你开始和她说话、QQ 转来任务单): 马上停. 键鼠、全屏、通话由练习进程自己每 15 秒查
                if growth.runner_alive() and (why := growth.busy()):
                    growth.stop(f"{why}, 先停")
                if n % 180 == 0:  # 每 30 分钟盯一次日常失败率
                    growth.watch()
            except Exception as exc:  # noqa: BLE001
                log.warning("动作成长: 盯失败率出错: %s", exc)

    async def on_start(app_):
        try:
            growth.check_stale()
        except Exception as exc:  # noqa: BLE001
            log.warning("动作成长: 检查参数版本出错: %s", exc)
        app_[_TASK] = asyncio.create_task(watch_loop(app_))

    async def on_clean(app_):
        t = app_.get(_TASK)
        if t:
            t.cancel()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_clean)


def growth_of(app) -> Growth | None:
    return app.get(KEY) if KEY else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wm.motion_growth", description="动作成长: 数据在哪、状态、退回、盯失败率")
    ap.add_argument("cmd", choices=["where", "status", "rollback", "watch", "reset"])
    ap.add_argument("to", nargs="?", help="rollback: 退回到哪一版 (factory / base / v0002; 不给就是这一槽的上一版)")
    ap.add_argument("--avatar", help="rollback / status: 哪个形象 (models/vrm 下的文件名; 不给就是基础槽位)")
    ap.add_argument("--yes", action="store_true", help="reset: 确认 (参数回到出厂, 停掉正在练的; 历史留着)")
    ap.add_argument("--history", action="store_true", help="reset: 连历史 (毛病记录、每一版、候选、练习记录) 一起清")
    ap.add_argument("--config")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    from .config import load_config
    g = Growth(load_config(a.config, a.set))
    if a.cmd == "where":
        print(g.dir)
    elif a.cmd == "status":
        print(json.dumps(g.status(a.avatar), ensure_ascii=False, indent=1))
    elif a.cmd == "rollback":
        slot = g.slot_of(a.avatar) if a.avatar else GLOBAL
        if not slot:
            raise SystemExit(f"没有这个形象: {a.avatar}")
        print(json.dumps(g.rollback(a.to or True, why="命令行退回", slot=slot), ensure_ascii=False, indent=1))
    elif a.cmd == "reset":
        if not a.yes:
            what = "连历史 (失败片段、她换上过的每一版、候选、练习记录) 一起清, " if a.history else ""
            print(f"会把 {g.dir} 的动作参数退回出厂、停掉正在练的, {what}确认加 --yes")
            return 1
        print(json.dumps(g.reset("命令行重置", history=a.history), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(g.watch(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
