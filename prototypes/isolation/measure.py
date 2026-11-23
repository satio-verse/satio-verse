# ruff: noqa: E402
"""隔离原型的测量脚本 (2026-11-14, 插件宿主方案第六节第 6 步). 用户自己双击 run_me.cmd 在普通权限下跑.

回答一件事: 把一个 Python 程序关进受限的身份里跑, 它还读不读得到用户的文件、写不写得出自己的文件夹、
连不连得上本机回环、要不要得到管理员权限. 四种"关法"各量一遍, 填进同一张表:
  应用容器 / 受限令牌·只降级 / 受限令牌·加限制 SID / 对照 (不关).

用法:
  python measure.py          真测, 六步: 自查 -> 建东西 -> 登记沙箱身份加权限 -> 起探针 -> 提权那一条 -> 收尾
  python measure.py --plan   只把六步要做的事打出来 (带实际路径), 什么都不建、不起、不改. 管理员权限下也能跑.
退出码: 0 测完 (各关法能不能用是测量结果, 不影响退出码); 1 中途出错 (收尾照做); 2 环境不满足;
        3 自己是管理员权限 (或者查不出来), 什么都没做就停了.

红线 (和 README 一致): 自己是管理员就停, 没有任何"管理员也照跑"的开关, 也没有降权再跑的办法; 只碰自己建的假文件;
不下载、不安装、不连外网 (只连自己在 127.0.0.1 上开的端口); 不改系统设置; 结果里不带个人信息.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 不在原型目录里留 __pycache__ (跑完该只剩 results\)

import argparse
import datetime
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import sandbox_win as sw

HERE = Path(__file__).resolve().parent

APPC_NAME = "wm.ayane.isolation.proto"
APPC_DISPLAY = "WorldModel isolation prototype"
APPC_DESC = "Temporary sandbox identity of the isolation prototype. Removed at the end of every run."
PROBE_LIMIT_S = 20.0   # 每个探针最多 20 秒, 到时间就结束作业
PING_LIMIT_S = 15.0    # 起来以后第一句话 (ping) 最多等多久
CMD_LIMIT_S = 8.0      # 普通指令最多等多久
ELEV_CMD_LIMIT_S = 12.0
ELEV_WAIT_S = 5.0      # 提权请求发出后, 最多等 5 秒看结果文件
ELEV_REFUSED_LOOK_S = 0.5   # 请求当场被拒 (没有进程) 时, 结果文件只再看这么久
SETTLE_S = 0.2         # 连了测试端口以后, 给监听线程记下这次连接的时间
FAKE_BOX_NAME = "fake_vault.txt"
FAKE_BOX_LINE = "FAKE-BOX-0001 this is not a real secret"
DECOY_LINE = "FAKE-DECOY-0002 this is not a real secret"
MARKER_NAME = ".wm_isolation_work.json"
TEMP_PREFIX = "wm_iso_"
STEP_NAMES = ["自查", "建东西", "登记沙箱身份、加权限", "起探针", "提权那一条", "收尾"]
UAC_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
UAC_VALUES = ("EnableLUA", "ConsentPromptBehaviorAdmin", "PromptOnSecureDesktop")
ELEV_QUESTION = "提权那一条真试吗？输入 y 回车才试，直接回车不试："

# 一次结果里的四种说法 (外加"测法不成立": 对照没写 ELEVATED 时, 提权那一条用它)
CAN, CANNOT, UNKNOWN, ERROR, INVALID = "can", "cannot", "unknown", "error", "invalid"
STATE_TEXT = {CAN: "做得到", CANNOT: "做不到", UNKNOWN: "没验", ERROR: "出错", INVALID: "测法不成立"}

ITEM_KEYS = ("python", "read_fake", "read_decoy", "write_work", "write_temp", "write_home", "connect", "elevate")
ITEM_TITLES = {"python": "Python 起得来", "read_fake": "读假保管箱", "read_decoy": "读假文件",
               "write_work": "写外面(work)", "write_temp": "写外面(临时)", "write_home": "写自己文件夹",
               "connect": "连本机回环", "elevate": "要管理员权限"}
# 这种关法"能用"时每一样该是什么 (对照那一行则全是"做得到")
EXPECT = {"python": CAN, "read_fake": CANNOT, "read_decoy": CANNOT, "write_work": CANNOT, "write_temp": CANNOT,
          "write_home": CAN, "connect": CANNOT, "elevate": CANNOT}
PROBLEM_TEXT = {
    ("python", CANNOT): "Python 起不来", ("read_fake", CAN): "读得到假保管箱", ("read_decoy", CAN): "读得到假文件",
    ("write_work", CAN): "写得进 work (自己文件夹以外)", ("write_temp", CAN): "写得进临时文件夹 (自己文件夹以外)",
    ("write_home", CANNOT): "写不进自己的文件夹", ("connect", CAN): "连得上本机回环", ("elevate", CAN): "要得到了管理员权限",
}
# 对照放第一个跑: 它起得来、读写连提权都做得到, 就排除了“运行时刚拷完还在被扫”这类和时间有关的原因
MODES = [("control", "对照(不关)"), ("appcontainer", "应用容器"), ("restricted_downgrade", "受限令牌·只降级"),
         ("restricted_sids", "受限令牌·加限制SID")]


# ---------------------------------------------------------------- 个人信息抹除
class Redactor:
    """把文字里的家目录、用户名、电脑名、SID 抹掉. 结果会被对话读走, 也可能归档进知识库, 不能带这些."""

    def __init__(self, home: str = "", names: list[str] | None = None, sids: list[str] | None = None) -> None:
        self.home = home.rstrip("\\/") if home else ""
        self.names = sorted({n for n in (names or []) if n and len(n) >= 3}, key=len, reverse=True)
        self.sids = sorted({s for s in (sids or []) if s}, key=len, reverse=True)

    @classmethod
    def from_env(cls) -> Redactor:
        names = [os.environ.get("USERNAME", ""), os.environ.get("COMPUTERNAME", ""), os.environ.get("USERDOMAIN", "")]
        try:
            names.append(getpass.getuser())
        except Exception:  # noqa: BLE001 - 取不到用户名不影响别的
            pass
        try:
            # 不用 platform.node(): Python 3.10 上它第一次会起一个 cmd /c ver; socket.gethostname() 不起进程
            names.append(socket.gethostname())
        except Exception:  # noqa: BLE001
            pass
        return cls(os.path.expanduser("~"), names)

    def add_sids(self, *sids: str) -> None:
        self.sids = sorted({*self.sids, *[s for s in sids if s]}, key=len, reverse=True)

    def __call__(self, text: object) -> str:
        out = str(text)
        for sid in self.sids:
            out = re.sub(re.escape(sid), "<SID>", out, flags=re.IGNORECASE)
        if self.home:
            parts = [p for p in re.split(r"[\\/]+", self.home) if p]
            if parts:
                out = re.sub(r"[\\/]+".join(re.escape(p) for p in parts), "<家目录>", out, flags=re.IGNORECASE)
        for name in self.names:
            out = re.sub(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", "<名字>", out,
                         flags=re.IGNORECASE)
        return out

    def leaks(self, text: str) -> list[str]:
        """抹完以后再扫一遍: 还找得到什么 (找得到说明抹漏了). 返回种类, 不返回内容."""
        found = []
        low = re.sub(r"\\+", r"\\", text.lower())  # json 里反斜杠是成对的, 先并成一个再比
        if self.home and self.home.lower() in low:
            found.append("家目录")
        for name in self.names:
            if re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", low):
                found.append("用户名或电脑名")
                break
        for sid in self.sids:
            if sid.lower() in low:
                found.append("SID")
                break
        return found


# ---------------------------------------------------------------- 路径和布局
@dataclass(frozen=True)
class Layout:
    root: Path
    work: Path
    home: Path       # 沙箱自己的文件夹
    pyrt: Path       # 拷来的 Python 运行时
    probe: Path      # 探针脚本的拷贝
    decoy_dir: Path
    decoy_file: Path
    results: Path
    marker: Path


def make_layout(root: Path) -> Layout:
    work = root / "work"
    return Layout(root=root, work=work, home=work / "home", pyrt=work / "pyrt", probe=work / "probe",
                  decoy_dir=work / "decoy", decoy_file=work / "decoy" / "secret.txt", results=root / "results",
                  marker=work / MARKER_NAME)


def show_path(path: Path | str, root: Path, rd: Redactor) -> str:
    """显示路径: 在原型目录下的写相对路径, 别的过一遍抹除."""
    p = Path(path)
    try:
        return str(p.relative_to(root))
    except ValueError:
        return rd(str(p))


def minimal_env(home: Path) -> dict[str, str]:
    """探针的环境变量自己造一份最小的, 不继承: 系统目录、只含系统目录的 PATH、指到沙箱自己文件夹的 TEMP 和 TMP."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or "C:\\Windows"
    return {"SystemRoot": root, "windir": root, "PATH": root + "\\System32;" + root, "TEMP": str(home), "TMP": str(home)}


def probe_command(layout: Layout) -> tuple[str, list[str]]:
    """起探针的程序和参数: 拷来的 python.exe, -I -B -S (隔离模式、不写字节码、不导入 site) 加探针脚本."""
    return str(layout.pyrt / "python.exe"), ["-I", "-B", "-S", str(layout.probe / "child_probe.py")]


# ---------------------------------------------------------------- 拷运行时
RUNTIME_SKIP_DIRS = {"site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip", "turtledemo"}


def plan_runtime_copy(src: Path) -> list[tuple[Path, Path]]:
    """要拷的 (源文件, 相对路径): python.exe 和同目录的 DLL、DLLs\\ 里的 .pyd/.dll、Lib\\ 里除了
    site-packages、测试、缓存、idlelib、tkinter、ensurepip (和不装 tkinter 用的 turtledemo) 以外的全部文件."""
    files: list[tuple[Path, Path]] = []
    for entry in sorted(src.iterdir()):
        if entry.is_file() and (entry.name.lower() == "python.exe" or entry.suffix.lower() == ".dll"):
            files.append((entry, Path(entry.name)))
    dlls = src / "DLLs"
    if dlls.is_dir():
        for entry in sorted(dlls.iterdir()):
            if entry.is_file() and entry.suffix.lower() in (".pyd", ".dll"):
                files.append((entry, Path("DLLs") / entry.name))
    lib = src / "Lib"
    if lib.is_dir():
        for folder, dirs, names in os.walk(lib):
            dirs[:] = sorted(d for d in dirs if d not in RUNTIME_SKIP_DIRS)
            for name in sorted(names):
                if name.lower().endswith((".pyc", ".pyo")):
                    continue
                path = Path(folder) / name
                files.append((path, path.relative_to(src)))
    return files


def copy_runtime(plan: list[tuple[Path, Path]], dst: Path) -> tuple[int, int]:
    """按计划拷, 返回 (文件数, 字节数)."""
    made: set[Path] = set()
    total = 0
    for source, rel in plan:
        target = dst / rel
        if target.parent not in made:
            target.parent.mkdir(parents=True, exist_ok=True)
            made.add(target.parent)
        shutil.copy2(source, target)
        total += target.stat().st_size
    return len(plan), total


# ---------------------------------------------------------------- 提权那一条: 命令、结果文件
SAFE_PATH_RE = re.compile(r"[A-Za-z0-9\\:_.\-]+")


def path_is_safe(path: str) -> bool:
    """生成提权命令前的检查: 路径里只许字母、数字、反斜杠、冒号、下划线、横杠、点."""
    return bool(path) and SAFE_PATH_RE.fullmatch(path) is not None


def build_elevate_command(results_dir: str, mode_id: str, system_root: str) -> tuple[dict | None, str]:
    """生成"以管理员身份运行"要起的系统命令行. 它只做一件事: 看自己的完整性级别是不是高 (whoami /groups 的输出里
    有没有 S-1-16-12288, 输出直接管道给 findstr、不落盘), 把 ELEVATED 或 NOT_ELEVATED 一个词写进
    <结果目录>\\elev_<关法>.txt, 然后退出. 路径里有不许的字符就不生成, 返回 (None, 原因).
    """
    for label, path in (("结果目录", results_dir), ("系统目录", system_root)):
        if not path_is_safe(path):
            return None, f"{label}的路径里有字母、数字、\\、:、_、-、. 以外的字符，不试提权"
    if not re.fullmatch(r"[a-z_]+", mode_id):
        return None, "关法的编号不合规"
    sys32 = system_root + "\\System32"
    out_file = results_dir + "\\elev_" + mode_id + ".txt"
    inner = (f"{sys32}\\whoami.exe /groups | {sys32}\\findstr.exe /c:S-1-16-12288 >nul"
             f" && (>{out_file} echo ELEVATED) || (>{out_file} echo NOT_ELEVATED)")
    return {"file": sys32 + "\\cmd.exe", "params": f'/d /s /c "{inner}"', "dir": sys32,
            "result": out_file, "name": "elev_" + mode_id + ".txt"}, ""


def wants_elevation_test(answer: str) -> bool:
    """对“提权那一条真试吗”的回答: 只有 y (大小写都行, 前后空白不算) 才试; 直接回车和别的一律不试."""
    return answer.strip().lower() == "y"


def read_elev_word(path: str) -> str | None:
    """结果文件里的那个词: 'ELEVATED' / 'NOT_ELEVATED' / 'OTHER' (别的内容) / None (没有文件, 或者还是空的)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(64)
    except OSError:
        return None
    if not raw.strip():
        return None
    word = raw.decode("ascii", "replace").strip()
    return word if word in ("ELEVATED", "NOT_ELEVATED") else "OTHER"


# ---------------------------------------------------------------- 一样一样的结果
@dataclass
class Item:
    state: str
    code: int | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "code": self.code, "note": self.note}


def fmt_code(code: int | None) -> str:
    if code is None:
        return ""
    return str(code) if code < 0x10000 else f"0x{code:X}"


def item_cell(item: dict) -> str:
    text = STATE_TEXT.get(item.get("state", ""), "?")
    if item.get("code") is not None and item.get("state") in (CANNOT, ERROR):
        text += f"({fmt_code(item['code'])})"
    return text


def err_code(reply: dict) -> int | None:
    winerror, errno = reply.get("winerror"), reply.get("errno")
    return winerror if winerror is not None else errno


def is_denied(reply: dict) -> bool:
    return (reply.get("err") == "PermissionError" or reply.get("winerror") in (5, 10013)
            or reply.get("errno") in (13, 10013))


def is_not_found(reply: dict) -> bool:
    return reply.get("err") == "FileNotFoundError" or reply.get("winerror") in (2, 3)


def classify_read(reply: dict | None, err: str | None) -> Item:
    """读假文件. 读到了 = 做得到 (漏了); 被拒 = 做不到."""
    if err or reply is None:
        return Item(ERROR, note=f"探针没回话 ({err})")
    if reply.get("ok"):
        return Item(CAN) if reply.get("eq") else Item(ERROR, note="读到了，但内容和假文件对不上")
    code = err_code(reply)
    if is_denied(reply):
        return Item(CANNOT, code, "拒绝访问")
    if is_not_found(reply):
        return Item(CANNOT, code, "探针里看不到这个文件 (文件是在的, 测量脚本核对过)")
    return Item(ERROR, code, str(reply.get("err")))


def classify_write(reply: dict | None, err: str | None, exists_after: bool | None) -> Item:
    """往某处写一个小文件. exists_after 是测量脚本自己去看文件在不在 (不全听探针的)."""
    if err or reply is None:
        return Item(ERROR, note=f"探针没回话 ({err})")
    if reply.get("ok"):
        if exists_after is False:
            return Item(ERROR, note="探针说写成了，但文件不在")
        return Item(CAN)
    code = err_code(reply)
    if exists_after:
        return Item(ERROR, code, "探针说没写成，但文件在")
    if is_denied(reply):
        return Item(CANNOT, code, "拒绝访问")
    if is_not_found(reply):
        return Item(CANNOT, code, "探针里看不到这个位置")
    return Item(ERROR, code, str(reply.get("err")))


def classify_connect(reply: dict | None, err: str | None, accepted_delta: int) -> Item:
    """连测量脚本在 127.0.0.1 上开的测试端口. accepted_delta 是测试端口这边多看到几次连接."""
    if err or reply is None:
        return Item(ERROR, note=f"探针没回话 ({err})")
    if reply.get("ok"):
        if accepted_delta < 1:
            return Item(ERROR, note="探针说连上了，但测试端口这边没看到连接")
        return Item(CAN)
    code = err_code(reply)
    name = str(reply.get("err"))
    if is_denied(reply) or name in ("timeout", "TimeoutError", "ConnectionRefusedError", "ConnectionResetError") \
            or code in (10013, 10060, 10061):
        return Item(CANNOT, code, name)
    return Item(ERROR, code, name)


def classify_elevate(reply: dict | None, err: str | None, word: str | None) -> Item:
    """提权那一条. 看结果文件里的词, 没有文件再看请求自己的返回值."""
    if word == "ELEVATED":
        return Item(CAN, note="拿到了管理员权限 (结果文件写着 ELEVATED)")
    if word == "NOT_ELEVATED":
        return Item(CANNOT, note="起了新进程，但不是管理员权限 (NOT_ELEVATED)")
    if word == "OTHER":
        return Item(ERROR, note="结果文件里不是预期的词")
    if err or reply is None:
        return Item(ERROR, note=f"探针没回话 ({err})")
    if not reply.get("ok"):
        return Item(ERROR, err_code(reply), "探针里调用出错: " + str(reply.get("err")))
    if reply.get("ret") == 0:
        return Item(CANNOT, reply.get("err"), "要不到 (请求被拒)")
    return Item(ERROR, note=f"请求返回了成功，但 {ELEV_WAIT_S:.0f} 秒内没见到结果文件")


def mode_verdict(mode_id: str, items: dict[str, Item], elevation_tried: bool = True, diagnostic: bool = False) -> dict:
    """一行的结论: 对照是不是正常, 其余三种能不能用. 用户选了不试提权时, 对照那行不因为这一条没验就算不正常;
    其余三行照样写"还不能下结论 (要管理员权限 没验出来)" -- 说明页说过, 不试的代价就是这样."""
    control = mode_id == "control"
    problems: list[str] = []
    undetermined: list[str] = []
    for key in ITEM_KEYS:
        if control and key == "elevate" and not elevation_tried:
            continue
        want = CAN if control else EXPECT[key]
        item = items.get(key)
        got = item.state if item else UNKNOWN
        if got == want:
            continue
        if got in (CAN, CANNOT):  # 和预期相反
            problems.append(f"对照里「{ITEM_TITLES[key]}」没成" if control else PROBLEM_TEXT[(key, got)])
        else:
            undetermined.append(ITEM_TITLES[key])
    if control:
        if problems:
            text, ok = "对照有毛病, 先修探针: " + "；".join(problems), False
        elif undetermined:
            text, ok = "对照有几项没验出来: " + "、".join(undetermined), None
        else:
            text, ok = "对照正常: 这几样平时都做得到", True
    elif items.get("python") and items["python"].state == CANNOT:
        text, ok = "Python 没起来, 其余各样都没验: 这一行不能当成“已隔离”, 也不能当成“不行”", False
    elif problems:
        text, ok = "不能用: " + "；".join(problems), False
    elif undetermined:
        text, ok = "还不能下结论 (" + "、".join(undetermined) + " 没验出来)", None
    else:
        text, ok = "能用", True
    if diagnostic and not control:  # 只有诊断配法起来了: 它量到的另放 (diagnostic_items), 这一行照实写规格的结果
        if ok is True:  # 保险: 诊断配法起来的行永远不会是“能用”
            ok = None
        text += "（只有诊断配法起来了，它量到的另放在“诊断配法量到的”里，不算数）"
    return {"ok": ok, "text": text, "problems": problems, "undetermined": undetermined}


def finalize_elevation(modes: list[dict], tried: bool) -> dict:
    """全部跑完以后给"要管理员权限"这一列下结论. 对照没写 ELEVATED 就是测法不成立, 其余三行不许写"要不到"."""
    if not tried:
        return {"tried": False, "conclusion": "没验 (只读了令牌信息)", "valid": None}
    attempted = (CAN, CANNOT, ERROR)  # 真发出过请求的才有这几种状态; UNKNOWN 是没试成 (路径字符不合规、Python 没起来)
    if not any(m.get("items", {}).get("elevate", {}).get("state") in attempted for m in modes):
        why = sorted({m["items"]["elevate"].get("note", "") for m in modes if m.get("items", {}).get("elevate")})
        return {"tried": True, "valid": None, "conclusion": "没验 (选了真试，但一次都没试成: " + "；".join(w for w in why if w) + ")"}
    control = next((m for m in modes if m["id"] == "control"), None)
    control_item = (control or {}).get("items", {}).get("elevate", {})
    if control_item.get("state") != CAN:
        for mode in modes:
            item = mode.get("items", {}).get("elevate")
            if item and mode["id"] != "control" and item.get("state") in attempted:
                mode["elevate_raw"] = dict(item)  # 原来量到的留着, 表里改写成"测法不成立"
                item["state"] = INVALID
                item["note"] = "对照没写 ELEVATED，测法不成立"
        return {"tried": True, "conclusion": "测法不成立 (对照没写 ELEVATED)", "valid": False}
    # 诊断配法 (比如没降完整性级别的) 要到了管理员权限不代表这种关法拿得到: 不算进“有关法要得到了”
    got = [m["label"] for m in modes if m["id"] != "control" and not m.get("diagnostic")
           and m.get("items", {}).get("elevate", {}).get("state") == CAN]
    if got:
        return {"tried": True, "valid": True, "conclusion": "有关法要得到了管理员权限: " + "、".join(got), "leaks": got}
    sandbox = [m for m in modes if m["id"] != "control"]
    ran = [m["label"] for m in sandbox
           if m.get("items", {}).get("elevate", {}).get("state") in attempted and not m.get("diagnostic")]
    not_ran = [m["label"] for m in sandbox if m["label"] not in ran]
    if not ran:  # 11-16 真机就是这样: 三种关法的 Python 都没起来, 只有对照试了 -- 不能写“要不到”
        diag = [m["label"] for m in sandbox if m.get("diagnostic")]
        extra = ("（" + "、".join(diag) + " 只有诊断配法起来了，不算数，也没发提权请求）") if diag else ""
        return {"tried": True, "valid": None,
                "conclusion": "没验 (三种关法里没有一种规格里的配法把 Python 起起来, 只有对照试了提权; "
                              "对照写了 ELEVATED, 说明测法本身可用)" + extra}
    if not_ran:
        return {"tried": True, "valid": True,
                "conclusion": "测法成立 (对照写了 ELEVATED); " + "、".join(ran) + " 要不到; " + "、".join(not_ran)
                + " 没验 (规格里的配法没把 Python 起起来)"}
    return {"tried": True, "valid": True, "conclusion": "测法成立 (对照写了 ELEVATED); 三种关法都要不到"}


# ---------------------------------------------------------------- 起探针的配法 (一种关法按顺序试, 第一个起来的算数)
@dataclass(frozen=True)
class Variant:
    name: str
    register: bool = False        # 应用容器: 先登记再起
    restrict: tuple = ()          # 受限令牌: 限制 SID 的名字 ("tok" 沙箱令牌 SID, "logon" 登录 SID, 或 SID 字符串)
    env_ext: bool = False         # 环境变量在最小的五个以外再加几个 (应用容器起进程时系统要在环境里找它们)
    env_inherit: bool = False     # 诊断: 让子进程继承本进程的环境 (只在诊断配法里)
    low: bool = True              # 令牌的完整性级别设成低 (False 只在诊断配法里)
    plain: bool = False           # 诊断: 令牌不去特权 (flags=0)、不改组, 几乎和自己的一样 (只在诊断配法里)
    diagnostic: bool = False      # 诊断配法: 为了查清为什么规格里的配法起不来; 它起来了也不算规格里的结果, 也不发提权请求


VARIANTS = {
    "appcontainer": [
        Variant("只按名字算出 SID, 没登记"),
        Variant("只按名字算出 SID, 没登记, 环境里多几个变量", env_ext=True),
        Variant("登记后再起", register=True),
        Variant("登记后再起, 环境里多几个变量", register=True, env_ext=True),
        Variant("诊断: 登记后再起, 环境让子进程继承 (查报 203 是不是缺环境变量)", register=True, env_inherit=True,
                diagnostic=True),
    ],
    "restricted_downgrade": [
        Variant("只降级, 不加限制 SID"),
        Variant("诊断: 只降级, 但完整性级别不降 (中)", low=False, diagnostic=True),
        Variant("诊断: 令牌几乎和自己的一样 (不去特权、不改组、完整性级别不降), 只是起进程走“用令牌起进程”那个系统接口",
                low=False, plain=True, diagnostic=True),
    ],
    "restricted_sids": [
        Variant("严: 限制 SID = 沙箱令牌 SID + ALL APPLICATION PACKAGES", restrict=("tok", sw.SID_ALL_APP_PACKAGES)),
        Variant("松: 限制 SID = 沙箱令牌 SID + Users + Everyone + RESTRICTED + 登录 SID",
                restrict=("tok", sw.SID_USERS, sw.SID_EVERYONE, sw.SID_RESTRICTED, "logon")),
        Variant("诊断: 松, 但完整性级别不降 (中)",
                restrict=("tok", sw.SID_USERS, sw.SID_EVERYONE, sw.SID_RESTRICTED, "logon"), low=False, diagnostic=True),
    ],
    "control": [Variant("不关")],
}

EXTENDED_ENV_NAMES = ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramData", "SystemDrive")


def extended_env(env: dict[str, str]) -> dict[str, str]:
    """最小环境再加这几个 (值取自本进程自己的环境): 应用容器起进程时系统会给它安排 TEMP 等变量, 找不到这些就报 203."""
    out = dict(env)
    for name in EXTENDED_ENV_NAMES:
        if os.environ.get(name):
            out[name] = os.environ[name]
    return out


def resolve_restrict_sids(names: tuple, token_sid: bytes, logon_sid: bytes | None) -> list[bytes]:
    out: list[bytes] = []
    for name in names:
        if name == "tok":
            out.append(token_sid)
        elif name == "logon":
            if logon_sid:
                out.append(logon_sid)
        else:
            out.append(sw.sid_from_string(name))
    return out


SDDL_ALIASES = {"SY": "SYSTEM", "BA": "Administrators", "BU": "Users", "WD": "Everyone", "AU": "Authenticated Users",
                "AC": "ALL APPLICATION PACKAGES", "RC": "RESTRICTED", "CO": "CREATOR OWNER", "OW": "OWNER RIGHTS",
                "IU": "INTERACTIVE", "LS": "LOCAL SERVICE", "NS": "NETWORK SERVICE", "PU": "Power Users",
                "BG": "Guests", "AN": "ANONYMOUS", "S-1-15-2-2": "ALL RESTRICTED APPLICATION PACKAGES"}


def sddl_principals(sddl: str, user_sid: str | None = None, pkg_sid: str | None = None,
                    token_sid: str | None = None) -> list[str]:
    """从 SDDL 里取出每条访问控制项是给谁的 (去重), 把本人、沙箱 SID 换成名字, 别的 S-1-5-21 账号和登录会话抹成泛称."""
    names: list[str] = []
    for ace in re.findall(r"\(([^()]*)\)", sddl):
        parts = ace.split(";")
        if len(parts) < 6:
            continue
        who = parts[5].strip()
        if user_sid and who.upper() == user_sid.upper():
            who = "<本人>"
        elif pkg_sid and who.upper() == pkg_sid.upper():
            who = "<沙箱SID>"
        elif token_sid and who.upper() == token_sid.upper():
            who = "<沙箱令牌SID>"
        elif who.upper().startswith("S-1-5-21-"):
            who = "<其他账号或组>"
        elif who.upper().startswith("S-1-5-5-"):
            who = "<登录会话>"
        else:
            who = SDDL_ALIASES.get(who.upper(), who)
        if who not in names:
            names.append(who)
    return names


# ---------------------------------------------------------------- 测试端口
class Listener:
    """测量脚本自己在 127.0.0.1 上开的随机端口. 只绑回环地址 (绑别的地址会弹防火墙的框); 探针连一下就关."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


# ---------------------------------------------------------------- 报告
def render_report_md(report: dict) -> str:
    """report.md: 一张表 (每种关法一行, 每样一列) 加几节说明. 缺哪一块就跳过哪一块, 中途出错也能渲染."""
    env = report.get("environment", {})
    lines = ["# 隔离原型测量结果", ""]
    lines.append(f"- 时间：{report.get('started_at', '?')}（本机时区）；退出码 {report.get('exit_code', '?')}")
    lines.append(f"- Python：{env.get('python', '?')}（{env.get('bits', '?')} 位）；Windows 版本号：{env.get('windows', '?')}")
    uac = report.get("uac_settings") or {}
    lines.append("- 提权确认三项设置（只读）：" + "，".join(f"{k}={uac.get(k)}" for k in UAC_VALUES))
    elev = report.get("elevation") or {}
    lines.append(f"- 提权那一条：用户选了{'真试' if elev.get('tried') else '不试'}；结论：{elev.get('conclusion', '?')}")
    app = report.get("appcontainer") or {}
    lines.append(f"- 应用容器：{app.get('summary', '没用到')}")
    lines += ["", "## 结果表", ""]
    head = ["关法"] + [ITEM_TITLES[k] for k in ITEM_KEYS] + ["起一次(ms)", "结论"]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "|".join(["---"] * len(head)) + "|")
    for mode in report.get("modes", []):
        cells = [item_cell(mode.get("items", {}).get(k, {"state": UNKNOWN})) for k in ITEM_KEYS]
        start = mode.get("start_ms")
        lines.append("| " + " | ".join([mode.get("label", "?")] + cells
                                       + ["" if start is None else str(round(start)),
                                          (mode.get("verdict") or {}).get("text", "?")]) + " |")
    lines += ["", "读、写外面、连、要管理员权限这几列，“做不到”才是想要的；“做得到”说明漏了。"
              "Python 起得来和写自己文件夹这两列反过来。对照那一行应当全是“做得到”。", ""]
    lines += ["## 每种关法的细节", ""]
    for mode in report.get("modes", []):
        lines.append(f"### {mode.get('label', '?')}")
        lines.append(f"- 最后用的配置：{mode.get('variant') or '（没起成功）'}")
        for t in mode.get("tries", []):
            lines.append("- 试过：" + "；".join(f"{k}={v}" for k, v in t.items()))
        if mode.get("diagnostic_items"):
            dit = mode["diagnostic_items"]
            lines.append(f"- **诊断配法量到的（不算数，只当线索；它不发提权请求）**：配法「{mode.get('variant')}」，起一次 "
                         f"{mode.get('diagnostic_start_ms')} ms；" + "；".join(f"{ITEM_TITLES[k]}={item_cell(dit[k])}"
                                                                          for k in ITEM_KEYS if k in dit))
        if mode.get("token"):
            lines.append("- 探针读到的令牌信息：" + "，".join(f"{k}={v}" for k, v in mode["token"].items()))
        for key in ITEM_KEYS:
            item = (mode.get("items") or {}).get(key)
            if item and item.get("note"):
                lines.append(f"- {ITEM_TITLES[key]}：{item_cell(item)} -- {item['note']}")
        if mode.get("timeout"):
            lines.append("- 这个探针超过 20 秒，作业被结束，记成超时")
        if mode.get("close"):
            lines.append("- 收尾时核对：" + "，".join(f"{k}={v}" for k, v in mode["close"].items()))
        lines.append("")
    lines += ["## 提权那一条", ""]
    for mode in report.get("modes", []):
        detail = mode.get("elevate_detail")
        if detail:
            lines.append(f"- {mode.get('label', '?')}：" + "，".join(f"{k}={v}" for k, v in detail.items()))
    if elev.get("command"):
        lines.append(f"- 生成的命令（探针原样用）：`{elev['command']}`")
    lines += ["", "## 每一步用时", "", "| 步 | 名称 | 毫秒 |", "|---|---|---|"]
    for step in report.get("steps", []):
        lines.append(f"| {step.get('n')} | {step.get('name')} | {step.get('ms')} |")
    lines += ["", "## 权限里有谁（只读核对，已抹掉本人和别的账号）", ""]
    for key, names in (report.get("acl") or {}).items():
        lines.append(f"- {key}：{'、'.join(names) if isinstance(names, list) else names}")
    lines += ["", "## 收尾核对", ""]
    for key, value in (report.get("cleanup") or {}).items():
        lines.append(f"- {key}：{value}")
    if report.get("errors"):
        lines += ["", "## 中途出的错", ""] + [f"- {e}" for e in report["errors"]]
    lines += ["", f"个人信息扫描：{(report.get('pii_scan') or {}).get('result', '未扫')}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- --plan
VARIANT_PLAN_PREFIX = "    · 配法 "


def variant_plan_lines() -> list[str]:
    """--plan 第 4 步里的配法清单: VARIANTS 里每一种一行 (测试守着“脚本里有几种，这里就列几种”), 再用大白话说清多出来的事."""
    lines: list[str] = []
    for mode_id, label in MODES:
        for i, variant in enumerate(VARIANTS[mode_id], 1):
            tag = "【诊断，不算结果】" if variant.diagnostic else ""
            lines.append(f"{VARIANT_PLAN_PREFIX}{label} 第 {i} 种：{tag}{variant.name}")
    total = sum(len(v) for v in VARIANTS.values())
    lines += [
        f"  · 配法是这样用的：每种关法从第 1 种开始试，没起来或者起来了不回话，就结束它、换下一种；不会同时起几种。"
        f"所以整次最多起 {total} 个 Python（应用容器最多 {len(VARIANTS['appcontainer'])} 个，受限令牌只降级最多 "
        f"{len(VARIANTS['restricted_downgrade'])} 个，加限制 SID 最多 {len(VARIANTS['restricted_sids'])} 个，对照 1 个），"
        "一般少得多：第一个回了话的就停。",
        "    试不成的配法，可能是几毫秒就退出的进程，也可能起来了不回话、卡住最多 15 秒后被结束；起进程时遇到“文件被占着”"
        "（比如杀毒软件在扫）会等 1 秒再试一次。所以整次正常 1 到 3 分钟（不含你回答“真试吗”的时间），最坏约 5 分钟。",
        "  · “多几个变量”的配法（应用容器第 2、4 种）会把你真实的这 5 个环境变量交给沙箱里的进程："
        + "、".join(EXTENDED_ENV_NAMES) + "。值是你本人的真路径（带着你的用户名），只交给那个子进程，不写进结果。",
        "  · 应用容器最后一种诊断配法让子进程继承本脚本的整份环境变量（你的环境里可能有密钥）。探针不读、不回环境变量，"
        "结果文件和窗口里不写任何环境变量的值，这种配法起来了不回话时也不记它的 stderr 原文，只记长度。",
        "  · 标“诊断”的配法只在它前面的配法都起不来时才会试，目的是查清为什么起不来。它起来了，探针照常做下面那几样"
        "（读、写、连端口），但诊断配法不发提权请求，那一格记“没验”；它量到的另放在“诊断配法量到的”里，"
        "表格那一行照实写“Python 做不到，其余没验”，窗口里逐项打印、第 5 步也都标“诊断，不算数”。",
        "  · “只降级”的第 3 种诊断配法造的令牌几乎和你自己的一样（不去特权、不改组、完整性级别不降），只是起进程走“用令牌起进程”那个系统接口（对照走的是普通起进程那个）；"
        "加限制 SID 的诊断配法是“松，但完整性级别不降”。它们只用来分清卡在哪一级，不算结果。",
        "  · 只对令牌那两种关法：进程创建了却没回话时，开一个临时线程换成同一个令牌的身份，对一份固定名单"
        "（运行时里的 python.exe 等几个文件、探针、沙箱自己的文件夹、System32 里 4 个 DLL）各只读打开一次，"
        "只记相对名字和错误码，然后换回来、线程退出；先核对线程令牌确实到了“模拟”级别，没到就记“诊断没做成”。不改任何文件和权限。",
        "  · 对除对照以外的关法：任何一种配法没起来，或者创建了进程却没回话，就用同一个身份、环境、工作目录、作业对象，"
        "再起一次系统自带的 cmd.exe /d /c exit 7，只看退出码是不是 7（造令牌就失败的不做）；它几毫秒就退出，不写文件。",
        "  · 受限令牌加限制 SID 造令牌时报 87（参数不对）才会对每个限制 SID 单独造一次令牌，再用整组、不改组造一次，"
        f"只造令牌、不起进程、立刻关；一种配法最多额外造 {max(len(v.restrict) for v in VARIANTS['restricted_sids']) + 1} 个。",
    ]
    return lines


def plan_lines(layout: Layout, runtime_src: Path, temp_root: Path, rd: Redactor) -> list[str]:
    """六步要做的事, 带实际会用的路径. 只拼字符串, 不碰系统."""
    w = lambda p: show_path(p, layout.root, rd)  # noqa: E731
    box = rd(str(temp_root / (TEMP_PREFIX + "<随机8位>") / FAKE_BOX_NAME))
    return [
        "隔离原型 · 计划（--plan：只打印；不建目录、不改任何权限、不碰注册表、不起你能看见的程序。"
        "它只读几个环境变量和本机名，用来在输出里把你的用户名、家目录抹掉，别的什么都不做）",
        f"原型目录：{rd(str(layout.root))}",
        "",
        "第 1 步  自查",
        "  · 查这个窗口是不是管理员权限（令牌是否提权）。是，或者查不出来，或者令牌没提权但完整性级别是高，"
        "就打一句说明、退出码 3，什么都不做、什么都不建。",
        "  · 找不到可以拷的 Python 运行时（python.exe、Lib\\）就退出码 2，同样什么都还没建。",
        "  · 只读记下提权确认的三项设置（注册表 HKLM\\" + UAC_KEY + " 里的 " + "、".join(UAC_VALUES) + "），"
        "再记 Python 和 Windows 的版本号。",
        "",
        "第 2 步  建东西（都在运行时建，现在一样都还没有）",
        f"  · 开头先看 {w(layout.work)}\\ 在不在：上次没收尾留下的，里面有标记文件 .wm_isolation_work.json 的，"
        "先清掉（它记着的 %TEMP%\\wm_iso_* 临时文件夹、登记的沙箱身份、work\\ 本身）；有 work\\ 却没有标记文件就不敢删，直接停下（退出码 2）。",
        f"  · {w(layout.results)}\\<年月日-时分秒>\\    放结果；提权那一条的结果文件也写在这里",
        f"  · {w(layout.work)}\\.wm_isolation_work.json    标记文件（记着临时文件夹在哪、登记没登记；万一没收尾，下次靠它清）",
        f"  · {w(layout.home)}\\    沙箱自己的文件夹",
        f"  · {w(layout.pyrt)}\\    把正在用的 Python 运行时拷一份（python.exe、同目录的 DLL、DLLs\\、Lib\\；"
        f"不带 site-packages、测试、缓存、idlelib、tkinter、ensurepip；约 28 MB；从 {rd(str(runtime_src))} 拷，不下载）",
        f"  · {w(layout.probe)}\\child_probe.py    探针的一份拷贝",
        f"  · {w(layout.decoy_file)}    原型目录里的假文件（一行假字）",
        f"  · {box}    假保管箱（一行假字；文件夹的权限只有本人、SYSTEM、Administrators，和家目录下默认的一样）",
        "",
        "第 3 步  登记沙箱身份、加权限",
        f"  · 应用容器：先只按名字 {APPC_NAME} 算出 SID、不登记；起不来才登记（会在你的用户注册表里写一项，"
        "在 AppData\\Local\\Packages 下建一个空文件夹），用了哪种写进结果。登记表里原来就有这个名字的（上次没收尾留下的），"
        "收尾时也一并删。",
        f"  · 只给沙箱身份这几样权限：读 {w(layout.pyrt)}\\ 和 {w(layout.probe)}\\，读写 {w(layout.home)}\\。"
        "别的目录的权限一律不动。",
        "  · “沙箱身份”有两个：应用容器那个，和受限令牌用的一个按名字编出来的 SID（长得像 S-1-5-21-…-1337，不对应任何真账号）。"
        "上面这几样权限两个都加，都只加在 work 下这几个文件夹上，收尾删 work 时随文件夹一起没了。",
        f"  · 改“低”完整性标签要多一项权限，所以一开始就给本人对 {w(layout.home)}\\ 加完全控制（这个文件夹随 work 一起删），"
        "再给它打一个“低”完整性标签，低完整性的进程才写得进去。",
        "  · 只读核对（不改东西）：注册表里（HKCU 下）有没有这个应用容器的登记项；拷进去的文件有没有继承到权限。",
        "",
        "第 4 步  起探针（对照不关、应用容器、受限令牌只降级、受限令牌加限制 SID；对照第一个跑；"
        "每种关法按下面列的配法顺序试，第一个把 Python 起起来的算数）",
        *variant_plan_lines(),
        "  · 开头先问你一次：「" + ELEV_QUESTION.rstrip("：") + "」（默认不试）。再在 127.0.0.1 上开一个随机端口（只绑本机回环），自己先连一次。",
        "  · 每个探针：环境变量自己造一份最小的（SystemRoot、windir、只含系统目录的 PATH、指到沙箱自己文件夹的 TEMP 和 TMP；"
        "上面写了“多几个变量”和“继承”的配法除外）；"
        "Python 用 -I -B -S 起；放进作业对象（句柄全关掉时结束里面所有进程，不许脱离）；只继承标准输入输出三个句柄。"
        "每个探针从创建起约 20 秒的期限，到时间就结束作业，记成超时（提权那一步卡住时可能多几秒）。",
        "  · 探针做的事：回答一句问话；读读假保管箱和假文件；往 work\\ 和临时文件夹里各写一个小文件（写进了收尾时也会删），"
        "再往自己文件夹里写一个；连测量脚本自己在 127.0.0.1 上临时开的测试端口（不绑别的地址）；读自己令牌的几项信息。",
        "",
        "第 5 步  提权那一条（第 4 步里问了真试才做；不试就只读令牌信息，这一条记“没验”）",
        "  · 只对 Python 起来了的关法做：探针用 Windows 标准的方式请求“以管理员身份运行”一个系统自带的命令行"
        "（%SystemRoot%\\System32\\cmd.exe），只这一种请求。对照一定做，规格里 Python 起来了的关法各做一次，"
        "所以最多 4 次；Python 没起来的关法、诊断配法都不发，那一格写“没验”。",
        "  · 那条命令只做一件事：看自己的完整性级别是不是高，把 ELEVATED 或 NOT_ELEVATED 一个词写进 results\\<这次>\\elev_<关法>.txt。"
        "批准的话，它会起 cmd.exe，再由它起 whoami.exe 和 findstr.exe（都是系统自带的，只读自己的组信息，输出不落盘、直接丢掉），"
        "连管道两边的 cmd 约 5 个进程，各自几十毫秒就没了。",
        "  · 对照在这台提权确认关着的电脑上一定会成功，用它证明测法测得出来。",
        "  · 每试一次核对两样：这次试出来的进程已经没有了；结果文件夹里只多了预期的那一个文件（或者没多）。",
        "",
        "第 6 步  收尾（不管前面哪一步出错都会走到）",
        "  · 先把还活着的探针逐个结束，再核对自己起的进程都退了；删掉登记的沙箱身份（这次登记的，或登记表里原来就有的）；"
        "删掉 work\\ 整个目录和临时文件夹里那个子目录；写结果（report.json、report.md，不带个人信息）。最后只留 results\\。",
        "  · 只读核对：登记表里还有没有那一项、AppData\\Local\\Packages 下还有没有它的文件夹、原型目录里有没有多出来的东西。",
        "",
        "不会做的事：碰真的保管箱和家目录下的项目数据、读你别的文件、下载、安装、连外网、改系统设置或防火墙、建 Windows 账号、"
        "动提权确认的设置、改 prototypes\\isolation\\ 以外的任何文件。",
    ]


def print_plan() -> None:
    rd = Redactor.from_env()
    # 临时文件夹直接取环境变量 (和 tempfile 同样先看 TMP 再看 TEMP): tempfile.gettempdir() 第一次会在里面建一个小文件再删
    temp_root = Path(os.environ.get("TMP") or os.environ.get("TEMP") or "C:\\Windows\\Temp")
    for line in plan_lines(make_layout(HERE), Path(sys.base_prefix), temp_root, rd):
        print(line)


# ---------------------------------------------------------------- 运行时
def say(text: str = "") -> None:
    print(text, flush=True)


def setup_console() -> None:
    """窗口的代码页不一定能编出所有字: 编不出的字换成问号, 不要因此崩掉."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_uac_settings() -> dict:
    """提权确认三项设置, 只读."""
    out: dict = {name: None for name in UAC_VALUES}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UAC_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in UAC_VALUES:
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    out[name] = None
    except OSError:
        pass
    return out


EXPECTED_ROOT_NAMES = {"run_me.cmd", "看看会做什么.cmd", "measure.py", "sandbox_win.py", "child_probe.py", "README.md",
                       "tests", "results"}


def unexpected_root_names(names: list[str]) -> list[str]:
    """原型目录里“不该有”的东西: 源文件、tests、results 以外的; 说明文件 (*.md) 和存档的 --plan 输出 (plan-output-*.txt) 不算."""
    return sorted(n for n in names
                  if n not in EXPECTED_ROOT_NAMES and not n.endswith(".md") and not n.startswith("plan-output-"))


def rmtree_robust(path: Path, tries: int = 4) -> str | None:
    """删整个目录 (只读的文件先去掉只读). 返回 None 表示删干净了, 否则是剩下什么的说明."""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for _ in range(tries):
        if not path.exists():
            return None
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return None
        time.sleep(0.5)
    left = sum(len(files) for _, _, files in os.walk(path))
    return f"还剩 {left} 个文件"


@dataclass
class Ctx:
    layout: Layout
    rd: Redactor
    stamp: str
    results_dir: Path
    steps: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    uac: dict = field(default_factory=dict)
    env_info: dict = field(default_factory=dict)
    user_sid: bytes | None = None
    logon_sid: bytes | None = None
    pkg_sid: bytes | None = None
    token_sid: bytes | None = None
    temp_dir: Path | None = None
    fake_file: Path | None = None
    listener: Listener | None = None
    try_elevation: bool = False
    elevation_note: str = ""
    elevate_command: str = ""
    profile_before: bool | None = None
    profile_registered: bool = False   # 这次(或上次残留的)登记, 收尾要删
    modes: list = field(default_factory=list)
    acl: dict = field(default_factory=dict)
    cleanup: dict = field(default_factory=dict)
    elevated_pids: list = field(default_factory=list)
    closed: list = field(default_factory=list)   # 每个起过的探针收尾时的核对 (退出了没有、作业里还剩几个)
    root_extra: list = field(default_factory=list)
    work_created: bool = False   # work\ 是这次自己建的才会在收尾时删; 没标记的老 work\ 我们不敢删, 收尾也不碰
    live: list = field(default_factory=list)     # 起来了、还没结束的探针 (被 Ctrl+C 打断时第 6 步要先逐个结束它们)

    def path(self, p: Path | str) -> str:
        return show_path(p, self.layout.root, self.rd)

    def track(self, child: sw.Child) -> sw.Child:
        """刚起来的探针马上登记为“活着”: 之后不管在哪被打断, 第 6 步都找得到它."""
        self.live.append(child)
        return child

    def close_child(self, child: sw.Child) -> dict:
        """结束一个探针 (含它作业里的一切), 记下核对结果."""
        summary = child.close()  # 重复调返回的是同一个字典, 按身份去重 (内容可能一模一样, 不能按内容去重)
        if not any(summary is seen for seen in self.closed):
            self.closed.append(summary)
        self.live = [c for c in self.live if c is not child]
        return summary

    def save_marker(self) -> None:
        """work\\ 里留一份标记: 万一这次没走到收尾 (关了窗口、断电), 下次能认出哪些是自己留的残留."""
        data = {"stamp": self.stamp, "temp_dir": str(self.temp_dir) if self.temp_dir else None,
                "registered": self.profile_registered, "appcontainer": APPC_NAME}
        try:
            self.layout.marker.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


class EnvProblem(Exception):
    """环境不满足 (不是测量本身出错): 退出码 2."""


class StepTimer:
    def __init__(self, ctx: Ctx, n: int) -> None:
        self.ctx, self.n = ctx, n

    def __enter__(self) -> StepTimer:
        say(f"\n== 第 {self.n} 步：{STEP_NAMES[self.n - 1]} ==")
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_exc) -> None:
        self.ctx.steps.append({"n": self.n, "name": STEP_NAMES[self.n - 1],
                               "ms": round((time.perf_counter() - self.t0) * 1000)})


def handle_leftover(ctx: Ctx) -> str | None:
    """上次没收尾 (work\\ 还在). 有这个原型留的标记就善后 (删记在标记里的临时文件夹、登记的沙箱身份、work\\ 本身),
    没有标记就不敢删. 返回 None 表示可以继续, 否则是不能继续的原因."""
    layout = ctx.layout
    if not layout.work.exists():
        return None
    try:
        info = json.loads(layout.marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{ctx.path(layout.work)} 已经在了，但没有这个原型的标记文件，我不敢删。请确认里面没有要留的东西，手动删掉后重跑"
    say("  发现上次没收尾的残留，先清掉")
    temp = info.get("temp_dir")
    if temp:
        p = Path(temp)
        if p.parent == Path(tempfile.gettempdir()) and p.name.startswith(TEMP_PREFIX) and p.exists():
            left = rmtree_robust(p)
            say(f"    临时文件夹：{'已删' if left is None else left}")
    if info.get("registered"):
        code = sw.appcontainer_unregister(APPC_NAME)
        say(f"    登记的沙箱身份：{'已删' if code == 0 else '没删成, 错误码 ' + fmt_code(code)}")
    left = rmtree_robust(layout.work)
    say(f"    work：{'已删' if left is None else left}")
    return None if left is None else f"上次残留的 work 清不掉（{left}）"


def describe_token(info: dict) -> dict:
    """探针读到的令牌信息翻成人话 (提权类型 1 默认 2 完整 3 受限; 完整性级别 0x1000 低 0x2000 中 0x3000 高 0x4000 系统)."""
    level = {0x1000: "低", 0x2000: "中", 0x3000: "高", 0x4000: "系统"}.get(info.get("il"), info.get("il"))
    elev_type = {1: "默认", 2: "完整(提权后)", 3: "受限(未提权)"}.get(info.get("elev_type"), info.get("elev_type"))
    yes = {1: "是", 0: "否"}
    return {"是不是提权的": yes.get(info.get("elevated"), info.get("elevated")), "提权类型": elev_type,
            "完整性级别": level, "是不是应用容器": yes.get(info.get("appcontainer"), info.get("appcontainer")),
            "有没有限制SID(受限令牌)": yes.get(info.get("restricted"), info.get("restricted"))}


def process_gone_state(pid: int | None, waited: str | None, alive: bool | None, word: str | None) -> bool | None:
    """提权请求试出来的进程现在还在不在. True: 已经没有了; False: 还在; None: 没法确认.
    没有进程编号时: 结果文件也没出现 = 根本没起进程 (算已经没有); 文件出现了却没有编号 = 没法确认."""
    if not pid:
        return True if word is None else None
    if waited == "exited" or alive is False:
        return True
    if alive is True:
        return False
    return None


def step1(ctx: Ctx) -> int | None:
    """自查. 返回 None 表示通过; 返回数字是该退出的退出码."""
    with StepTimer(ctx, 1):
        elevated = sw.is_elevated()
        rid = sw.own_integrity_rid()
        if elevated is not False or (rid is not None and rid >= 0x3000):
            if elevated is None:
                say("  查不出这个窗口是不是管理员权限，按管理员处理。")
            else:
                say("  这个窗口是管理员权限。")
            say("  隔离要在普通权限下量才有意义，所以我什么都没做。")
            say("  请关掉这个窗口，从普通的文件夹窗口双击 run_me.cmd（不要从管理员的终端里起）。")
            return 3
        say("  这个窗口不是管理员权限：通过。")
        ctx.uac = read_uac_settings()
        say("  提权确认三项设置（只读）：" + "，".join(f"{k}={ctx.uac.get(k)}" for k in UAC_VALUES))
        v = sys.getwindowsversion()
        ctx.env_info = {"python": platform.python_version(), "bits": 64 if sys.maxsize > 2**32 else 32,
                        "windows": f"{v.major}.{v.minor}.{v.build}"}
        say(f"  Python {ctx.env_info['python']}（{ctx.env_info['bits']} 位），Windows 版本号 {ctx.env_info['windows']}")
        src = Path(sys.base_prefix)
        if not (src / "python.exe").is_file() or not (src / "Lib" / "os.py").is_file():
            say(f"  在 {ctx.rd(str(src))} 里没找到可以拷的 Python 运行时（python.exe、Lib\\），没法往下做。")
            return 2
    return None


def step2(ctx: Ctx) -> None:
    layout = ctx.layout
    with StepTimer(ctx, 2):
        why = handle_leftover(ctx)
        if why:
            raise EnvProblem(why)
        layout.results.mkdir(exist_ok=True)
        ctx.results_dir.mkdir()
        say(f"  结果文件夹：{ctx.path(ctx.results_dir)}")
        layout.work.mkdir()
        ctx.work_created = True
        ctx.save_marker()
        for folder in (layout.home, layout.pyrt, layout.probe, layout.decoy_dir):
            folder.mkdir()
        say(f"  沙箱自己的文件夹：{ctx.path(layout.home)}")
        src = Path(sys.base_prefix)
        plan = plan_runtime_copy(src)
        t0 = time.perf_counter()
        count, size = copy_runtime(plan, layout.pyrt)
        say(f"  Python 运行时：{count} 个文件，{size / 1e6:.1f} MB，来自 {ctx.rd(str(src))}，用时 {time.perf_counter() - t0:.1f} 秒")
        shutil.copy2(HERE / "child_probe.py", layout.probe / "child_probe.py")
        say(f"  探针脚本的一份拷贝：{ctx.path(layout.probe / 'child_probe.py')}")
        layout.decoy_file.write_bytes((DECOY_LINE + "\n").encode("ascii"))
        say(f"  原型目录里的假文件：{ctx.path(layout.decoy_file)}")
        ctx.user_sid = sw.current_user_sid()
        ctx.logon_sid = sw.current_logon_sid()
        ctx.rd.add_sids(sw.sid_to_string(ctx.user_sid), sw.sid_to_string(ctx.logon_sid) if ctx.logon_sid else "")
        ctx.temp_dir = Path(tempfile.gettempdir()) / (TEMP_PREFIX + secrets.token_hex(4))
        ctx.temp_dir.mkdir()
        ctx.save_marker()
        sw.protect_user_only(str(ctx.temp_dir), ctx.user_sid)
        ctx.fake_file = ctx.temp_dir / FAKE_BOX_NAME
        ctx.fake_file.write_bytes((FAKE_BOX_LINE + "\n").encode("ascii"))
        ctx.acl["临时文件夹(假保管箱所在)"] = sddl_principals(sw.dacl_sddl(str(ctx.temp_dir)), sw.sid_to_string(ctx.user_sid))
        say(f"  临时文件夹里的假保管箱：{ctx.rd(str(ctx.fake_file))}（权限：{'、'.join(ctx.acl['临时文件夹(假保管箱所在)'])}）")
        ctx.acl["work(假文件所在的上一级)"] = sddl_principals(sw.dacl_sddl(str(layout.work)), sw.sid_to_string(ctx.user_sid))
        ctx.acl["假文件所在文件夹 work\\decoy"] = sddl_principals(sw.dacl_sddl(str(layout.decoy_dir)), sw.sid_to_string(ctx.user_sid))


def step3(ctx: Ctx) -> None:
    layout = ctx.layout
    with StepTimer(ctx, 3):
        ctx.pkg_sid = sw.appcontainer_sid(APPC_NAME)
        pkg_text = sw.sid_to_string(ctx.pkg_sid)
        ctx.profile_before = sw.appcontainer_registered(ctx.pkg_sid)
        before_text = {True: "有", False: "没有", None: "没法确认"}[ctx.profile_before]
        say(f"  沙箱身份（应用容器 {APPC_NAME}）：只按名字算出了 SID，这一步没有登记（登记表里原来{before_text}）")
        if ctx.profile_before:  # 名字是这个原型专用的, 原来就有只能是上次没收尾留下的: 收尾时一并删
            ctx.profile_registered = True
            ctx.notes["profile_leftover_from_earlier_run"] = True
            ctx.save_marker()
        # 受限令牌那两种关法用的限制 SID 是另一个编出来的"沙箱令牌 SID" (应用容器的 SID 当限制 SID 系统不收, 见 sandbox_token_sid)
        ctx.token_sid = sw.sandbox_token_sid(APPC_NAME)
        sandbox_sids = [ctx.pkg_sid, ctx.token_sid]
        sw.set_dacl(str(layout.pyrt), [(s, sw.ACCESS_READ_EXEC, sw.INHERIT_BOTH) for s in sandbox_sids])
        sw.set_dacl(str(layout.probe), [(s, sw.ACCESS_READ_EXEC, sw.INHERIT_BOTH) for s in sandbox_sids])
        sw.set_dacl(str(layout.home), [(s, sw.ACCESS_MODIFY, sw.INHERIT_BOTH) for s in sandbox_sids])
        say(f"  给沙箱身份加权限：读 {ctx.path(layout.pyrt)} 和 {ctx.path(layout.probe)}，读写 {ctx.path(layout.home)}")
        # 改完整性标签要 WRITE_OWNER 这一项权限, "修改"里没有 (上次真机就是先被拒再补的): 一开始就让本人对这个自己建的
        # 文件夹有完全控制 (随 work 一起删), 再打标签, 不走“先失败再补”
        sw.set_dacl(str(layout.home), [(ctx.user_sid, sw.FILE_ALL_ACCESS, sw.INHERIT_BOTH)])
        ctx.notes["home_user_full_control"] = True
        sw.set_low_label(str(layout.home))
        say(f"  给本人对 {ctx.path(layout.home)} 加了完全控制（改标签要用），并打了“低”完整性标签（低完整性的进程才写得进去）")
        # 核对: 拷进去的文件有没有继承到权限; 没有就逐个补
        sample = layout.pyrt / "Lib" / "os.py"
        inherited = pkg_text.upper() in sw.dacl_sddl(str(sample)).upper()
        ctx.notes["permission_inherited_by_copied_files"] = inherited
        if not inherited:
            say("  拷进去的文件没有自动继承到权限，逐个补")
            for base in (layout.pyrt, layout.probe):
                for folder, dirs, names in os.walk(base):
                    for name in dirs + names:
                        sw.set_dacl(str(Path(folder) / name), [(s, sw.ACCESS_READ_EXEC, 0) for s in sandbox_sids])
        tok_text = sw.sid_to_string(ctx.token_sid)
        ctx.acl["work\\pyrt"] = sddl_principals(sw.dacl_sddl(str(layout.pyrt)), sw.sid_to_string(ctx.user_sid), pkg_text, tok_text)
        ctx.acl["work\\home"] = sddl_principals(sw.dacl_sddl(str(layout.home)), sw.sid_to_string(ctx.user_sid), pkg_text, tok_text)


class TokenFailed(sw.SandboxError):
    """造受限令牌这一步就失败了 (还没到起进程): 这时没有“同一个身份”可以拿来起别的程序做诊断."""


def prepare_token(ctx: Ctx, variant: Variant) -> int:
    """按配法造受限令牌 (用完交给 sw.close_handle). 报 87 时顺手查是哪个限制 SID 不被收; 失败抛 TokenFailed."""
    restrict = resolve_restrict_sids(variant.restrict, ctx.token_sid, ctx.logon_sid)
    notes: list[str] = []
    disable = [] if variant.plain else [sw.sid_from_string(sw.SID_ADMINS)]
    try:
        token = sw.make_restricted_token(disable=disable, restrict=restrict, low_integrity=variant.low,
                                         default_dacl_sids=[ctx.token_sid] if restrict else [], notes=notes,
                                         flags=0 if variant.plain else sw.DISABLE_MAX_PRIVILEGE)
    except sw.SandboxError as exc:
        if restrict and exc.code == sw.ERROR_INVALID_PARAMETER:  # 87: 查是哪个限制 SID 不被收 (一个一个单独试, 试完就关)
            ctx.notes.setdefault("restrict_sid_diagnosis", {})[variant.name] = diagnose_restrict_sids(ctx, variant)
        raise TokenFailed(exc.api, exc.code, exc.note) from exc
    if notes:  # 令牌上尽力而为的那一步没成, 记下来 (不算出错)
        ctx.notes.setdefault("token_notes", []).extend(ctx.rd(n) for n in notes)
    return token


def system_cmd() -> tuple[str, list[str]]:
    """诊断用: 系统自带的 cmd.exe /d /c exit 7 -- 什么都不做, 只回一个退出码 7."""
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    return root + "\\System32\\cmd.exe", ["/d", "/c", "exit", "7"]


def launch(ctx: Ctx, mode_id: str, variant: Variant, command: tuple[str, list[str]] | None = None) -> sw.Child:
    """按关法起一个探针 (挂起 -> 进作业 -> 恢复), 只继承标准输入输出. command 不给就是起探针; 给了 (诊断用) 就起它."""
    exe, args = command if command is not None else probe_command(ctx.layout)
    env, cwd = minimal_env(ctx.layout.home), str(ctx.layout.home)
    if variant.env_ext:
        env = extended_env(env)
    if variant.env_inherit:
        env = None
    if mode_id == "appcontainer":
        if variant.register:
            _, created = sw.appcontainer_register(APPC_NAME, APPC_DISPLAY, APPC_DESC)
            ctx.profile_registered = True
            ctx.notes["profile_created_by_this_run"] = bool(ctx.notes.get("profile_created_by_this_run") or created)
            ctx.save_marker()
        return sw.spawn(exe, args, cwd, env, appcontainer_sid_bytes=ctx.pkg_sid)
    if mode_id == "control":
        return sw.spawn(exe, args, cwd, env)
    token = prepare_token(ctx, variant)
    try:
        return sw.spawn(exe, args, cwd, env, token=token)
    finally:
        sw.close_handle(token)


def sid_label(ctx: Ctx, name: str) -> str:
    """限制 SID 的名字换成能写进结果的说法 (登录 SID 是这次登录的, 不写出来)."""
    return {"tok": "沙箱令牌SID(编出来的)", "logon": "登录SID(本次登录, 不写出)"}.get(name, name)


def diagnose_restrict_sids(ctx: Ctx, variant: Variant) -> dict:
    """CreateRestrictedToken 报 87 时: 每个限制 SID 单独试一次 (只造令牌、立刻关, 不起进程), 看是哪个被拒. 返回 {名字: 错误码或 0}."""
    out: dict[str, int] = {}
    for name in variant.restrict:
        sids = resolve_restrict_sids((name,), ctx.token_sid, ctx.logon_sid)
        if not sids:
            out[sid_label(ctx, name)] = -1  # 这个名字解析不出 SID (比如取不到登录 SID)
            continue
        try:
            sw.close_handle(sw.make_restricted_token(restrict=sids, low_integrity=False))
            out[sid_label(ctx, name)] = 0
        except sw.SandboxError as exc:
            out[sid_label(ctx, name)] = exc.code
    if len(variant.restrict) > 1:  # 逐个都收、合起来却 87 时, 全是 0 容易被读成“都收了”: 再用整组、不改组试一次
        sids = resolve_restrict_sids(variant.restrict, ctx.token_sid, ctx.logon_sid)
        try:
            sw.close_handle(sw.make_restricted_token(restrict=sids, low_integrity=False))
            out["全部一起(不改组)"] = 0
        except sw.SandboxError as exc:
            out["全部一起(不改组)"] = exc.code
    return out


def d1_paths(layout: Layout) -> list[tuple[str, str]]:
    """D1 诊断只读打开的固定名单: (写进结果的相对名字, 绝对路径). 运行时里的几个关键文件、探针、沙箱自己的文件夹、System32 里几个 DLL."""
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    names = [("pyrt\\" + n, str(layout.pyrt / n)) for n in
             ("python.exe", "python310.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "Lib\\os.py")]
    names += [("probe\\child_probe.py", str(layout.probe / "child_probe.py")), ("home", str(layout.home))]
    names += [("System32\\" + n, root + "\\System32\\" + n) for n in ("ucrtbase.dll", "version.dll", "ws2_32.dll", "shlwapi.dll")]
    return names


def diag_open_as_token(ctx: Ctx, variant: Variant, rec: dict) -> None:
    """D1: 令牌的进程创建了却没回话时, 临时线程换成同样的令牌身份, 对固定名单各只读打开一次, 只记相对名字和错误码.
    做不成只记一笔, 不影响后面."""
    key = "以同一个令牌的身份只读打开这几个文件(0=打得开,其余是错误码: 先是查属性,再是打开)"
    paths = d1_paths(ctx.layout)
    try:
        token = prepare_token(ctx, variant)
    except Exception as exc:  # noqa: BLE001 - 诊断做不成只记一笔
        rec[key] = "诊断没做成: " + ctx.rd(str(exc))
        return
    try:
        got = sw.open_as_token(token, [p for _, p in paths])
    except Exception as exc:  # noqa: BLE001
        rec[key] = "诊断没做成: " + ctx.rd(f"{type(exc).__name__}: {exc}")
        return
    finally:
        sw.close_handle(token)
    if not got.get("impersonated"):
        rec[key] = "诊断没做成: " + ctx.rd(str(got.get("error")))
    else:
        rec[key] = {label: got["results"].get(path) for label, path in paths}


def diag_system_cmd(ctx: Ctx, mode_id: str, variant: Variant, rec: dict) -> None:
    """D2: 用同一个身份、环境、工作目录、作业对象, 起系统自带的 cmd.exe /d /c exit 7, 只看退出码是不是 7. 做不成只记一笔."""
    key = "同一个身份起系统自带的cmd(exit 7)"
    try:
        child = launch(ctx, mode_id, variant, command=system_cmd())
    except Exception as exc:  # noqa: BLE001
        rec[key] = "没起来: " + ctx.rd(str(exc))
        return
    ctx.track(child)
    try:
        exited = child.wait_exit(5000)
        code = child.exit_code()
    finally:
        ctx.close_child(child)
    rec[key] = ("起来了, 退出码 7 (对)" if code == 7 else f"起来了, 但退出码是 {fmt_code(code) or '没退出'}") if exited \
        else "起来了, 但 5 秒内没退出 (已结束)"


TRANSIENT_CODES = (32, 33)  # 文件正被别的程序占着 (比如杀毒软件在扫刚拷进来的 python.exe), 隔一会儿就好
RETRY_WAIT_S = 1.0


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, sw.SandboxError) and exc.code in TRANSIENT_CODES


def launch_with_retry(ctx: Ctx, mode_id: str, variant: Variant, rec: dict) -> sw.Child:
    """起探针; 只对"文件被占着"这一种暂时性的错等 1 秒再起一次 (别的错一次就记下, 不重试)."""
    try:
        return launch(ctx, mode_id, variant)
    except sw.SandboxError as exc:
        if not is_transient(exc):
            raise
        rec["重试"] = f"第一次起进程时文件被占着 (错误码 {exc.code}), 等 {RETRY_WAIT_S:g} 秒再起了一次"
        time.sleep(RETRY_WAIT_S)
        return launch(ctx, mode_id, variant)


def safe_ask(child: sw.Child, cmd: dict, limit: float) -> tuple[dict | None, str | None]:
    """问探针一句. 返回 (回话, 出错说明); 出错说明: None / timeout / eof / broken / deadline."""
    if limit <= 0:
        return None, "deadline"
    try:
        return child.ask(cmd, limit), None
    except TimeoutError:
        return None, "timeout"
    except EOFError:
        return None, "eof"
    except OSError:
        return None, "broken"


def start_probe(ctx: Ctx, mode_id: str, result: dict) -> tuple[sw.Child | None, float]:
    """按这种关法的几种配法依次试, 第一个"起来并回了 ping"的算数. 返回 (探针, 这个探针的最后期限)."""
    for variant in VARIANTS[mode_id]:
        rec: dict = {"配法": variant.name}
        if variant.restrict:
            rec["限制SID"] = [sid_label(ctx, n) for n in variant.restrict]
        if variant.env_ext:
            rec["环境变量"] = "最小五个 + " + "、".join(n for n in EXTENDED_ENV_NAMES if os.environ.get(n))
        if variant.env_inherit:
            rec["环境变量"] = "让子进程继承本进程的环境(只是诊断)"
        if not variant.low:
            rec["完整性级别"] = "不降(中), 只是诊断"
        if variant.plain:
            rec["令牌"] = "不去特权、不改组(几乎和自己的一样), 只是诊断"
        result["tries"].append(rec)
        t_launch = time.perf_counter()
        deadline = time.monotonic() + PROBE_LIMIT_S  # 每个探针从创建起最多 20 秒
        try:
            child = launch_with_retry(ctx, mode_id, variant, rec)
        except (sw.SandboxError, OSError, ValueError) as exc:
            rec["起来了吗"] = "没起来"
            rec["错误"] = ctx.rd(str(exc))
            diag = ctx.notes.get("restrict_sid_diagnosis", {}).get(variant.name)
            if diag:
                rec["逐个限制SID单独试(0=收,其余是错误码)"] = diag
            result["python_code"] = getattr(exc, "code", None)
            say(f"    配法「{variant.name}」没起来：{ctx.rd(str(exc))}")
            if mode_id != "control" and not isinstance(exc, TokenFailed):  # 造令牌就失败的没有“同一个身份”可用, 不做 D2
                diag_system_cmd(ctx, mode_id, variant, rec)
            continue
        ctx.track(child)  # 起来就登记为“活着”: 下面等回话时被打断, 第 6 步也找得到它
        try:
            rec["创建进程用时ms"] = round(child.create_ms, 1)
            nonce = secrets.token_hex(4)
            reply, err = safe_ask(child, {"cmd": "ping", "nonce": nonce}, min(PING_LIMIT_S, deadline - time.monotonic()))
            if err is None and reply and reply.get("ok") and reply.get("pong") == nonce:
                rec["起来了吗"] = "起来了"
                result["variant"] = variant.name
                result["start_ms"] = round((time.perf_counter() - t_launch) * 1000)
                rec["到第一句回话ms"] = result["start_ms"]
                if variant.diagnostic:
                    result["diagnostic"] = True
                return child, deadline
            summary = ctx.close_child(child)
        except BaseException:
            ctx.close_child(child)
            raise
        rec["起来了吗"] = "创建了进程，但没回话"
        rec["没回话的原因"] = str(err or "回话对不上")
        rec["退出码"] = fmt_code(summary.get("exit_code")) or "没退出"
        # 继承整份环境的诊断配法: 子进程的 stderr 原文不记 (抹除盖不住任意的密钥值), 只记长度
        text = child.stderr_text().strip()
        rec["stderr"] = f"(这种配法不记原文, 共 {len(text)} 个字符)" if variant.env_inherit else ctx.rd(text[:400])
        result["python_code"] = summary.get("exit_code")
        if err in ("timeout", "deadline"):
            rec["超时"] = True
        say(f"    配法「{variant.name}」创建了进程但没回话（{err}；退出码 {rec['退出码']}）")
        if mode_id in ("restricted_downgrade", "restricted_sids"):
            diag_open_as_token(ctx, variant, rec)       # D1
        if mode_id != "control":
            diag_system_cmd(ctx, mode_id, variant, rec)  # D2
        if not summary.get("exited"):  # 结束了它却还没退出: 不再试下一种, 免得新旧两个同时在
            rec["没退出"] = "结束作业后 5 秒它还没退出, 不再试后面的配法"
            say("    它结束后还没退出, 不再试后面的配法")
            break
    return None, 0.0


def probe_elevation(ctx: Ctx, mode_id: str, child: sw.Child, limit: float) -> tuple[Item, dict]:
    """让探针向 Windows 要一次"以管理员身份运行", 然后核对: 结果文件、进程有没有留下、结果文件夹里多了什么."""
    command, why = build_elevate_command(str(ctx.results_dir), mode_id, os.environ.get("SystemRoot") or "C:\\Windows")
    if command is None:
        return Item(UNKNOWN, note="没试: " + why), {"没试的原因": why}
    ctx.elevate_command = ctx.rd(command["file"] + " " + command["params"]).replace(command["name"], "elev_<关法>.txt")
    before = set(os.listdir(ctx.results_dir))
    reply, err = safe_ask(child, {"cmd": "elevate", "file": command["file"], "params": command["params"],
                                  "dir": command["dir"], "wait_ms": int(ELEV_WAIT_S * 1000)}, limit)
    # 最多等 5 秒, 看那个文件有没有出现. 请求当场被拒的 (返回 FALSE) 没有进程, 文件不会出现, 只再看一眼就够;
    # 收尾时还会对着“没出现”的再核对一遍 (迟到检查)
    refused = bool(reply and reply.get("ok") and reply.get("ret") == 0)
    deadline = time.monotonic() + (ELEV_REFUSED_LOOK_S if refused else ELEV_WAIT_S)
    while True:
        word = read_elev_word(command["result"])
        if word is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    item = classify_elevate(reply, err, word)
    pid = (reply or {}).get("pid")
    alive = sw.process_exists(pid) if pid else None
    waited = (reply or {}).get("waited")
    gone = process_gone_state(pid, waited, alive, word)
    if pid:
        ctx.elevated_pids.append((pid, gone))   # 收尾时没确认过已经没了的, 再核对一遍
    after = set(os.listdir(ctx.results_dir))
    extra = sorted(after - before - {command["name"]})
    if extra:
        folder_text = "多了意外的: " + "、".join(extra)
    elif word is not None:
        folder_text = "只多了预期的那一个"
    else:
        folder_text = "没多"
    detail = {
        "请求返回值": (reply or {}).get("ret"), "请求错误码": (reply or {}).get("err"),
        "结果文件": {"ELEVATED": "ELEVATED", "NOT_ELEVATED": "NOT_ELEVATED", "OTHER": "别的内容", None: "没出现"}[word],
        "拿到进程句柄": bool(pid), "等它退出": waited or "没有进程可等",
        "这次试出来的进程已经没有了": {True: "是", False: "否", None: "没法确认"}[gone],
        "结果文件夹里多出的文件": folder_text,
    }
    return item, detail


def run_checks(ctx: Ctx, mode_id: str, child: sw.Child, deadline: float, result: dict, items: dict[str, Item]) -> None:
    """探针起来以后依次问: 令牌信息 / 读两个假文件 / 往三处写 / 连测试端口 / (问了真试的话)要管理员权限.
    deadline 是这个探针的最后期限 (time.monotonic 的刻度); 到了就不再问, 收尾时结束作业, 记成超时."""
    layout = ctx.layout
    dead: str | None = None
    items["python"] = Item(CAN)

    def ask(cmd: dict, limit: float = CMD_LIMIT_S) -> tuple[dict | None, str | None]:
        nonlocal dead
        if dead:
            return None, dead
        reply, err = safe_ask(child, cmd, min(limit, deadline - time.monotonic()))
        if err:
            dead = err
            if err in ("timeout", "deadline"):
                result["timeout"] = True
        return reply, err

    reply, _ = ask({"cmd": "info"})
    if reply and reply.get("ok"):
        result["token"] = describe_token(reply)
    elif reply:  # 探针连自己的令牌都打不开 (限制 SID 的令牌可能这样): 记下是什么错, 不算这一种关法出错
        result["token"] = {"读不到令牌信息": f"{reply.get('err')}({fmt_code(err_code(reply))})"}

    for key, path, expect in (("read_fake", ctx.fake_file, FAKE_BOX_LINE), ("read_decoy", layout.decoy_file, DECOY_LINE)):
        reply, err = ask({"cmd": "read", "path": str(path), "expect": expect})
        items[key] = classify_read(reply, err)
    for key, folder in (("write_work", layout.work), ("write_temp", ctx.temp_dir), ("write_home", layout.home)):
        target = folder / f"wtest_{mode_id}.txt"
        reply, err = ask({"cmd": "write", "path": str(target)})
        items[key] = classify_write(reply, err, target.exists())
    before = ctx.listener.accepted
    reply, err = ask({"cmd": "connect", "port": ctx.listener.port})
    if reply and reply.get("ok"):
        time.sleep(SETTLE_S)  # 给监听线程一点时间记下这次连接
    items["connect"] = classify_connect(reply, err, ctx.listener.accepted - before)
    diagnostic = bool(result.get("diagnostic"))
    if diagnostic:  # 诊断配法一律不发提权请求 (没降权的那种可能真拿到管理员权限, 换来的结果又不算数)
        items["elevate"] = Item(UNKNOWN, note="没验 (诊断配法不发提权请求)")
    elif ctx.try_elevation:
        if dead:
            items["elevate"] = Item(ERROR, note=f"探针没了 ({dead})，没法试")
        else:
            item, detail = probe_elevation(ctx, mode_id, child, min(ELEV_CMD_LIMIT_S, max(deadline - time.monotonic(), 1.0)))
            items["elevate"] = item
            result["elevate_detail"] = detail
    else:
        items["elevate"] = Item(UNKNOWN, note="用户选了不试; 只读了令牌信息")
    tag = "（诊断配法量到的，不算数）" if diagnostic else ""
    for key in ITEM_KEYS:
        say(f"    {tag}{ITEM_TITLES[key]}：{item_cell(items[key].to_dict())}")
    if not dead:  # 礼貌地让它自己退出 (等一下, 免得收尾时被当成强行结束); 退不出去收尾时会结束作业
        safe_ask(child, {"cmd": "quit"}, 3.0)
        child.wait_exit(2000)


def measure_mode(ctx: Ctx, mode_id: str, label: str) -> dict:
    """量一种关法. 结果字典一开头就放进 ctx.modes, 中途被打断也留得下已经量到的."""
    say(f"\n  [{label}]")
    result: dict = {"id": mode_id, "label": label, "variant": None, "tries": [], "items": {}, "start_ms": None,
                    "token": None}
    ctx.modes.append(result)
    items: dict[str, Item] = {}
    try:
        child, deadline = start_probe(ctx, mode_id, result)
        if child is None:
            code = result.pop("python_code", None)
            items["python"] = Item(CANNOT, code, "Python 没起来 (见“试过”)")
            for key in ITEM_KEYS[1:]:
                items[key] = Item(UNKNOWN, note="没验: Python 没起来")
            say(f"    Python 起不来：{item_cell(items['python'].to_dict())}")
        else:
            diag = bool(result.get("diagnostic"))
            say(f"    Python 起来了（配法「{result['variant']}」，{result['start_ms']} ms）"
                + ("——这是诊断配法，下面量到的不算数" if diag else ""))
            try:
                run_checks(ctx, mode_id, child, deadline, result, items)
            finally:
                result["close"] = ctx.close_child(child)
            if diag:  # 诊断配法量到的单独放; 规格那一行照实写: Python 做不到, 其余没验
                result["diagnostic_items"] = {key: item.to_dict() for key, item in items.items()}
                result["diagnostic_start_ms"], result["start_ms"] = result["start_ms"], None
                code = result.pop("python_code", None)
                items = {"python": Item(CANNOT, code, "规格里的配法都没起来 (只有诊断配法起来了, 它量到的另放, 不算数)")}
                items.update({key: Item(UNKNOWN, note="没验: 规格里的配法没起来") for key in ITEM_KEYS[1:]})
    finally:
        result.pop("python_code", None)
        result["items"] = {key: item.to_dict() for key, item in items.items()}
    return result


def step4(ctx: Ctx) -> None:
    with StepTimer(ctx, 4):
        say("  下面每个探针还会顺带做第 5 步（提权那一条），所以现在先问你一次。")
        say("  “真试”会让 Python 起来了的探针向 Windows 要一次“以管理员身份运行”，去起系统自带的 cmd，它只把 ELEVATED 或")
        say("  NOT_ELEVATED 一个词写进结果文件夹（诊断配法除外：诊断配法不发这个请求）。对照那一次一定会真的以管理员身份跑一下")
        say("  （你电脑上提权确认是关着的）。不试的话，这一条只读令牌信息，记“没验”。")
        try:
            answer = input(ELEV_QUESTION)
        except EOFError:
            answer = ""
        ctx.try_elevation = wants_elevation_test(answer)
        ctx.elevation_note = "用户输入 y，真试" if ctx.try_elevation else "用户没输入 y，不试"
        say("  -> " + ("真试" if ctx.try_elevation else "不试"))
        ctx.listener = Listener()
        self_before = ctx.listener.accepted
        try:
            socket.create_connection(("127.0.0.1", ctx.listener.port), timeout=2).close()
            time.sleep(SETTLE_S)
        except OSError:
            pass
        ok = ctx.listener.accepted > self_before
        ctx.notes["listener_selfcheck"] = ok
        say(f"  测试端口：127.0.0.1:{ctx.listener.port}（只绑本机回环）；自己先连了一次：{'通' if ok else '不通'}")
        for mode_id, label in MODES:
            measure_mode(ctx, mode_id, label)


def step5(ctx: Ctx) -> None:
    with StepTimer(ctx, 5):
        ctx.notes["elevation"] = finalize_elevation(ctx.modes, ctx.try_elevation)
        concl = ctx.notes["elevation"]["conclusion"]
        if not ctx.try_elevation:
            say("  没试（你选了不试）：只读了各探针令牌的几项信息，这一条记“没验”。")
        else:
            for mode in ctx.modes:
                d = mode.get("elevate_detail") or {}
                it = mode["items"].get("elevate", {"state": UNKNOWN})
                if mode.get("diagnostic"):
                    say(f"  {mode['label']}：只有诊断配法起来了，诊断配法不发提权请求，这一格没验")
                    continue
                say(f"  {mode['label']}：{item_cell(it)}"
                    f"；结果文件：{d.get('结果文件', '-')}；这次试出来的进程已没有：{d.get('这次试出来的进程已经没有了', '-')}；"
                    f"结果文件夹：{d.get('结果文件夹里多出的文件', '-')}")
        say(f"  结论：{concl}")


def step6(ctx: Ctx) -> None:
    """收尾. 不管前面哪一步出错都要走到, 每一小步各自兜住错误."""
    layout = ctx.layout
    with StepTimer(ctx, 6):
        cl = ctx.cleanup

        def attempt(name: str, fn) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 收尾的每一小步都不能拖垮后面的
                cl[name] = "出错: " + ctx.rd(f"{type(exc).__name__}: {exc}")
                say(f"  {name}：出错 {ctx.rd(str(exc))}")

        # 1 结束还活着的作业, 核对自己起的进程都退了
        def close_probes() -> None:
            # 先把还活着的探针逐个结束 (比如等第一句回话时被 Ctrl+C 打断的), 再核对
            for child in list(ctx.live):
                try:
                    ctx.close_child(child)
                except Exception as exc:  # noqa: BLE001 - 一个结束不了不拖垮后面的
                    cl["结束探针出错"] = ctx.rd(f"{type(exc).__name__}: {exc}")
            summaries = ctx.closed
            cl["起过的探针个数"] = len(summaries)
            cl["全部已退出"] = all(s.get("exited") for s in summaries) if summaries else True
            cl["作业里没有剩下的进程"] = all(s.get("active_in_job") in (0, None) for s in summaries) if summaries else True
            alive = [pid for pid, gone in ctx.elevated_pids if gone is not True and sw.process_exists(pid) is not False]
            cl["提权请求试出来的进程都没有了"] = "是" if not alive else "否或没法确认 (" + str(len(alive)) + " 个)"
            say(f"  结束作业、核对进程：探针 {len(summaries)} 个，全部已退出 {cl['全部已退出']}，"
                f"作业里没剩进程 {cl['作业里没有剩下的进程']}；提权请求试出来的进程都没有了：{cl['提权请求试出来的进程都没有了']}")
        attempt("核对进程", close_probes)

        # 2 关测试端口
        def close_listener() -> None:
            if ctx.listener:
                ctx.listener.close()
        attempt("关测试端口", close_listener)

        # 3 删登记的沙箱身份
        def drop_profile() -> None:
            if ctx.profile_registered:
                code = sw.appcontainer_unregister(APPC_NAME)
                cl["删登记的沙箱身份"] = "已删" if code == 0 else "没删成, 错误码 " + fmt_code(code)
            else:
                cl["删登记的沙箱身份"] = "没登记过, 不用删"
            if ctx.pkg_sid:
                cl["登记表里还有这个沙箱身份吗"] = {True: "有", False: "没有", None: "没法确认"}[sw.appcontainer_registered(ctx.pkg_sid)]
                if os.environ.get("LOCALAPPDATA"):
                    folder = Path(os.environ["LOCALAPPDATA"]) / "Packages" / APPC_NAME
                    cl["AppData\\Local\\Packages 下还有它的文件夹吗"] = "有" if folder.exists() else "没有"
            say(f"  沙箱身份：{cl.get('删登记的沙箱身份')}；登记表里还有：{cl.get('登记表里还有这个沙箱身份吗', '-')}")
        attempt("删沙箱身份", drop_profile)

        # 4 删 work\ 和临时文件夹里的子目录
        def drop_dirs() -> None:
            if ctx.temp_dir is not None:
                left = rmtree_robust(ctx.temp_dir)
                cl["临时文件夹里的子目录"] = "已删" if left is None else left
            if ctx.work_created:
                left = rmtree_robust(layout.work)
                cl["work 目录"] = "已删" if left is None else left
            else:
                cl["work 目录"] = "这次没建过，没动"
            say(f"  删假文件和拷来的运行时：work {cl['work 目录']}；临时文件夹里的子目录 {cl.get('临时文件夹里的子目录', '没建过')}")
        attempt("删目录", drop_dirs)

        # 5 迟到检查: 记成"没出现"的提权结果文件, 现在再看一眼
        def late_check() -> None:
            late = [m["id"] for m in ctx.modes if m.get("elevate_detail", {}).get("结果文件") == "没出现"
                    and read_elev_word(str(ctx.results_dir / f"elev_{m['id']}.txt")) is not None]
            cl["迟到出现的提权结果文件"] = "、".join(late) if late else "没有"
        attempt("迟到检查", late_check)

        # 6 最后只留 results\
        def root_listing() -> None:
            extra = unexpected_root_names(os.listdir(layout.root))
            ctx.root_extra = extra
            cl["原型目录里多出来的东西"] = "、".join(extra) if extra else "没有 (只剩源文件、tests 和 results)"
        attempt("核对原型目录", root_listing)


def appcontainer_summary(ctx: Ctx) -> str:
    """应用容器那一句总结: 按配法的 register / diagnostic 字段判断, 登记和收尾删没删取真实的记录, 不写死."""
    app_name = next((m.get("variant") for m in ctx.modes if m["id"] == "appcontainer"), None)
    variant = next((v for v in VARIANTS["appcontainer"] if v.name == app_name), None)
    if variant is None:
        head = "没有一种配法起得来"
    elif variant.diagnostic:
        head = "只有诊断配法起来了（不算数）"
    elif variant.register:
        head = "这次登记了沙箱身份（" + ("这次新建的" if ctx.notes.get("profile_created_by_this_run") else "上次的残留") + "），登记后起来了"
    else:
        head = "只按名字算出了 SID，没有登记（没写注册表，没建 AppData\\Local\\Packages 下的文件夹）"
    registered = ctx.profile_registered or bool(ctx.notes.get("profile_created_by_this_run"))
    if registered and not head.startswith("这次登记了沙箱身份"):
        head += "；这次登记过沙箱身份"
    if registered:
        head += "；收尾：" + str(ctx.cleanup.get("删登记的沙箱身份", "还没走到收尾"))
    return head


def build_report(ctx: Ctx, exit_code: int) -> dict:
    summary = appcontainer_summary(ctx)
    modes = []
    for mode in ctx.modes:
        mode_out = dict(mode)
        mode_out["verdict"] = mode_verdict(mode["id"], {k: Item(**v) for k, v in mode.get("items", {}).items()},
                                           ctx.try_elevation, bool(mode.get("diagnostic")))
        modes.append(mode_out)
    elevation = dict(ctx.notes.get("elevation") or {"tried": ctx.try_elevation, "conclusion": "没跑到这一步"})
    elevation["choice"] = ctx.elevation_note
    if ctx.elevate_command:
        elevation["command"] = ctx.elevate_command
    return {
        "schema": 1, "started_at": ctx.notes.get("started_at"), "finished_at": now_iso(), "exit_code": exit_code,
        "environment": ctx.env_info, "uac_settings": ctx.uac, "elevation": elevation,
        "appcontainer": {"name": APPC_NAME, "summary": summary, "registered_before_this_run": ctx.profile_before},
        "steps": ctx.steps, "modes": modes, "acl": ctx.acl, "cleanup": ctx.cleanup,
        "notes": {k: v for k, v in ctx.notes.items() if k not in ("elevation", "started_at")},
        "errors": ctx.errors,
    }


def write_results(ctx: Ctx, exit_code: int) -> None:
    ctx.results_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(ctx, exit_code)
    # 先抹一遍再扫一遍 (json 和 md 两份都扫); 扫的结果也写进结果里, 所以再生成一次
    leaks = ctx.rd.leaks(ctx.rd(json.dumps(report, ensure_ascii=False, indent=2)) + "\n" + ctx.rd(render_report_md(report)))
    report["pii_scan"] = {"result": "没找到个人信息" if not leaks else "抹完还找到: " + "、".join(leaks)}
    text_json = ctx.rd(json.dumps(report, ensure_ascii=False, indent=2))
    text_md = ctx.rd(render_report_md(report))
    for name, text in (("report.json", text_json), ("report.md", text_md)):
        with open(ctx.results_dir / name, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    say(f"\n  结果写在 {ctx.path(ctx.results_dir)}（report.md 一张表，report.json 全部数据）；个人信息扫描：{report['pii_scan']['result']}")


def run() -> int:
    if os.name != "nt":
        say("这个原型只能在 Windows 上跑。")
        return 2
    rd = Redactor.from_env()
    layout = make_layout(HERE)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ctx = Ctx(layout=layout, rd=rd, stamp=stamp, results_dir=layout.results / stamp)
    ctx.notes["started_at"] = now_iso()
    say("隔离原型：把 Python 关进受限的身份里，量它读不读得到文件、写不写得出去、连不连得上本机、要不要得到管理员权限。")
    say("正常 1 到 3 分钟（不含你回答“真试吗”的时间），最坏约 5 分钟；结果写在 results 文件夹里；跑完会把自己建的东西都删掉。")
    early = step1(ctx)
    if early is not None:
        return early
    exit_code = 0
    try:
        step2(ctx)
        step3(ctx)
        step4(ctx)
        step5(ctx)
    except KeyboardInterrupt:
        ctx.errors.append("被 Ctrl+C 打断")
        exit_code = 1
    except EnvProblem as exc:
        ctx.errors.append(rd(str(exc)))
        say(f"\n  环境不满足：{rd(str(exc))}")
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 - 出什么错都要先收尾, 再把错写进结果
        ctx.errors.append(rd("".join(traceback.format_exception_only(type(exc), exc)).strip()))
        ctx.errors.append(rd(traceback.format_exc()[-1500:]))
        say(f"\n  中途出错：{rd(str(exc))}（先收尾，错误会写进结果）")
        exit_code = 1
    finally:
        try:
            step6(ctx)
        except Exception as exc:  # noqa: BLE001 - 收尾本身出错也要走到写结果
            ctx.errors.append("收尾出错: " + rd(f"{type(exc).__name__}: {exc}"))
            exit_code = 1
        bad = [k for k, v in ctx.cleanup.items() if isinstance(v, str) and v.startswith(("出错", "还剩", "没删成", "否或没法确认"))]
        bad += [k for k in ("全部已退出", "作业里没有剩下的进程") if ctx.cleanup.get(k) is False]
        if bad or ctx.root_extra:
            exit_code = exit_code or 1
        try:
            write_results(ctx, exit_code)
        except Exception as exc:  # noqa: BLE001
            say(f"  写结果出错：{rd(str(exc))}")
            exit_code = 1
    say("\n跑完了。把“跑完了”告诉对话，对话会去读 results 里最新的一份。")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="measure.py", description="隔离原型的测量脚本 (用户双击 run_me.cmd 跑)")
    parser.add_argument("--plan", action="store_true", help="只把六步要做的事打出来 (什么都不建、不起、不改)")
    args = parser.parse_args(argv)
    setup_console()
    if args.plan:
        print_plan()
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
