"""账号保管箱 (2026-11-07 用户: "我想的是能不能在桌宠或者工作台底层加入一个填写各平台账号密码, 然后 ai 自己登录").

规矩 (和邮箱授权码 mailbox.py 同一套, 再多几条):
- 账号只能由主人自己填: 服务端的 /vault 页面 (vault_http.py) 的密码框, 或者程序按平台一步步做的扫码登录 (B 站, bili_login.py)
  存进来的. 不走聊天、不走模型; 接口只进不出, 没有"把密码取出来"这一项.
- 加密: Windows DPAPI (这台电脑的这个 Windows 用户才解得开), 熵值和微信、邮箱、桌宠设置的密文分开, 还绑着"账号编号 + 字段名":
  把 A 账号的密文拷到 B 账号的文件里解不开. 存在 ~/.worldmodel/vault/accounts/<编号>.json.
- 密码、令牌、Cookie 从不进模型、日志、知识库、事件、界面回显: 对外的"公开视图"(public) 只有 "存了没有"、打码的账号名、
  非机密的附加信息 (B 站的 uid 和昵称这类); 取出来的东西 (Secrets) 打印出来也是 <Secrets ...>.
- 每个账号写清能被哪些事用到 (PLATFORMS 里每个平台的 purposes). 每个用途有三态: allow (主人点过头) / ask (还没定: 第一次要用时
  弹申请卡片, 15 秒没人点就上 QQ / 微信问, 不过期) / deny (主人说不行). 用的时候走 lease(), 不在 allow 里的拿不到密码.
- 使用记录 log.jsonl: 什么时候、哪个账号、做什么、成没成, 不带密码, 页面上能看. 超过 LOG_KEEP 条只留最近的.
- 工作台守门: policy.py 的保护目录和命令正则, 加上黑名单里的两条 (protect), 所有档位都挡. 这只防工作台误碰、被带偏,
  防不了恶意的本机程序 (同一个 Windows 用户的程序本来就能调 DPAPI), 邮箱那边也是这个口径.
- 没有对应登录程序的平台 (platform=other) 只加密保存, 不会被任何东西自动使用.
- 要验证码、短信、扫码确认的时候一律发给主人本人去做; 这里不做任何绕过验证码、人机校验的东西.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("wm.channels.vault")

HOME = Path.home() / ".worldmodel" / "vault"  # 加密的账号、使用记录; 测试换成临时目录 (见 tests/conftest.py)
PAGE_URL = "http://127.0.0.1:8790/vault"      # 填账号的页面; vault_http.register 按 server.port 改
MAX_ACCOUNTS = 20
LOG_KEEP = 800            # log.jsonl 最多留多少条
WANT_AGAIN_S = 6 * 3600   # 申请的卡片没人理, 隔多久再弹一次
FIELD_MAX = {"cookie": 8192, "refresh_token": 2048}
SECRET_MAX = 512          # 别的机密字段最长多少字
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ENTROPY = b"WorldModel-vault\x00"  # 和微信、邮箱、桌宠设置的密文分开: 别处的密文拿到这里解不开
# 公开视图里允许出现的附加信息 (非机密). 别的键存不进去, 免得有人把机密塞进附加信息里再从接口读出来
META_KEYS = {"mid", "uname", "expires", "verified_at", "verified", "saved_via", "note", "uin"}
STATES = ("allow", "ask", "deny")


class VaultError(Exception):
    """保管箱的事没办成. kind: config (没这个账号、填得不对) / secret (密文解不开) / limit (太多了) / platform (只能 Windows).
    text 是给人看的一句, 不带密码."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind, self.text = kind, text


# -- 平台 ------------------------------------------------------------------------
@dataclass(frozen=True)
class Purpose:
    id: str
    name: str
    desc: str
    ui_on: bool = False   # 页面上新存账号时这个用途的勾默认打不打 (存储上没勾 = ask, 不是 allow)
    risk: str = "low"     # high: 会用她的号公开说话、花钱之类的


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    kind: str                                  # password / cookie
    secrets: tuple[tuple[str, str], ...]       # (字段名, 页面上的叫法)
    purposes: tuple[Purpose, ...]
    single: bool = True                        # 一个平台只存一个账号 (编号就是平台名)
    login: str = ""                            # program: 程序用密码登录 / scan: 页面上扫码 / "": 只保存
    howto: str = ""


PLATFORMS: dict[str, Platform] = {p.id: p for p in (
    Platform(
        "snowluma", "SnowLuma 管理页", "password", (("password", "管理页密码"),),
        (Purpose("qq_attach", "挂接她的 QQ 号",
                 "SnowLuma 重启或 QQ 重新登录后，程序登录 SnowLuma 的管理页，只对她那个 QQ 号点“挂接”，然后退出登录。"
                 "不碰别的 QQ 号，不改 SnowLuma 的设置。", ui_on=True),),
        login="program",
        howto="就是你平时在浏览器里打开 SnowLuma 管理页（http://localhost:5099，被占用时是 5100）登录用的密码。"),
    Platform(
        "bilibili", "B 站（她的号）", "cookie", (("cookie", "登录凭证"), ("refresh_token", "刷新令牌")),
        (Purpose("read", "看视频时读字幕、看消息中心和私信", "只读，不发任何东西。", ui_on=True),
         Purpose("write", "发评论、回复、点赞、回私信",
                 "用她的号公开说话。平时还要过“怎么发”的设置（先问你 / 自查后发 / 关闭），这里没勾就一律发不出去。",
                 ui_on=False, risk="high")),
        login="scan",
        howto="点“扫码登录”，用登着她那个 B 站号的手机 App 扫码并确认；不用输密码。"),
    Platform(
        "other", "其他账号（只保存）", "password", (("secret", "密码 / 令牌"),), (), single=False, login="",
        howto="这个平台还没有对应的自动登录程序：先加密存着，不会被任何东西自动使用。之后做了对应平台的登录程序，"
              "才会按你勾的用途去用。"),
)}


# -- DPAPI ----------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def fix_hint() -> str:
    return f"到 {PAGE_URL} 重新填一次"


def _dpapi(data: bytes, encrypt: bool, slot: str) -> bytes:
    """Windows DPAPI 加密 / 解密. slot 是 "账号编号:字段名", 放进熵值: 密文只认自己那一格. 不用 pywin32, 免得便携包缺它."""
    if os.name != "nt":
        raise VaultError("platform", "账号保管箱用 Windows 的 DPAPI 加密, 只能在 Windows 上用")
    entropy = _ENTROPY + slot.encode("utf-8")
    crypt, kernel = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p if encrypt else ctypes.c_void_p, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    src = (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\0")
    salt = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
    out = _Blob()
    if not fn(ctypes.byref(_Blob(len(data), src)), "WorldModel vault" if encrypt else None,
              ctypes.byref(_Blob(len(entropy), salt)), None, None, 0x1, ctypes.byref(out)):  # 0x1: 不弹窗
        raise VaultError("secret", f"存好的账号解不开 (换了电脑或 Windows 用户?): {fix_hint()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.memset(out.pbData, 0, out.cbData)
        kernel.LocalFree(out.pbData)


class Secrets(dict):
    """取出来的机密 (字段名 -> 值). 打印、写日志、拼进字符串都只显示字段名."""
    __slots__ = ()

    def __repr__(self) -> str:
        return f"<Secrets {','.join(sorted(self))}>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("机密不能被序列化")


class Lease:
    """vault.lease() 的结果: ok 为真时 secrets 里有机密; 用完调 done(成没成, 一句话). 不调的话退出 with 时记一条"没说结果"."""

    def __init__(self, vault: "Vault", aid: str, purpose: str, by: str, ok: bool, why: str = "",
                 secrets: Secrets | None = None, text: str = ""):
        self._vault, self.aid, self.purpose, self.by = vault, aid, purpose, by
        self.ok, self.why, self.text = ok, why, text
        self.secrets = secrets if secrets is not None else Secrets()
        self._done = not ok  # 没拿到的在 lease() 里已经记过了

    def get(self, name: str, default: str = "") -> str:
        return self.secrets.get(name, default)

    def done(self, ok: bool, note: str = "", quiet_s: float = 0) -> None:
        """quiet_s: 同一个账号、用途、结果的记录这么多秒内不重复记 (读字幕这类一天几十次的用途别刷屏)."""
        if self._done:
            return
        self._done = True
        self._vault.record(self.aid, self.purpose, self.by, ok, note, quiet_s=quiet_s)
        self.secrets = Secrets()

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.done(False, "没说结果" if exc[0] is None else "出错了: " + exc[0].__name__)


def mask_user(user: str) -> str:
    """给人看的账号名: 邮箱留首字母和域名, 手机号留头 3 尾 4, 别的留首字母."""
    s = str(user or "").strip()
    if not s:
        return ""
    if "@" in s:
        name, _, domain = s.partition("@")
        return f"{name[:1]}{'*' * min(6, max(1, len(name) - 1))}@{domain}"
    if s.isdigit() and len(s) >= 8:
        return f"{s[:3]}{'*' * (len(s) - 7)}{s[-4:]}"
    return f"{s[:1]}{'*' * min(6, max(1, len(s) - 1))}"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Vault:
    def __init__(self, home: Path | None = None, clock: Callable[[], float] = time.time,
                 on_want: Callable[[dict, dict, str], None] | None = None):
        self.home = Path(home or HOME)
        self.clock = clock
        self.on_want = on_want  # (账号公开视图, 用途, 谁要用) -> None; 可能在任何线程里被调, 由对方转到事件循环里弹卡片
        self._lock = threading.RLock()
        self._wanting: dict[tuple[str, str], float] = {}

    # -- 文件 ----------------------------------------------------------------
    def _path(self, aid: str) -> Path:
        if not _ID.fullmatch(str(aid or "")):
            raise VaultError("config", "账号编号不对")
        return self.home / "accounts" / f"{aid}.json"

    def _load(self, aid: str) -> dict | None:
        """账号文件 -> dict; 没有返回 None; 文件坏了抛 VaultError(config)."""
        path = self._path(aid)
        try:
            data = _read_json(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise VaultError("config", f"账号文件坏了 ({aid}): {fix_hint()}") from None
        if not isinstance(data, dict) or data.get("platform") not in PLATFORMS or data.get("id") != aid:
            raise VaultError("config", f"账号文件坏了 ({aid}): {fix_hint()}")
        return data

    def _ids(self) -> list[str]:
        folder = self.home / "accounts"
        try:
            return sorted(p.stem for p in folder.glob("*.json") if _ID.fullmatch(p.stem))
        except OSError:
            return []

    def _flags_all(self) -> dict:
        try:
            data = _read_json(self.home / "state.json")
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def flags(self, aid: str) -> dict:
        """运行时的标记 (比如"这个密码被拒过"): 不是机密, 公开视图里有."""
        v = self._flags_all().get(aid)
        return dict(v) if isinstance(v, dict) else {}

    def set_flags(self, aid: str, **kv) -> None:
        """None 的键删掉."""
        with self._lock:
            data = self._flags_all()
            cur = dict(data.get(aid) or {})
            for k, v in kv.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            if cur:
                data[aid] = cur
            else:
                data.pop(aid, None)
            _write_json(self.home / "state.json", data)

    # -- 看 ------------------------------------------------------------------
    def public(self, acct: dict) -> dict:
        """账号的公开视图: 没有机密, 账号名打过码."""
        plat = PLATFORMS[acct["platform"]]
        flags = self.flags(acct["id"])
        used = self.last_used(acct["id"])
        purposes = []
        for p in plat.purposes:
            cur = (acct.get("purposes") or {}).get(p.id) or {}
            state = cur.get("state") if cur.get("state") in STATES else "ask"
            purposes.append({"id": p.id, "name": p.name, "desc": p.desc, "risk": p.risk, "ui_on": p.ui_on,
                             "state": state, "at": cur.get("at"), "by": cur.get("by", ""),
                             "waiting": state == "ask" and (acct["id"], p.id) in self._wanting})
        return {"id": acct["id"], "platform": plat.id, "platform_name": plat.name, "label": acct.get("label") or plat.name,
                "login": plat.login, "user": acct.get("user_mask", ""),
                "secrets": [{"name": n, "label": lbl, "set": bool((acct.get("secrets") or {}).get(n))} for n, lbl in plat.secrets],
                "meta": {k: v for k, v in (acct.get("meta") or {}).items() if k in META_KEYS},
                "purposes": purposes, "created": acct.get("created"), "updated": acct.get("updated"),
                "last_used": used, "flags": {k: v for k, v in flags.items() if k in ("rejected_at", "expired_at", "note")},
                "rejected": "rejected_for" in flags and flags.get("rejected_for") == acct.get("updated")}

    def accounts(self) -> list[dict]:
        """所有账号的公开视图 (坏掉的也列出来, broken 里写原因)."""
        out = []
        for aid in self._ids():
            try:
                acct = self._load(aid)
            except VaultError as exc:
                out.append({"id": aid, "platform": "", "platform_name": "", "label": aid, "broken": exc.text, "secrets": [],
                            "purposes": [], "meta": {}, "user": "", "flags": {}})
                continue
            if acct is not None:
                out.append(self.public(acct))
        order = list(PLATFORMS)
        out.sort(key=lambda a: (order.index(a["platform"]) if a["platform"] in order else 99, a["id"]))
        return out

    def get(self, aid: str) -> dict | None:
        acct = self._load(aid)
        return None if acct is None else self.public(acct)

    def last_used(self, aid: str) -> dict | None:
        for rec in reversed(self.log_tail(60)):
            if rec.get("account") == aid and rec.get("kind") == "use" and rec.get("ok") is not None:
                return {"t": rec.get("t"), "purpose": rec.get("purpose"), "ok": rec.get("ok")}
        return None

    # -- 存 ------------------------------------------------------------------
    def put(self, platform: str, fields: dict, *, label: str = "", user: str = "", meta: dict | None = None,
            purposes: dict | None = None, by: str = "page", aid: str | None = None) -> dict:
        """存 (或换掉) 一个账号的机密. fields: 字段名 -> 值, 只收这个平台有的字段; 同一个账号再存时没给的字段原样留着.
        purposes: 用途 -> True (允许) / False (还没定, 第一次用时问) / "allow" / "ask" / "deny"; 没给的保持原状 (新账号是 ask).
        返回公开视图."""
        plat = PLATFORMS.get(platform)
        if plat is None:
            raise VaultError("config", "不认识这个平台")
        known = dict(plat.secrets)
        clean: dict[str, str] = {}
        for name, value in (fields or {}).items():
            if name not in known:
                raise VaultError("config", f"{plat.name} 没有「{name}」这一项")
            value = str(value or "").rstrip("\r\n")
            if not value:
                continue
            if _CTRL.search(value) or "\n" in value or "\r" in value:
                raise VaultError("config", f"「{known[name]}」里有换行或控制字符, 是不是粘贴错了")
            if len(value) > FIELD_MAX.get(name, SECRET_MAX):
                raise VaultError("config", f"「{known[name]}」太长了")
            clean[name] = value
        if not clean:
            raise VaultError("config", "没填东西")
        with self._lock:
            ids = self._ids()
            if aid is None:
                aid = plat.id if plat.single else f"{plat.id}-{_secrets.token_hex(3)}"
            try:
                old = self._load(aid)
            except VaultError:
                old = None  # 坏掉的文件直接换新的
            if old is None and aid not in ids and len(ids) >= MAX_ACCOUNTS:
                raise VaultError("limit", f"最多存 {MAX_ACCOUNTS} 个账号: 先删掉不用的")
            if old is not None and old["platform"] != plat.id:
                raise VaultError("config", "账号编号被别的平台占了")
            now = self.clock()
            data = old or {"v": 1, "id": aid, "platform": plat.id, "created": now, "secrets": {}, "meta": {}, "purposes": {}}
            for name, value in clean.items():
                data["secrets"][name] = base64.b64encode(_dpapi(value.encode("utf-8"), True, f"{aid}:{name}")).decode("ascii")
            if user:
                data["user"] = base64.b64encode(_dpapi(str(user).strip()[:120].encode("utf-8"), True, f"{aid}:user")).decode("ascii")
                data["user_mask"] = mask_user(user)
            if label or "label" not in data:
                data["label"] = (str(label).strip()[:40] or plat.name)
            if meta:
                for k, v in meta.items():
                    if k in META_KEYS and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 80:
                        data["meta"][k] = v
            known_purposes = {p.id for p in plat.purposes}
            for pid, want in (purposes or {}).items():
                if pid not in known_purposes:
                    continue
                state = want if want in STATES else ("allow" if want is True else "ask")
                data["purposes"][pid] = {"state": state, "at": now, "by": by}
            for pid in known_purposes:
                data["purposes"].setdefault(pid, {"state": "ask", "at": now, "by": "default"})
            data["updated"] = now
            _write_json(self._path(aid), data)
            self.set_flags(aid, rejected_for=None, rejected_at=None, expired_at=None, note=None)  # 新存的: 以前"被拒过""过期了"的标记作废
        self.record(aid, "", by, True, "存了" if old is None else "换了新的", kind="change")
        return self.public(data)

    def update_meta(self, aid: str, **meta) -> None:
        """只改非机密的附加信息 (比如核对过 B 站登录还有效), 不动机密和 updated."""
        with self._lock:
            data = self._load(aid)
            if data is None:
                return
            for k, v in meta.items():
                if k in META_KEYS and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 80:
                    data["meta"][k] = v
            _write_json(self._path(aid), data)

    def forget(self, aid: str, *, by: str = "page") -> bool:
        """删掉一个账号 (机密和用途一起没了; 使用记录留着). 没有这个账号返回 False."""
        with self._lock:
            path = self._path(aid)
            if not path.exists():
                return False
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            self.set_flags(aid, rejected_for=None, rejected_at=None, expired_at=None, note=None)
            for key in [k for k in self._wanting if k[0] == aid]:
                self._wanting.pop(key, None)
        self.record(aid, "", by, True, "删了", kind="change")
        return True

    def set_purpose(self, aid: str, purpose: str, state: str, *, by: str = "page") -> dict:
        if state not in STATES:
            raise VaultError("config", "用途只有 允许 / 先问我 / 不允许 三种")
        with self._lock:
            data = self._load(aid)
            if data is None:
                raise VaultError("config", "没有这个账号")
            if purpose not in {p.id for p in PLATFORMS[data["platform"]].purposes}:
                raise VaultError("config", "这个账号没有这个用途")
            data["purposes"][purpose] = {"state": state, "at": self.clock(), "by": by}
            _write_json(self._path(aid), data)
            self._wanting.pop((aid, purpose), None)
        self.record(aid, purpose, by, True, {"allow": "允许了", "ask": "改成先问我", "deny": "不允许了"}[state], kind="change")
        return self.public(data)

    # -- 用 ------------------------------------------------------------------
    def lease(self, aid: str, purpose: str, *, by: str, explicit: bool = False) -> Lease:
        """要用一个账号做一件事. 没有这个账号、用途还没点头 (ask)、被拒 (deny)、解不开: ok=False, why 说原因.
        explicit: 主人自己在页面上点了按钮 (这一下就是点头), 不查用途的三态. 用完调 done()."""
        try:
            data = self._load(aid)
        except VaultError as exc:
            self.record(aid, purpose, by, False, "账号文件坏了")
            return Lease(self, aid, purpose, by, False, "broken", text=exc.text)
        if data is None:
            return Lease(self, aid, purpose, by, False, "none")
        if purpose not in {p.id for p in PLATFORMS[data["platform"]].purposes} and not explicit:
            self.record(aid, purpose, by, False, "没有这个用途")
            return Lease(self, aid, purpose, by, False, "unknown")
        state = ((data.get("purposes") or {}).get(purpose) or {}).get("state", "ask")
        if not explicit and state != "allow":
            if state == "deny":
                self.record(aid, purpose, by, False, "主人没允许, 没用", quiet_s=600)
                return Lease(self, aid, purpose, by, False, "deny")
            self.record(aid, purpose, by, None, "等主人点头", quiet_s=600)
            self._want(data, purpose, by)
            return Lease(self, aid, purpose, by, False, "ask")
        try:
            secrets = Secrets()
            for name in (data.get("secrets") or {}):
                try:
                    blob = base64.b64decode(str(data["secrets"][name]), validate=True)
                except ValueError:
                    raise VaultError("secret", f"存好的账号坏了: {fix_hint()}") from None
                secrets[name] = _dpapi(blob, False, f"{aid}:{name}").decode("utf-8")
        except (VaultError, UnicodeDecodeError) as exc:
            if isinstance(exc, UnicodeDecodeError):
                exc = VaultError("secret", f"存好的账号解不开: {fix_hint()}")
            self.record(aid, purpose, by, False, "解不开")
            return Lease(self, aid, purpose, by, False, "broken", text=exc.text)
        return Lease(self, aid, purpose, by, True, secrets=secrets)

    def user_of(self, aid: str) -> str:
        """账号名 (generic 账号才有), 只给要用它的程序; 不对外."""
        data = self._load(aid)
        if data is None or not data.get("user"):
            return ""
        return _dpapi(base64.b64decode(data["user"]), False, f"{aid}:user").decode("utf-8")

    def _want(self, acct: dict, purpose: str, by: str) -> None:
        key = (acct["id"], purpose)
        now = self.clock()
        with self._lock:
            last = self._wanting.get(key)
            if last is not None and now - last < WANT_AGAIN_S:
                return
            self._wanting[key] = now
        cb = self.on_want
        if cb is None:
            return
        view = self.public(acct)
        info = next((p for p in view["purposes"] if p["id"] == purpose), {"id": purpose, "name": purpose, "desc": ""})
        try:
            cb(view, info, by)
        except Exception:  # 卡片没弹出来不能连累用它的程序; 页面上照样看得到"等你点头"
            log.exception("保管箱的申请没发出去")

    def unwant(self, aid: str, purpose: str) -> None:
        self._wanting.pop((aid, purpose), None)

    # -- 使用记录 ------------------------------------------------------------------
    def record(self, aid: str, purpose: str, by: str, ok: bool | None, note: str = "", quiet_s: float = 0,
               kind: str = "use") -> None:
        """记一条 (不带密码). kind: use (用了 / 想用) 或 change (存了、删了、改了用途). quiet_s: 同一个账号、用途、结果的记录
        这么多秒内不重复记 (一直在等点头时别刷屏)."""
        rec = {"t": round(self.clock(), 3), "account": aid, "purpose": purpose, "by": str(by)[:24], "ok": ok,
               "note": str(note)[:120], "kind": kind}
        with self._lock:
            try:
                if quiet_s:
                    last = next((r for r in reversed(self.log_tail(8)) if r.get("account") == aid and r.get("purpose") == purpose
                                 and r.get("by") == rec["by"]), None)
                    if last and last.get("ok") == ok and last.get("note") == rec["note"] and rec["t"] - float(last.get("t") or 0) < quiet_s:
                        return
                path = self.home / "log.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if path.stat().st_size > LOG_KEEP * 260:
                    lines = path.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    os.replace(tmp, path)
            except OSError as exc:
                log.warning("保管箱的使用记录没写上: %s", type(exc).__name__)

    def log_tail(self, n: int = 50) -> list[dict]:
        try:
            lines = (self.home / "log.jsonl").read_text(encoding="utf-8").splitlines()[-max(1, int(n)):]
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
        return out


# -- 工作台守门 ------------------------------------------------------------------------
def protect(store, home: Path | None = None) -> tuple[bool, str]:
    """保管箱目录和 /api/vault 加进工作台的黑名单 (约束设置里看得见、能改; 所有档位都挡). 做法和 mailbox.protect 一样:
    你删掉、停用过的不再加. store 是 wm.agent.governance.BlacklistStore. 返回 (目录现在挡不挡得住, 说明)."""
    home = Path(home or HOME).resolve()
    rules = store.list()
    removed = [e.get("before") or {} for e in store.audit(1000) if e.get("action") == "remove" and e.get("by") == "user"]
    want = [({"kind": "path", "value": str(home), "operations": "all",
              "reason": "账号保管箱里的密码和令牌只给主人: 工作台不读不改 (要调试先在这里停用)"}, "目录"),
            ({"kind": "command", "value": "/api/vault",
              "reason": "账号保管箱只由主人在页面上操作: 工作台的命令不碰 /api/vault (要调试先在这里停用)"}, "接口")]
    notes, ok = [], True
    for rule, label in want:
        same = [r for r in rules if r.get("kind") == rule["kind"] and _same_rule(r["value"], rule["value"], rule["kind"])]
        if any(r.get("enabled") for r in same):
            notes.append(f"{label}已在保护名单里")
            continue
        if same or any(b.get("kind") == rule["kind"] and _same_rule(str(b.get("value", "")), rule["value"], rule["kind"])
                       for b in removed):
            ok = ok and label != "目录"
            notes.append(f"{label}的保护你以前停用 / 删掉过, 没再加")
            continue
        try:
            store.add(rule, by="user", evidence="用账号保管箱时加的 (服务端启动 / 存第一个账号): 密码和令牌只给主人")
            notes.append(f"{label}加进了工作台的保护名单")
        except ValueError as exc:
            ok = ok and label != "目录"
            notes.append(f"{label}没加进保护名单: {exc}")
    return ok, "; ".join(notes)


def protected(store, home: Path | None = None) -> bool:
    """工作台的黑名单现在挡不挡这个目录."""
    if store is None:
        return False
    home = Path(home or HOME).resolve()
    try:
        return any(r.get("enabled") and r.get("kind") == "path" and r.get("operations", "all") == "all"
                   and _inside(home, Path(r["value"])) for r in store.list())
    except (OSError, ValueError, KeyError):
        return False


def _same_rule(a: str, b: str, kind: str) -> bool:
    if kind == "path":
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def _inside(path: Path, root: Path) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)
