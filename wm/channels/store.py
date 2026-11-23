"""外部渠道在磁盘上的东西, 和主人的记忆分开放:
  - 群聊原文默认只留内存上下文; 显式开启 group.log_raw / 单群 log_raw 才写 <库>/外部渠道/qq/群<号>/日志/;
  - 私聊<号>/日志/YYYY-MM-DD.md: 原文, 保留 log_days 天 (到期删);
  - wm-guest (AstrBot 等) 不能可靠区分群/私聊, 默认不存原文; 显式 api_log_raw 才写 <库>/外部渠道/api/<会话>/日志/;
  - 关闭归档时不读取或清理已有日志, 不删除旧数据; 原文归档不等于经确认的长期知识;
  - <库>/外部渠道/qq/可以说的.md: 主人写的、她可以对外说的自己的事 (QQ 上她只知道这些和本会话聊过的);
  - 主人记忆只读两样: 本人·远程档要的名字和喜好 (owner_profile), 外发检查要的住处、生日、工作这些值 (owner_secrets);
  - SnowLuma 的 token 现用现读 (snowluma_token), 不复制进我们的配置.
主人记忆的检索 (Memory._files) 会遍历整个库, 所以 外部渠道/ 必须在 memory.exclude 里: 管理器启动前用 excluded 查.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .. import memory_ops as O

PUBLIC_FILE = "可以说的.md"
_PUBLIC_HEAD = ("# 可以对外说的\n\n<!-- QQ 上的她只知道这里写的自己的事 (自我介绍、公开的设定) 和那个群、那个私聊里聊过的事, "
                "不知道你的记忆. 想让她对外说的写在下面, 一行一条. 不要写你的住址、电话这类隐私. -->\n")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def excluded(rel: str, exclude) -> bool:
    """rel (相对库根目录) 会不会被主人记忆的检索跳过: 它自己或某一级上层在 memory.exclude 里 (和 Memory._files 的判断一样)."""
    ex = tuple(str(x).strip("/") + "/" for x in exclude or [] if str(x).strip("/"))
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    return bool(ex) and any(("/".join(parts[:i]) + "/").startswith(ex) for i in range(1, len(parts) + 1))


_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*\s.]+')


def conv_folder(key: tuple) -> str:
    """会话 -> 文件夹名. QQ: 群<号> / 私聊<号>; wm-guest (("api", 会话键)): 会话键里不能做文件名的字换掉, 再接 6 位哈希 (防撞名)."""
    kind, ident = key
    if kind == "group":
        return f"群{ident}"
    if kind == "private":
        return f"私聊{ident}"
    if kind == "qzone":
        return "QQ空间"
    if kind == "self":
        return "她自己"
    if kind == "weixin":
        return "微信"
    safe = _UNSAFE.sub("_", str(ident)).strip("_")[:40] or "某人"
    return f"{safe}-{hashlib.sha1(str(ident).encode('utf-8')).hexdigest()[:6]}"


class ArchivePolicy:
    """同一管理器的新旧 store 共用的原文归档开关; 关闭同样约束在途回复与保留期清理."""
    def __init__(self, group_log_raw=False, group_overrides=None, api_log_raw=False):
        self.lock = threading.RLock()
        self.configure(group_log_raw, group_overrides, api_log_raw)

    def configure(self, group_log_raw=False, group_overrides=None, api_log_raw=False):
        if type(group_log_raw) is not bool or type(api_log_raw) is not bool:
            raise ValueError("原文归档开关必须是 bool")
        overrides = dict(group_overrides or {})
        if any(type(k) is not int or k <= 0 or type(v) is not bool for k, v in overrides.items()):
            raise ValueError("按群归档配置必须是正整数群号到 bool 的映射")
        with self.lock:
            self.group_log_raw, self.group_overrides = group_log_raw, overrides
            self.api_log_raw = api_log_raw

    def enabled(self, key):
        if key[0] == "group":
            return self.group_overrides.get(key[1], self.group_log_raw)
        return self.api_log_raw if key[0] == "api" else True


class ChannelStore:
    def __init__(self, root: Path | None, log_days: int = 30, clock=datetime.now, *,
                 group_log_raw: bool = False, group_overrides: dict[int, bool] | None = None,
                 api_log_raw: bool = False, retention: ArchivePolicy | None = None):
        """原文归档策略; 不影响调用方的短期上下文. 没开库时所有渠道都不落盘."""
        self.retention = retention if retention is not None else ArchivePolicy(group_log_raw, group_overrides, api_log_raw)
        self.root = root
        self.log_days = int(log_days)
        self.clock = clock
        self._lock = self.retention.lock
        self._public: tuple[int, str] | None = None

    @property
    def group_log_raw(self):
        return self.retention.group_log_raw

    @property
    def group_overrides(self):
        return self.retention.group_overrides

    @property
    def api_log_raw(self):
        return self.retention.api_log_raw

    def logs_enabled(self, key: tuple) -> bool:
        """群/API 原文只有明确 opt-in 才能存取; 私聊等原有机制不变."""
        with self._lock:
            return self.retention.enabled(key)

    def _folder_enabled(self, name: str) -> bool:
        if re.fullmatch(r"群[0-9]+", name):
            return self.retention.enabled(("group", int(name[1:])))
        if re.fullmatch(r"私聊[0-9]+", name) or name in ("QQ空间", "她自己", "微信"):
            return True
        return self.api_log_raw  # wm-guest 的不透明会话名; 不猜它是不是某个群

    def append(self, key: tuple[str, int], line: str) -> None:
        with self._lock:
            if self.root is None or not self.retention.enabled(key):
                return
            now = self.clock()
            folder = self.root / conv_folder(key) / "日志"
            folder.mkdir(parents=True, exist_ok=True)
            day = folder / f"{now:%Y-%m-%d}.md"
            new = not day.exists()
            with day.open("a", encoding="utf-8") as f:
                if new:
                    f.write(f"# {now:%Y-%m-%d} {conv_folder(key)}\n\n")
                f.write(f"- {line}\n")

    def tail(self, key: tuple[str, int], n: int = 20) -> list[str]:
        """这个会话最近 n 行已授权归档的日志; 关闭后不回放旧档."""
        with self._lock:
            if self.root is None or not self.retention.enabled(key):
                return []
            folder = self.root / conv_folder(key) / "日志"
            now = self.clock()
            lines: list[str] = []
            for day in (now - timedelta(days=1), now):
                path = folder / f"{day:%Y-%m-%d}.md"
                try:
                    lines += [ln[2:] for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
                except OSError:
                    continue
            return lines[-n:]

    def day_lines(self, key: tuple[str, int], day: datetime) -> list[str]:
        """这个会话某一天已授权归档的日志行; 关闭后不读取旧档."""
        with self._lock:
            if self.root is None or not self.retention.enabled(key):
                return []
            try:
                text = (self.root / conv_folder(key) / "日志" / f"{day:%Y-%m-%d}.md").read_text(encoding="utf-8")
            except OSError:
                return []
            return [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]

    def prune(self) -> list[Path]:
        """仅对仍开启的原文归档执行保留期; 关闭的旧档不读不删. log_days=0 不删."""
        with self._lock:
            if self.root is None or self.log_days <= 0 or not self.root.is_dir():
                return []
            oldest = (self.clock() - timedelta(days=self.log_days)).date()
            gone = []
            for folder in self.root.iterdir():
                if not self._folder_enabled(folder.name):
                    continue
                for path in (folder / "日志").glob("*.md"):
                    if _DAY.match(path.stem) and datetime.strptime(path.stem, "%Y-%m-%d").date() < oldest:
                        path.unlink(missing_ok=True)
                        gone.append(path)
            return gone

    def public(self, limit: int = 800) -> str:
        """可以说的.md 的正文 (去掉标题和注释), 没有就建一个空模板."""
        if self.root is None:
            return ""
        path = self.root / PUBLIC_FILE
        with self._lock:
            if not path.exists():
                self.root.mkdir(parents=True, exist_ok=True)
                path.write_text(_PUBLIC_HEAD, encoding="utf-8")
            mtime = path.stat().st_mtime_ns
            if self._public and self._public[0] == mtime:
                return self._public[1]
            text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8", errors="ignore"), flags=re.S)
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
            body = "\n".join(lines)[:limit]
            self._public = (mtime, body)
            return body


# ---------------------------------------------------------------- 主人记忆 (只读)
def _owner_facts(path: Path | None) -> list[str]:
    """关于用户.md 里程序记下、没划掉的事实. 用户手写的行不算 (没法判断类别)."""
    if path is None or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        fact = O.fact_of_line(line)
        if fact and O.is_auto(line):
            out.append(fact)
    return out


def owner_profile(path: Path | None) -> list[str]:
    """本人·远程档能看的: 名字和喜好 ("用户叫…""用户喜欢…/不喜欢…"). 住处、生日、工作、日志、摘要、图谱都不给."""
    return [f for f in _owner_facts(path) if O.SLOTS["名字"].match(f) or O._PREF.match(f)]


_WORK_AT = re.compile(r"在(.{2,12}?)(?:做|上班|工作)")


def owner_secrets(path: Path | None) -> list[tuple[str, str]]:
    """外发检查用的主人隐私值: 住处、生日、出生年, 以及"在哪儿上班"的单位名."""
    out = []
    for fact in _owner_facts(path):
        for slot in ("住处", "生日", "出生年"):
            m = O.SLOTS[slot].match(fact)
            if m:
                out.append((slot, m.group(1).strip(" ，,。")))
        if O.SLOTS["工作"].match(fact) and (m := _WORK_AT.search(fact)):
            out.append(("工作单位", m.group(1)))
    return [(k, v) for k, v in out if len(v) >= 2]


# ---------------------------------------------------------------- SnowLuma 的配置 (只读)
def _find(obj, key: str):
    """在嵌套的 dict / list 里找第一个叫 key 的值 (SnowLuma 的配置结构可能变, 不写死层级)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def snowluma_files(directory: str, self_id: int) -> list[Path]:
    d = Path(directory)
    return [d / "config" / f"onebot_{self_id}.json", d / f"onebot_{self_id}.json"]


def snowluma_token(directory: str, self_id: int, endpoint: str) -> tuple[str, list[str]]:
    """从 SnowLuma 的 config/onebot_<她的号>.json (用户说也可能在根目录) 读正向 WS 的 accessToken:
    wsServers 里 name 是 ws-default 的那项, 没有就按 endpoint 的端口找. 返回 (token, 提醒). 找不到抛 FileNotFoundError / KeyError."""
    path = next((p for p in snowluma_files(directory, self_id) if p.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"找不到 SnowLuma 的配置 config/onebot_{self_id}.json (目录 {str(directory)[:120]}); "
                                "先在 SnowLuma 里挂上她的号, 或者在面板里直接填 token")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    servers = _find(data, "wsServers") or []
    port = urlsplit(endpoint).port
    pick = next((s for s in servers if isinstance(s, dict) and s.get("name") == "ws-default"), None) \
        or next((s for s in servers if isinstance(s, dict) and port and s.get("port") == port), None)
    if pick is None:
        raise KeyError(f"{path.name} 里没有 ws-default (正向 WebSocket), 也没有端口 {port} 的: 在 SnowLuma 里打开正向 WS")
    warnings = []
    status = _find(data, "statusCommand")
    if not (isinstance(status, dict) and status.get("enabled") is False):
        warnings.append("SnowLuma 的 #sl 状态命令可能开着: 群里有人发 #sl 时它会自己回, 绕过这边的检查. "
                        "在 SnowLuma 的配置里把 statusCommand.enabled 关掉")
    return str(pick.get("accessToken") or ""), warnings
