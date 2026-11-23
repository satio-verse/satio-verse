"""绫音读主人的邮箱 (2026-11-01 用户: "桌宠能不能自行读取邮箱内容……并根据邮箱内容进行整理、汇报，垃圾邮箱会忽略之类的";
用的是 QQ 邮箱, 汇报走自动: 你最近在微信上找过她就发微信, 否则 QQ 私聊, 桌宠对话栏里也显示一条).

这一份管"怎么读、怎么分": 账号和授权码怎么存、只读的 IMAP、拆信、按规则分拣. 什么时候说、怎么说见 mailbox_report.py,
本机接口、连接邮箱的页面 (/mail) 和挂接见 mailbox_http.py; 主人在页面上 (或终端里的 tools/mail_setup.py) 填授权码.

- 只读: 文件夹只用 EXAMINE 打开 (只读), 取信只用 BODY.PEEK (不标已读). 连接一建好就套上 guard(): 只放 CAPABILITY / LOGIN /
  ID / LIST / STATUS / EXAMINE / UID SEARCH / UID FETCH (带 PEEK) / NOOP / LOGOUT, 别的命令 (SELECT、STORE、COPY、MOVE、
  EXPUNGE、APPEND、DELETE…) 在发出去之前就抛 ReadOnlyViolation. 不标已读、不删、不移动、不发信.
- 授权码: 只由主人自己填 (连接邮箱的页面的密码框, 或终端里的 tools/mail_setup.py, 都不经过聊天), Windows DPAPI 加密 (这台电脑的这个 Windows 用户才解得开,
  和微信的 token 一样); 不进记忆库、日志、知识库, 接口、状态、读信记录里都没有. 最多 MAX_ACCOUNTS 个邮箱 (2026-11-01 用户:
  "还要加入gmail等邮箱"): 第一个在 ~/.worldmodel/mail/account.json (第一版就这一个), 后加的在 accounts/<地址>.json.
  设置邮箱时这个目录加进工作台的保护名单 (protect), 工作台的工具读不到、命令碰不到.
- 代理: 国外的邮箱 (Gmail、iCloud、Yahoo…) 国内常连不上, proxy=auto 时经这台电脑设的 HTTP 代理 (环境变量或 Windows 系统代理)
  打 CONNECT 隧道再握 TLS (证书照样校验); 国内的直连. 只用标准库, 不装 socks 之类的库.
- 读哪些: 默认只读收件箱; 垃圾箱、广告 / 订阅文件夹不读, 只用 STATUS 数新进了几封. 第一次只看最近 first_run_hours 小时的,
  不翻旧账; 之后按 UID 只看新来的 (UIDVALIDITY 变了就重新起头). 每封先只取信头, 一看就是推送的不取正文, 剩下的只取开头
  BODY_BYTES 字节 (大附件不下). 正文只在内存里用, 不存盘.
- 分拣先用规则 (classify): 你说不看的发件人 / 字 → 不说; 垃圾标记 → 垃圾; 退订头、Precedence: bulk、群发平台的信头、营销发件人、
  广告字样、正文里的退订 → 推送 (不说); 剩下的按发件人和关键词分 要紧 (urgent, 单独马上说) / 要看 (important, 攒着一起说) /
  普通 (low, 只数个数). 你加的重要发件人最优先. 发件验证没过、名字里冒充别的地址的, 汇报时带一句提醒.
- QQ 邮箱: imap.qq.com:993, 邮箱地址 + 授权码 (QQ 邮箱网页版 → 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码, 不是 QQ 密码).
  网易 (163 / 126 / yeah / VIP / 188) 登录后要先报一下客户端 (ID 命令, RFC 2971), 不然打开文件夹会被拒 ("Unsafe Login").
  Gmail: 两步验证 + 应用专用密码; 它自己分到"推广""社交"的信 (X-GM-RAW 搜索, 只读) 直接算推送.
  微软的个人邮箱 (Outlook / Hotmail / Live) 已经不让用密码或授权码走 IMAP, 只认 OAuth, 这一版不接.
"""
from __future__ import annotations

import base64
import codecs
import contextlib
import ctypes
import email
import email.header
import email.utils
import hashlib
import imaplib
import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy as email_policy
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

HOME = Path.home() / ".worldmodel" / "mail"  # 账号 (加密的授权码)、设置、读到哪了、读信记录; 测试换成临时目录
PAGE_URL = "http://127.0.0.1:8790/mail"  # 连接邮箱的页面 (工作台左栏的「邮箱」); mailbox_http.register 按 server.port 改
BODY_BYTES = 32 * 1024  # 正文最多取开头多少字节 (BODY.PEEK[]<0.N>): 够看清是什么事, 大附件不下
TEXT_CHARS = 1500       # 拆出来的正文留多少字 (只在内存里)
MAX_PER_POLL = 100      # 一次最多看几封新信 (多的只记个数, 不补)
TIMEOUT_S = 30.0
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # IMAP 的日期 (别用 %b: 跟着系统语言)

# 各家的 IMAP (按公开的设置写的, 没逐个实测; 填错了 setup 试登录时就会说连不上, 用 --host 改). id: 登录后要报一下客户端 (网易);
# overseas: 国外的服务, 国内常要走代理 (proxy=auto 时用这台电脑设的代理); guide: 授权码在哪生成 (GUIDE 里的键)
PRESETS = {
    "qq": {"name": "QQ 邮箱", "short": "QQ邮箱", "host": "imap.qq.com", "domains": ("qq.com", "vip.qq.com", "foxmail.com")},
    "exmail": {"name": "腾讯企业邮", "short": "企业邮", "host": "imap.exmail.qq.com", "domains": ()},
    "163": {"name": "网易 163", "short": "163", "host": "imap.163.com", "domains": ("163.com",), "id": True, "guide": "163"},
    "126": {"name": "网易 126", "short": "126", "host": "imap.126.com", "domains": ("126.com",), "id": True, "guide": "163"},
    "yeah": {"name": "网易 yeah.net", "short": "yeah", "host": "imap.yeah.net", "domains": ("yeah.net",), "id": True,
             "guide": "163"},
    "vip163": {"name": "网易 VIP 邮箱", "short": "163VIP", "host": "imap.vip.163.com", "domains": ("vip.163.com",), "id": True,
               "guide": "163"},
    "188": {"name": "网易 188 邮箱", "short": "188", "host": "imap.188.com", "domains": ("188.com",), "id": True, "guide": "163"},
    "sina": {"name": "新浪邮箱", "short": "新浪", "host": "imap.sina.com", "domains": ("sina.com",), "guide": "cn"},
    "sohu": {"name": "搜狐邮箱", "short": "搜狐", "host": "imap.sohu.com", "domains": ("sohu.com",), "guide": "cn"},
    "aliyun": {"name": "阿里邮箱", "short": "阿里", "host": "imap.aliyun.com", "domains": ("aliyun.com",), "guide": "cn"},
    "139": {"name": "139 邮箱", "short": "139", "host": "imap.139.com", "domains": ("139.com",), "guide": "cn"},
    "189": {"name": "189 邮箱", "short": "189", "host": "imap.189.cn", "domains": ("189.cn",), "guide": "cn"},
    "gmail": {"name": "Gmail", "short": "Gmail", "host": "imap.gmail.com", "domains": ("gmail.com", "googlemail.com"),
              "overseas": True},
    "icloud": {"name": "iCloud 邮件", "short": "iCloud", "host": "imap.mail.me.com", "domains": ("icloud.com", "me.com", "mac.com"),
               "overseas": True},
    "yahoo": {"name": "Yahoo 邮箱", "short": "Yahoo", "host": "imap.mail.yahoo.com", "domains": ("yahoo.com",), "overseas": True},
    "yandex": {"name": "Yandex 邮箱", "short": "Yandex", "host": "imap.yandex.com", "domains": ("yandex.com", "yandex.ru"),
               "overseas": True, "guide": "app"},
    "zoho": {"name": "Zoho Mail", "short": "Zoho", "host": "imap.zoho.com", "domains": ("zoho.com",), "overseas": True,
             "guide": "app"},
}
GUIDE_URL = {  # 去哪生成授权码 / 应用专用密码 (连接邮箱的页面上那个"打开"按钮)
    "qq": "https://mail.qq.com/", "exmail": "https://exmail.qq.com/", "163": "https://mail.163.com/",
    "126": "https://mail.126.com/", "yeah": "https://mail.yeah.net/", "vip163": "https://vip.163.com/",
    "sina": "https://mail.sina.com.cn/", "sohu": "https://mail.sohu.com/", "aliyun": "https://mail.aliyun.com/",
    "139": "https://mail.10086.cn/", "189": "https://mail.189.cn/", "gmail": "https://myaccount.google.com/apppasswords",
    "icloud": "https://account.apple.com/", "yahoo": "https://login.yahoo.com/account/security",
    "yandex": "https://id.yandex.com/", "zoho": "https://accounts.zoho.com/",
}
_OAUTH_ONLY = re.compile(r"^(outlook|hotmail|live|msn)\.[a-z.]+$")  # 微软的个人邮箱: 只认 OAuth
GUIDE = {  # 授权码在哪生成 (setup 时给你看)
    "qq": "QQ 邮箱网页版 → 设置 → 账号 (新版在「账号与安全」) → 开启「IMAP/SMTP 服务」→ 按提示验证 → 得到 16 位授权码. "
          "授权码不是 QQ 密码; 不用了可以在同一个地方停用它.",
    "163": "网易邮箱网页版 → 设置 → POP3/SMTP/IMAP → 开启「IMAP/SMTP 服务」→ 按提示验证 → 得到授权码 (不是登录密码).",
    "cn": "邮箱网页版的设置里开启 IMAP, 按提示生成授权码 (也叫客户端专用密码, 不是登录密码).",
    "gmail": "先在 Google 账号里开两步验证, 再到 https://myaccount.google.com/apppasswords 生成一个「应用专用密码」(16 位, "
             "空格无所谓). IMAP 在 Gmail 设置 → 转发和 POP/IMAP 里 (新版默认开着). 公司 / 学校的账号如果管理员关了应用专用密码"
             "就用不了 (那只能 OAuth, 这一版没接).",
    "icloud": "Apple 账户 (account.apple.com) → 登录与安全 → App 专用密码 → 生成一个 (要先开双重认证).",
    "yahoo": "Yahoo 账号 → 账号安全 → 生成应用密码.",
    "app": "账号的安全设置里开两步验证, 生成一个应用专用密码; 邮箱设置里打开 IMAP.",
}

DEFAULTS = {
    "enabled": False,          # 读不读 (设置邮箱时问你; 关了一封都不读)
    "interval_min": 20,        # 隔多久看一次新信
    "via": "auto",             # 汇报发到哪: auto / pet / chat (见 mailbox_report.py)
    "summary": "off",          # 一句话摘要: off 按规则 / local 本机模型 / cloud 云端模型 (cloud 只能在本机终端里开)
    "max_items": 3,            # 一份汇总最多念几件
    "report_gap_min": 120,     # 两份汇总至少隔多久
    "quiet": "23:00-08:00",    # 免打扰 (她的时区): 只攒不说; 空串 = 不设
    "quiet_urgent": True,      # 免打扰时要紧的照说
    "folders": ["INBOX"],      # 读哪些文件夹
    "first_run_hours": 24,     # 第一次只看最近多少小时的
    "important_senders": [],   # 你说重要的发件人 (完整地址, 或 @域名)
    "ignore_senders": [],      # 你说不看的发件人
    "important_keywords": [],  # 主题、正文开头有这些字就算要看
    "ignore_keywords": [],     # 有这些字就不看
}
VIA = ("auto", "pet", "chat")
SUMMARY = ("off", "local", "cloud")
_RANGES = {"interval_min": (5, 1440), "max_items": (1, 10), "report_gap_min": (0, 1440), "first_run_hours": (1, 168)}
_LISTS = {"important_senders": (200, 120), "ignore_senders": (200, 120), "important_keywords": (100, 30),
          "ignore_keywords": (100, 30)}  # 最多几条, 每条最多几个字
_QUIET = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
_ADDR = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}$")
_HOST = re.compile(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+)$")


def fix_hint(what: str = "重新填一下授权码") -> str:
    """出了错让你去哪修: 连接邮箱的页面 (装好的电脑上不一定有 python 命令, 安装包也不带 tools/mail_setup.py), 终端那条备选."""
    return f"打开 {PAGE_URL} (工作台左栏的「邮箱」) {what}; 也可以在终端里跑 python tools/mail_setup.py setup"


class MailError(Exception):
    """读信没成. kind: config (没设置、文件坏了) / secret (授权码解不开) / auth (登录被拒) / network (连不上、超时) /
    server (服务器回了错) / readonly (守门拦下了会改邮箱的命令). text 是给人看的一句, 不带授权码."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind, self.text = kind, text


# -- 账号和授权码 ----------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


_ENTROPY = b"WorldModel-mail\x00"  # 和微信、桌宠设置的密文分开: 别处的密文拿到这里解不开


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """Windows DPAPI 加密 / 解密 (这台电脑的这个 Windows 用户才解得开). 不用 pywin32, 免得便携包缺它."""
    if os.name != "nt":
        raise MailError("secret", "授权码用 Windows 的 DPAPI 加密, 只能在 Windows 上用")
    crypt, kernel = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p if encrypt else ctypes.c_void_p, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    src = (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\0")
    salt = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    out = _Blob()
    if not fn(ctypes.byref(_Blob(len(data), src)), "WorldModel mail" if encrypt else None,
              ctypes.byref(_Blob(len(_ENTROPY), salt)), None, None, 0x1, ctypes.byref(out)):  # 0x1: 不弹窗
        raise MailError("secret", f"存好的授权码解不开 (换了电脑或 Windows 用户?): {fix_hint()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.memset(out.pbData, 0, out.cbData)
        kernel.LocalFree(out.pbData)


def preset_for(address: str) -> tuple[str, dict]:
    """按邮箱的域名认是哪家, 认不出返回 ("", {}). 微软个人邮箱抛 ValueError (只认 OAuth, 这一版不接)."""
    domain = str(address or "").rsplit("@", 1)[-1].strip().lower()
    if _OAUTH_ONLY.match(domain):
        raise ValueError("微软的个人邮箱 (Outlook / Hotmail / Live / MSN) 已经不让用密码或授权码走 IMAP 了, 只能 OAuth 登录 "
                         "(要先在微软 Azure 里注册一个应用, 再在浏览器里授权), 这一版还没接")
    for key, preset in PRESETS.items():
        if domain in preset["domains"]:
            return key, preset
    return "", {}


PAGE_STEPS = {  # 页面上的说法: 配着"打开"那个按钮, 一步一步来
    "qq": "点下面的按钮打开 QQ 邮箱 → 设置 → 账号（新版在「账号与安全」）→ 开启「IMAP/SMTP 服务」→ 按提示发短信验证 → "
          "复制 16 位授权码，粘贴到下面。授权码不是 QQ 密码。",
    "163": "点下面的按钮打开网易邮箱 → 设置 → POP3/SMTP/IMAP → 开启「IMAP/SMTP 服务」→ 按提示验证 → 复制授权码，粘贴到下面。",
    "cn": "点下面的按钮打开邮箱网页版，在设置里开启 IMAP、生成授权码（也叫客户端专用密码），复制回来粘贴到下面。",
    "gmail": "Google 账号要先开「两步验证」。然后点下面的按钮生成一个「应用专用密码」（名字随便填，比如 绫音），"
             "把那 16 位复制回来粘贴到下面，空格不用管。",
    "icloud": "Apple 账户要先开双重认证。点下面的按钮 → 登录与安全 → App 专用密码 → 生成一个，复制回来粘贴到下面。",
    "yahoo": "点下面的按钮打开 Yahoo 账号安全页 → 生成应用密码，复制回来粘贴到下面。",
    "app": "点下面的按钮打开账号设置：开两步验证、生成应用专用密码，复制回来粘贴到下面；邮箱设置里要打开 IMAP。",
}
_URL_LABEL = {"gmail": "去生成应用专用密码 ↗", "icloud": "去生成 App 专用密码 ↗", "yahoo": "去生成应用密码 ↗"}


def providers() -> list[dict]:
    """连接邮箱的页面用: 每家叫什么、哪些域名、服务器、授权码怎么弄 (steps)、去哪弄 (url, url_label)、要不要走代理."""
    out = []
    for key, p in PRESETS.items():
        guide = p.get("guide") or key
        out.append({"key": key, "name": p["name"], "short": p["short"], "domains": list(p["domains"]), "host": p["host"],
                    "guide": GUIDE.get(guide) or GUIDE["cn"], "steps": PAGE_STEPS.get(guide) or PAGE_STEPS["cn"],
                    "url": GUIDE_URL.get(key, ""), "url_label": _URL_LABEL.get(key) or f"打开{p['short']}网页版 ↗",
                    "overseas": bool(p.get("overseas"))})
    return out


def label_of(account: dict, accounts: list[dict] | None = None) -> str:
    """汇报里怎么称呼这个邮箱: QQ邮箱、Gmail…; 同一家有两个的, 用打码的地址分开."""
    short = PRESETS.get(account.get("preset") or "", {}).get("short") or str(account.get("address", "")).rsplit("@", 1)[-1]
    same = [a for a in accounts or [] if (PRESETS.get(a.get("preset") or "", {}).get("short")
                                          or str(a.get("address", "")).rsplit("@", 1)[-1]) == short]
    return mask_address(account.get("address", "")) if len(same) > 1 else short


def check_address(address: str) -> str:
    address = str(address or "").strip()
    if not _ADDR.match(address):
        raise ValueError("邮箱地址不对: 写成 名字@qq.com 这样")
    return address


def mask_address(address: str) -> str:
    """给人看的邮箱: 名字只留头三个字."""
    user, _, domain = str(address or "").partition("@")
    return f"{user[:3]}***@{domain}" if domain else ("***" if user else "")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """先写临时文件再换过去: 写到一半断电、两个进程一起写, 都不会留下半个文件."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


MAX_ACCOUNTS = 5
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def account_files(home: Path | None = None) -> list[Path]:
    """存账号的文件: 第一个邮箱在 account.json (第一版就这一个, 还没重启的旧服务端也认), 后加的在 accounts/<地址>.json."""
    home = Path(home or HOME)
    files = [home / "account.json"] if (home / "account.json").exists() else []
    with contextlib.suppress(OSError):
        files += sorted(p for p in (home / "accounts").glob("*.json") if p.is_file())
    return files


def load_accounts(home: Path | None = None) -> list[dict]:
    """存好的邮箱 (授权码还是密文), 每个带 _path (存在哪个文件, 不存盘). 读不出来的文件也列出来 (_broken 写原因),
    交给读信那边说一次; 同一个地址只算一次, 最多 MAX_ACCOUNTS 个."""
    out, seen = [], set()
    for path in account_files(home):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("address") or not data.get("secret") or not data.get("host"):
                raise ValueError("缺东西")
        except (OSError, ValueError):
            out.append({"address": path.stem if path.name != "account.json" else "account.json", "_path": str(path),
                        "_broken": f"{path.name} 读不出来 (被改坏了?): {fix_hint('重新连一次这个邮箱')}"})
            continue
        key = str(data["address"]).lower()
        if key not in seen:
            seen.add(key)
            out.append({**data, "_path": str(path)})
    return out[:MAX_ACCOUNTS]


def account_id(address: str) -> str:
    """接口、页面上指一个邮箱用的编号 (地址的哈希前 12 位): 状态里地址是打码的, 拿它来说删哪个、测哪个."""
    return hashlib.sha256(str(address).strip().lower().encode("utf-8")).hexdigest()[:12]


def address_of(home: Path | None, ref: str) -> str | None:
    """编号或地址 -> 存着的那个邮箱的地址 (没有返回 None)."""
    ref = str(ref or "").strip().lower()
    return next((a["address"] for a in load_accounts(home) if ref in (a["address"].lower(), account_id(a["address"]))), None)


def find_account(home: Path | None, address: str | None = None) -> dict | None:
    """按地址找一个 (不分大小写); 不给地址就是第一个."""
    accounts = [a for a in load_accounts(home) if not a.get("_broken")]
    if address is None:
        return accounts[0] if accounts else None
    return next((a for a in accounts if a["address"].lower() == str(address).strip().lower()), None)


def load_account(home: Path | None = None) -> dict | None:
    """第一个邮箱 (第一版只有一个时的写法). 没设置返回 None; 文件坏了抛 MailError(config)."""
    accounts = load_accounts(home)
    if accounts and accounts[0].get("_broken"):
        raise MailError("config", accounts[0]["_broken"])
    return accounts[0] if accounts else None


def save_account(home: Path | None, address: str, secret: str, host: str = "", port: int = 0, proxy: str = "auto",
                 tls: bool = True) -> dict:
    """存一个邮箱 (同一个地址就换掉那一份, 别的不动): 授权码 DPAPI 加密. 返回不带授权码的那份.
    proxy: auto (国外的邮箱用这台电脑设的代理) / off / http://主机:端口. tls=False 只给本机的邮件网桥 (127.0.0.1) 用."""
    home = Path(home or HOME)
    address = check_address(address)
    secret = re.sub(r"\s+", "", str(secret or ""))  # Gmail 的应用专用密码显示时带空格
    if not secret:
        raise ValueError("授权码是空的")
    key, preset = preset_for(address)
    host = str(host or preset.get("host") or "").strip().lower()
    if not _HOST.match(host):
        raise ValueError("邮箱服务器地址不对 (QQ 邮箱是 imap.qq.com)")
    port = int(port or 993)
    if not 0 < port < 65536:
        raise ValueError("端口不对")
    proxy = check_proxy(proxy)
    if not tls and host not in LOOPBACK:
        raise ValueError("不加密的连接只给本机的邮件网桥用 (服务器得是 127.0.0.1)")
    accounts = load_accounts(home)
    same = next((a for a in accounts if a["address"].lower() == address.lower()), None)  # 坏掉的 accounts/ 文件按文件名认
    if same is None and len([a for a in accounts if not a.get("_broken")]) >= MAX_ACCOUNTS:
        raise ValueError(f"最多 {MAX_ACCOUNTS} 个邮箱: 先删掉一个 ({PAGE_URL} 的「已连接的邮箱」里点删除; "
                         "终端里是 python tools/mail_setup.py forget 地址)")
    data = {"address": address, "host": host, "port": port, "preset": key, "id_command": bool(preset.get("id")),
            "proxy": proxy, "secret": base64.b64encode(_dpapi(secret.encode("utf-8"), True)).decode("ascii"),
            "saved_at": time.time(), **({} if tls else {"tls": False})}
    legacy = home / "account.json"
    legacy_broken = any(a.get("_broken") and Path(a["_path"]) == legacy for a in accounts)
    if same is not None:
        path = Path(same["_path"])
    elif not legacy.exists() or legacy_broken:  # 第一个邮箱 (或坏掉的 account.json) 照旧写 account.json
        path = legacy
    else:
        path = home / "accounts" / f"{address.lower()}.json"
    write_json(path, data)
    return {k: v for k, v in data.items() if k != "secret"}


def set_proxy(home: Path | None, address: str, proxy: str) -> dict:
    """只改一个邮箱走不走代理 (授权码不动)."""
    account = find_account(home, address)
    if account is None:
        raise ValueError(f"没有这个邮箱: {address}")
    path = Path(account.pop("_path"))
    account["proxy"] = check_proxy(proxy)
    write_json(path, account)
    return {k: v for k, v in account.items() if k != "secret"}


def secret_of(account: dict) -> str:
    try:
        blob = base64.b64decode(str(account["secret"]), validate=True)
    except (KeyError, ValueError):
        raise MailError("secret", f"存好的授权码坏了: {fix_hint()}") from None
    return _dpapi(blob, False).decode("utf-8")


def forget_account(home: Path | None = None, address: str | None = None) -> bool:
    """删掉一个邮箱的账号文件 (读信记录留着). 不给地址: 只有一个邮箱时删它, 有几个时抛 ValueError (说清楚删哪个)."""
    accounts = load_accounts(home)
    if address is None:
        if len(accounts) > 1:
            raise ValueError("存了好几个邮箱, 说清楚删哪个: " + "、".join(mask_address(a["address"]) for a in accounts))
        targets = accounts
    else:
        want = str(address).strip().lower()
        targets = [a for a in accounts if a["address"].lower() == want]
    for account in targets:
        with contextlib.suppress(FileNotFoundError):
            Path(account["_path"]).unlink()
    return bool(targets)


def check_proxy(proxy: str) -> str:
    proxy = str(proxy or "auto").strip()
    if proxy.lower() in ("auto", "off"):
        return proxy.lower()
    if not hostport(proxy) or not proxy.lower().startswith("http://"):
        raise ValueError("代理写 auto (国外的邮箱用这台电脑设的代理)、off (不走代理) 或 http://127.0.0.1:端口 (只认 HTTP 代理)")
    return proxy


def hostport(url: str) -> tuple[str, int] | None:
    """"http://127.0.0.1:1082" / "127.0.0.1:1082" -> ("127.0.0.1", 1082); 认不出、socks 代理返回 None."""
    from urllib.parse import urlsplit
    url = str(url or "").strip()
    try:
        u = urlsplit(url if "://" in url else "http://" + url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or not _HOST.match(u.hostname):
        return None  # Windows 系统代理里 "socks=…" 这种分协议的写法也在这里挡掉
    return u.hostname, port or 8080


def system_proxy() -> tuple[str, int] | None:
    """这台电脑设的代理: 先看环境变量 (HTTPS_PROXY / HTTP_PROXY), 没有再看 Windows 的系统代理. 两样分开看:
    只设了 NO_PROXY 时 urllib.getproxies() 会整个不看系统代理."""
    import urllib.request
    for source in (urllib.request.getproxies_environment, getattr(urllib.request, "getproxies_registry", None)):
        try:
            found = source() if source else {}
        except OSError:
            found = {}
        for key in ("https", "http"):
            if found.get(key) and (hp := hostport(found[key])):
                return hp
    return None


def proxy_for(account: dict) -> tuple[str, int] | None:
    """这个邮箱走哪个代理 (None = 直连): off 直连; http://… 用它; auto 国外的邮箱用这台电脑设的代理, 国内的直连."""
    mode = str(account.get("proxy") or "auto").strip()
    if mode.lower() == "off":
        return None
    if mode.lower() != "auto":
        return hostport(mode)
    return system_proxy() if PRESETS.get(account.get("preset") or "", {}).get("overseas") else None


def protect(store, home: Path | None = None) -> tuple[bool, str]:
    """邮箱目录 (授权码、读信记录) 和 /api/mail 加进工作台的黑名单 (约束设置里看得见、能改; 所有档位都挡).
    store 是 wm.agent.governance.BlacklistStore. 你删掉、停用过的不再加 (尊重你的决定). 返回 (现在挡不挡得住, 说明)."""
    home = Path(home or HOME).resolve()
    rules = store.list()
    removed = [e.get("before") or {} for e in store.audit(1000) if e.get("action") == "remove" and e.get("by") == "user"]
    want = [({"kind": "path", "value": str(home), "operations": "all",
              "reason": "邮箱的授权码和读信记录只给主人: 工作台不读不改 (要调试先在这里停用)"}, "目录"),
            ({"kind": "command", "value": "/api/mail",
              "reason": "邮件内容只给主人看: 工作台的命令不读 /api/mail (要调试先在这里停用)"}, "接口")]
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
            store.add(rule, by="user", evidence="设置邮箱时加的 (连接邮箱的页面 / tools/mail_setup.py): 授权码和邮件内容只给主人")
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


# -- 设置 ------------------------------------------------------------------------
def parse_quiet(spec: str) -> tuple[int, int] | None:
    """"23:00-08:00" -> (1380, 480) 分钟; 空串 -> None. 写错抛 ValueError."""
    spec = str(spec or "").strip()
    if not spec:
        return None
    m = _QUIET.match(spec)
    if not m or int(m[1]) > 23 or int(m[3]) > 23 or int(m[2]) > 59 or int(m[4]) > 59:
        raise ValueError("免打扰写成 23:00-08:00 这样 (空着就是不设)")
    return int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])


def in_quiet(t: float, spec: str) -> bool:
    """t (秒) 在不在免打扰时段里 (按这个进程的时区: 服务端里就是她的时区, wm/tz.py)."""
    try:
        span = parse_quiet(spec)
    except ValueError:
        return False
    if span is None or span[0] == span[1]:
        return False
    d = datetime.fromtimestamp(t)
    now = d.hour * 60 + d.minute
    start, end = span
    return start <= now < end if start < end else (now >= start or now < end)


def clean_settings(changes: dict, *, local: bool = False) -> dict:
    """检查要改的设置 (只认 DEFAULTS 里的键), 返回整理过的. local: 主人在本机终端里改的 (tools/mail_setup.py),
    只有这样才能打开云端摘要 (邮件内容会发给云端模型); 接口、配置文件只能关它."""
    if not isinstance(changes, dict):
        raise ValueError("设置要是一个对象")
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"没有这个设置: {key}")
        if key in ("enabled", "quiet_urgent"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} 要是 true 或 false")
        elif key in _RANGES:
            lo, hi = _RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} 要是 {lo} 到 {hi} 之间的整数")
        elif key == "via":
            if value not in VIA:
                raise ValueError("via 只认 auto (微信 / QQ + 桌宠) / pet (只在桌宠上) / chat (只发微信 / QQ)")
        elif key == "summary":
            if value not in SUMMARY:
                raise ValueError("summary 只认 off / local / cloud")
            if value == "cloud" and not local:
                raise ValueError("云端摘要会把邮件内容发给云端模型, 只能你在本机终端里开: python tools/mail_setup.py set summary=cloud")
        elif key == "quiet":
            if not isinstance(value, str):
                raise ValueError("免打扰写成 23:00-08:00 这样")
            parse_quiet(value)
            value = value.strip()
        elif key == "folders":
            if not isinstance(value, list) or not 1 <= len(value) <= 5 or \
                    not all(isinstance(v, str) and 0 < len(v.strip()) <= 100 for v in value):
                raise ValueError("folders 要列 1 到 5 个文件夹名")
            value = [v.strip() for v in value]
        elif key in _LISTS:
            most, chars = _LISTS[key]
            if not isinstance(value, list) or len(value) > most or \
                    not all(isinstance(v, str) and 0 < len(v.strip()) <= chars for v in value):
                raise ValueError(f"{key} 最多 {most} 条, 每条 1 到 {chars} 个字")
            value = [v.strip().lower() for v in value]
        out[key] = value
    return out


def load_settings(home: Path | None = None, conf: dict | None = None) -> dict:
    """DEFAULTS <- 配置文件的 mail 段 (不能开云端摘要) <- settings.json (接口、tools/mail_setup.py 改的). 写错的项跳过."""
    out = dict(DEFAULTS)
    for source, local in ((conf or {}, False), (read_json(Path(home or HOME) / "settings.json", {}) or {}, True)):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key in DEFAULTS:
                with contextlib.suppress(ValueError):
                    out.update(clean_settings({key: value}, local=local))
    return out


def save_settings(home: Path | None, changes: dict, *, local: bool = False) -> dict:
    """改设置 (存进 settings.json), 返回改了哪些."""
    clean = clean_settings(changes, local=local)
    path = Path(home or HOME) / "settings.json"
    saved = read_json(path, {}) or {}
    saved.update(clean)
    write_json(path, saved)
    return clean


# -- 只读的 IMAP -----------------------------------------------------------------
imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))  # RFC 2971 (网易要); 3.10 的 imaplib 不认识它
ALLOWED = frozenset({"CAPABILITY", "LOGIN", "ID", "LIST", "STATUS", "EXAMINE", "UID", "NOOP", "LOGOUT"})
_WRITE_FETCH = re.compile(r"BODY\[|BINARY\[|RFC822(?!\.SIZE|\.HEADER)", re.I)  # 这些取法会顺手把信标成已读


class ReadOnlyViolation(RuntimeError):
    """守门拦下了会改动邮箱的命令 (没发出去)."""


def guard(imap: imaplib.IMAP4) -> imaplib.IMAP4:
    """之后这个连接只能发只读的命令. 挡在 imaplib 的 _command 上: 不管哪个方法 (store、select、copy、expunge、append、
    uid('MOVE')…) 最后都走它, 不在名单里的在发出去之前就抛 ReadOnlyViolation. UID 只放 SEARCH 和 FETCH, FETCH 只放 BODY.PEEK."""
    send = imap._command

    def checked(name, *args):
        name = str(name).upper()
        sub = str(args[0]).upper() if name == "UID" and args else ""
        why = ""
        if name not in ALLOWED or (name == "UID" and sub not in ("SEARCH", "FETCH")):
            why = f"只读守门拦下了 {name} {sub}".strip()
        elif sub == "FETCH" and _WRITE_FETCH.search(" ".join(str(a) for a in args[2:])):
            why = "只读守门拦下了不带 PEEK 的取信 (会把信标成已读)"
        if why:
            imap.literal = None  # append 这类先挂好了要跟着发的字面量: 拦下了就丢掉, 别跟着下一条命令发出去
            raise ReadOnlyViolation(why)
        return send(name, *args)

    imap._command = checked
    return imap


def tunnel(proxy: tuple[str, int], host: str, port: int, timeout: float | None) -> socket.socket:
    """经 HTTP 代理 (CONNECT) 打一条到邮箱服务器的 TCP 隧道. 回复按字节读到空行为止: 隧道一通服务器就可能先说话,
    多读一个字节都会吃掉它的开场白."""
    sock = socket.create_connection(proxy, timeout)
    try:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            byte = sock.recv(1)
            if not byte:
                raise OSError(f"代理 {proxy[0]}:{proxy[1]} 断开了")
            head += byte
            if len(head) > 8192:
                raise OSError(f"代理 {proxy[0]}:{proxy[1]} 回的不像 HTTP")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if not re.match(r"HTTP/1\.[01] 2\d\d", status):
            raise OSError(f"代理 {proxy[0]}:{proxy[1]} 不让连: {status[:80]}")
        return sock
    except BaseException:
        sock.close()
        raise


class _TunnelIMAP4(imaplib.IMAP4):
    """经代理的不加密连接 (只给本机的邮件网桥, 测试也用它)."""

    def __init__(self, host, port, proxy, timeout=None):
        self._proxy = proxy
        super().__init__(host, port, timeout)

    def _create_socket(self, timeout):
        return tunnel(self._proxy, self.host, self.port, timeout)


class _TunnelIMAP4SSL(imaplib.IMAP4_SSL):
    """经代理的 TLS 连接: 隧道通了再在上面握手 (证书和主机名照样校验)."""

    def __init__(self, host, port, proxy, ssl_context, timeout=None):
        self._proxy = proxy
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        return self.ssl_context.wrap_socket(tunnel(self._proxy, self.host, self.port, timeout), server_hostname=self.host)


def connect(account: dict, timeout: float = TIMEOUT_S) -> imaplib.IMAP4:
    """连邮箱服务器: TLS, 校验证书和主机名; 要走代理的 (proxy_for) 先经 HTTP 代理打隧道. 不加密只给本机的邮件网桥
    (account 里 tls=false 且服务器是 127.0.0.1). 测试换成连本机的假服务端."""
    host, port = account["host"], int(account.get("port") or 993)
    proxy, plain = proxy_for(account), account.get("tls") is False and host in LOOPBACK
    if plain:
        return _TunnelIMAP4(host, port, proxy, timeout) if proxy else imaplib.IMAP4(host, port, timeout)
    ctx = ssl.create_default_context()
    return _TunnelIMAP4SSL(host, port, proxy, ctx, timeout) if proxy else imaplib.IMAP4_SSL(host, port, ssl_context=ctx,
                                                                                            timeout=timeout)


def mutf7_decode(name: str) -> str:
    """IMAP 的文件夹名 (修改过的 UTF-7, RFC 3501 5.1.3) -> 能看的: &UXZO1mWHTvZZOQ- -> 其他文件夹."""
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        j = name.find("-", i)
        if j < 0:
            out.append(name[i:])
            break
        chunk = name[i + 1:j]
        if not chunk:
            out.append("&")
        else:
            try:
                raw = base64.b64decode(chunk.replace(",", "/") + "=" * (-len(chunk) % 4))
                out.append(raw.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(name[i:j + 1])
        i = j + 1
    return "".join(out)


def mutf7_encode(name: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


@dataclass
class Folder:
    raw: str              # 服务器上的名字 (修改过的 UTF-7), 发命令用
    name: str             # 解出来的名字, 给人看、和设置比
    flags: tuple = ()
    delim: str = "/"

    @property
    def junk(self) -> bool:
        """垃圾箱、广告 / 订阅文件夹: 不读, 只数新进了几封."""
        if {"\\junk", "\\spam"} & set(self.flags):
            return True
        leaf = self.name.rsplit(self.delim, 1)[-1] if self.delim else self.name
        return bool(_JUNK_NAME.match(leaf.strip()))


_JUNK_NAME = re.compile(r"^(junk|spam|bulk( mail)?|junk e-?mail|垃圾邮件|垃圾箱|广告邮件|广告|订阅邮件|(qq)?邮件订阅)$", re.I)
_LIST = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s*(?P<name>.*)$', re.I)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list(item) -> Folder | None:
    """LIST 回的一行 -> Folder. 名字是字面量时 imaplib 给的是 (开头, 名字) 两段."""
    if isinstance(item, tuple) and len(item) >= 2:
        m = _LIST.match(bytes(item[0]).strip())
        raw = bytes(item[1]).decode("utf-8", "replace") if m else ""
    elif isinstance(item, bytes):
        m = _LIST.match(item.strip())
        raw = _unquote(m["name"].decode("utf-8", "replace")) if m else ""
    else:
        return None
    if not m or not raw:
        return None
    flags = tuple(f.lower() for f in m["flags"].decode("ascii", "replace").split())
    delim = "" if m["delim"].upper() == b"NIL" else _unquote(m["delim"].decode("ascii", "replace"))
    return Folder(raw, mutf7_decode(raw), flags, delim)


def _ints(data) -> list[int]:
    out = []
    for part in data or []:
        if isinstance(part, bytes):
            out += [int(x) for x in part.split() if x.isdigit()]
    return out


def _fetch_items(data) -> list[tuple[bytes, bytes]]:
    """UID FETCH 的回复 -> [(这一封的元数据, 字面量)]. imaplib 给的是 (b'1 (UID 5 … {342}', 字面量), b')' 交替;
    有的服务器把 UID、FLAGS 放在字面量后面 (b' UID 5)'), 拼回元数据里一起认."""
    items, cur = [], None
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2:
            if cur is not None:
                items.append((cur[0], cur[1]))
            cur = [bytes(part[0]), bytes(part[1] or b"")]
        elif isinstance(part, bytes):
            if cur is not None:
                cur[0] += b" " + part
                items.append((cur[0], cur[1]))
                cur = None
            elif part.strip():
                items.append((part, b""))
    if cur is not None:
        items.append((cur[0], cur[1]))
    return items


def _meta(meta: bytes) -> dict:
    out = {}
    if m := re.search(rb"\bUID (\d+)", meta):
        out["uid"] = int(m[1])
    if m := re.search(rb"RFC822\.SIZE (\d+)", meta):
        out["size"] = int(m[1])
    if m := re.search(rb"FLAGS \(([^)]*)\)", meta):
        out["flags"] = tuple(f.decode("ascii", "replace").lower() for f in m[1].split())
    if b"INTERNALDATE" in meta:
        with contextlib.suppress(ValueError, OverflowError, TypeError):
            tt = imaplib.Internaldate2tuple(meta)
            if tt is not None:
                out["date"] = time.mktime(tt)
    return out


def server_text(exc: BaseException) -> str:
    raw = exc.args[0] if exc.args else exc
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return squash(re.sub(r"(?i)https?://\S+", "", text))[:160] or type(exc).__name__


class Reader:
    """一次读信 (阻塞, 在线程里跑, 别放在事件循环里): with Reader(account, secret) as r: … 进去登录, 出来退出.
    connector(account, timeout) 建连接 (默认 connect, 测试换成连假服务端的)."""

    def __init__(self, account: dict, secret: str, connector=None, timeout: float = TIMEOUT_S):
        self.account, self._secret = account, secret
        self.connector, self.timeout = connector or connect, timeout
        self.imap: imaplib.IMAP4 | None = None

    def __enter__(self) -> Reader:
        host = self.account.get("host", "")
        try:
            self.imap = guard(self.connector(self.account, self.timeout))
        except (OSError, imaplib.IMAP4.error) as exc:  # socket.timeout、ssl.SSLError 都是 OSError
            raise MailError("network", f"连不上 {host}: {server_text(exc)}") from None
        try:
            self.imap.login(self.account["address"], self._secret)
        except imaplib.IMAP4.abort as exc:
            self._drop()
            raise MailError("network", f"登录时断了: {server_text(exc)}") from None
        except imaplib.IMAP4.error as exc:
            self._drop()
            raise MailError("auth", f"登录被拒: {server_text(exc)}") from None
        except OSError as exc:
            self._drop()
            raise MailError("network", f"登录时连接出错: {server_text(exc)}") from None
        finally:
            self._secret = None
        if self.account.get("id_command"):
            with contextlib.suppress(imaplib.IMAP4.error):
                self.imap._simple_command("ID", '("name" "WorldModel" "version" "1.0" "vendor" "WorldModel")')
        return self

    def __exit__(self, *exc) -> None:
        if self.imap is not None:
            with contextlib.suppress(Exception):
                self.imap.logout()
            self._drop()

    def _drop(self) -> None:
        with contextlib.suppress(Exception):
            self.imap.shutdown()

    def _ok(self, typ, data, what: str):
        if typ != "OK":
            text = b" ".join(d for d in (data or []) if isinstance(d, bytes)).decode("utf-8", "replace")
            raise MailError("server", f"{what}: {squash(text)[:120] or typ}")
        return data

    def folders(self) -> list[Folder]:
        data = self._ok(*self.imap.list(), "列不出文件夹")
        return [f for f in (parse_list(item) for item in data) if f is not None]

    def examine(self, folder: Folder) -> tuple[int | None, int | None, int]:
        """只读打开 -> (UIDVALIDITY, UIDNEXT, 一共几封)."""
        typ, data = self.imap.select(_quote(folder.raw), readonly=True)
        self._ok(typ, data, f"打不开文件夹「{folder.name}」")
        got = self.imap.untagged_responses

        def num(key):
            vals = _ints(got.get(key) or [])
            return vals[-1] if vals else None
        return num("UIDVALIDITY"), num("UIDNEXT"), _ints(data)[-1] if _ints(data) else 0

    def status(self, folder: Folder) -> dict:
        """不打开文件夹, 问一下有几封、下一封的 UID (垃圾箱只数个数用)."""
        typ, data = self.imap.status(_quote(folder.raw), "(UIDNEXT UIDVALIDITY MESSAGES)")
        text = b" ".join(d for d in self._ok(typ, data, f"问不到「{folder.name}」") if isinstance(d, bytes))
        return {k.decode().lower(): int(v) for k, v in re.findall(rb"(UIDNEXT|UIDVALIDITY|MESSAGES) (\d+)", text)}

    def uids_after(self, last: int) -> list[int]:
        data = self._ok(*self.imap.uid("SEARCH", "UID", f"{int(last) + 1}:*"), "找新信没成")
        return sorted(u for u in set(_ints(data)) if u > last)  # "N:*" 没有新信时也会回最后一封

    def gmail_categories(self, uids: list[int]) -> dict[int, str]:
        """Gmail 自己归到"推广""社交"的是哪几封 (X-GM-RAW 搜索, 只读). 不是 Gmail、搜不了返回空."""
        out = {}
        for name, query in (("推广", "category:promotions"), ("社交", "category:social")):
            try:
                typ, data = self.imap.uid("SEARCH", "UID", f"{min(uids)}:{max(uids)}", "X-GM-RAW", f'"{query}"')
            except imaplib.IMAP4.error:
                return out
            if typ == "OK":
                out.update({u: name for u in _ints(data) if u in uids})
        return out

    def uids_since(self, day: datetime) -> list[int]:
        data = self._ok(*self.imap.uid("SEARCH", "SINCE", f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"), "找新信没成")
        return sorted(set(_ints(data)))

    def _fetch(self, uids: list[int], items: str, what: str) -> dict[int, tuple[dict, bytes]]:
        out = {}
        for i in range(0, len(uids), 50):
            chunk = uids[i:i + 50]
            data = self._ok(*self.imap.uid("FETCH", ",".join(map(str, chunk)), items), what)
            for meta, literal in _fetch_items(data):
                info = _meta(meta)
                if info.get("uid") in chunk:
                    out[info["uid"]] = (info, literal)
        return out

    def mails(self, uids: list[int], folder: str) -> list[Mail]:
        """只取信头 (BODY.PEEK[HEADER]) 和大小、日期、标记."""
        got = self._fetch(uids, "(UID RFC822.SIZE INTERNALDATE FLAGS BODY.PEEK[HEADER])", "取信头没成")
        return [mail_from(uid, info, raw, folder) for uid, (info, raw) in sorted(got.items())]

    def bodies(self, mails: list[Mail]) -> None:
        """取正文开头 (BODY.PEEK[]<0.N>, 不标已读), 拆好填进每封的 text / attachments / calendar / unsub."""
        got = self._fetch([m.uid for m in mails], f"(UID BODY.PEEK[]<0.{BODY_BYTES}>)", "取正文没成")
        for m in mails:
            if m.uid in got:
                m.text, m.attachments, m.calendar, m.unsub = body_of(got[m.uid][1])


# -- 拆信 ------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")
_CHARSETS = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030", "gb_2312-80": "gb18030",
             "unknown-8bit": "utf-8", "x-unknown": "utf-8", "utf8": "utf-8", "ks_c_5601-1987": "cp949"}


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub("", str(text or ""))).strip()


def _charset(cs) -> str:
    cs = str(cs or "").strip().strip('"').lower()
    cs = _CHARSETS.get(cs, cs) or "utf-8"
    try:
        codecs.lookup(cs)
        return cs
    except LookupError:
        return "utf-8"


def _decode_bytes(raw: bytes, cs) -> str:
    if cs:
        with contextlib.suppress(UnicodeDecodeError, LookupError):
            return raw.decode(_charset(cs))
    for guess in ("utf-8", "gb18030"):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode(guess)
    return raw.decode(_charset(cs), "replace")


def header_text(value) -> str:
    """信头 -> 能看的一行: RFC 2047 编码的 (=?gb2312?B?…?=) 解开; 没编码直接塞 8 位字节的 (常见是 GBK) 也认."""
    if value is None:
        return ""
    s = str(value)
    if any(0xDC80 <= ord(c) <= 0xDCFF for c in s):  # compat32 用 surrogateescape 留着的原始字节
        s = _decode_bytes(s.encode("ascii", "surrogateescape"), None)
    try:
        parts = email.header.decode_header(s)
    except Exception:
        return squash(s)
    return squash("".join(_decode_bytes(t, cs) if isinstance(t, bytes) else t for t, cs in parts))


@dataclass
class Mail:
    uid: int
    folder: str
    date: float
    size: int
    sender: str                 # 发件人的名字 (没有名字就是地址)
    address: str                # 发件地址 (小写)
    subject: str
    to: list = field(default_factory=list)
    cc: list = field(default_factory=list)
    head: Message | None = None  # 原始信头 (分拣用, 不存盘)
    flags: tuple = ()
    text: str = ""              # 正文开头 (清理过), 只在内存里
    attachments: list = field(default_factory=list)
    calendar: bool = False      # 带日程邀请 (text/calendar、.ics)
    unsub: bool = False         # 正文里有退订
    category: str = ""          # Gmail 自己的分类 (推广 / 社交), 别家没有
    level: str = ""             # junk / promo / low / important / urgent
    kind: str = ""              # 安全提醒、账单…
    reasons: list = field(default_factory=list)
    warn: str = ""              # 可能是冒充的之类, 汇报时带上

    @property
    def key(self) -> str:
        return f"{self.folder}:{self.uid}"


def _raw_all(head: Message, name: str) -> list[str]:
    """信头原样的值: compat32 用 get() 取时会把没编码的 8 位字节 (GBK) 换成 \\ufffd, 这里拿原样的交给 header_text 认."""
    name = name.lower()
    return [v if isinstance(v, str) else str(v) for k, v in head.raw_items() if k.lower() == name]


def mail_from(uid: int, info: dict, raw_head: bytes, folder: str) -> Mail:
    head = email.message_from_bytes(raw_head or b"", policy=email_policy.compat32)
    # 先按原始信头拆地址, 再解名字: 解出来的名字里可能有逗号、尖括号, 先解再拆会拆错
    raw_from = (_raw_all(head, "From") or [""])[0]
    name, addr = email.utils.parseaddr(raw_from)
    if (m := re.search(r"<\s*([^<>\s@]+@[^<>\s]+)\s*>\s*$", raw_from)) and m[1].lower() != addr.lower():
        name, addr = raw_from[:m.start()].strip().strip('"'), m[1]  # 名字里没加引号的 @ (冒充常用): 以尖括号里的为准
    name = header_text(name)
    rcpt = lambda key: [a.strip().lower() for _, a in email.utils.getaddresses(_raw_all(head, key)) if a]
    date = info.get("date")
    if date is None:
        with contextlib.suppress(TypeError, ValueError, IndexError, OverflowError):
            date = email.utils.parsedate_to_datetime(str(head.get("Date"))).timestamp()
    return Mail(uid=uid, folder=folder, date=float(date or time.time()), size=int(info.get("size") or 0),
                sender=squash(name) or addr.lower(), address=addr.strip().lower(), subject=header_text((_raw_all(head, "Subject") or [""])[0]),
                to=rcpt("To"), cc=rcpt("Cc"), head=head, flags=tuple(info.get("flags") or ()))


class _HtmlText(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "noscript"}
    _BLOCK = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _HtmlText()
    with contextlib.suppress(Exception):
        p.feed(html)
        p.close()
    return "".join(p.out)


_CUT = re.compile(r"^(-{2,}\s*(original message|原始邮件|转发邮件|forwarded message)\s*-{2,}|on .{4,160} wrote:|"
                  r"[在于] .{4,160}写道[:：]|from:\s|发件人[:：])", re.I)
_UNSUB_BODY = re.compile(r"退订|取消订阅|unsubscribe|不想再收到|不再接收|拒收此类|如不希望|"
                         r"view (this|it|the) (email |e-mail |message )?in (your |a )?browser|在浏览器中(查看|打开)|"
                         r"无法正常显示.{0,8}(点击|请点)", re.I)


def clean_body(text: str) -> str:
    """去掉引用的旧信、签名、零宽字符, 合并空白."""
    text = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\t", " "))
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if s == "--":  # 签名
            break
        if lines and _CUT.match(s):  # 下面是引用的旧信
            break
        if s and not s.startswith(">"):
            lines.append(re.sub(r"[ 　]+", " ", s))
    return "\n".join(lines)


def _part_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(payload, bytes):
        return ""
    return payload.decode(_charset(part.get_content_charset()), "replace")


def body_of(raw: bytes) -> tuple[str, list[str], bool, bool]:
    """信的开头 (可能被截断) -> (正文开头, 附件名, 带不带日程邀请, 有没有退订字样). 先要纯文本, 没有再把 HTML 转成字."""
    msg = email.message_from_bytes(raw or b"", policy=email_policy.compat32)
    plain = html = None
    files, calendar = [], False
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = header_text(part.get_filename() or "")
        if ctype == "text/calendar" or name.lower().endswith(".ics"):
            calendar = True
        if "attachment" in str(part.get("Content-Disposition") or "").lower() or (name and not ctype.startswith("text/")):
            if name:
                files.append(name[:60])
            continue
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html is None:
            html = _part_text(part)
    full = plain if plain and plain.strip() else html_text(html or "")
    text = clean_body(full)
    return text[:TEXT_CHARS], files[:10], calendar, bool(_UNSUB_BODY.search(full))


# -- 分拣 ------------------------------------------------------------------------
IGNORED = ("junk", "promo")
LEVELS = ("junk", "promo", "low", "important", "urgent")

_SPAM_SUBJECT = re.compile(r"^\s*(\*{3}\s*spam\s*\*{3}|\[spam\]|【垃圾邮件】)", re.I)
_AD_LABEL = re.compile(r"【广告】|[(（]\s*广告\s*[)）]|^\s*(\[AD\]|\(AD\)|（AD）|AD\s*[:：])", re.I)  # 广告法要求标明的
_PROMO_STRONG = re.compile(r"广告|促销|秒杀|抢购|领券|优惠券|满减|大促|狂欢节|双十一|双11|双十二|黑五|\d+\s*% ?off|\bsale\b|"
                           r"coupon|promo code", re.I)
_PROMO_WEAK = re.compile(r"优惠|特惠|折扣|限时|红包|会员日|新品|上新|周刊|月刊|精选|好物|推荐|订阅|直播|福利|抽奖|免费领|"
                         r"newsletter|weekly|monthly|digest|\bdeals?\b|\bpromo|webinar|limited time", re.I)
_AUTO_LOCAL = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|notify|notice|alerts?|system|"
                         r"mailer-daemon|postmaster|robot|bot|auto(mail|mated)?|service|services|support|info|news|"
                         r"newsletters?|marketing|promo(tions?)?|edm|ads?|offers?|deals?|hello|team|accounts?|admin|billing|"
                         r"updates?|digest|subscriptions?|reply|feedback|member(ship)?|vip|club|security|verify|"
                         r"verification)([-_.+].*|\d*)$", re.I)
_MARKETING_LOCAL = re.compile(r"^(news|newsletters?|marketing|promo(tions?)?|edm|ads?|offers?|deals?|digest|subscriptions?|"
                              r"campaigns?)([-_.+].*|\d*)$", re.I)
_BULK_DOMAIN = re.compile(r"^(edm|news|newsletter|marketing|promo|mailer|em|e|m|bulk)\d*\.", re.I)
_ESP_HEADER = re.compile(r"^x-(mailgun|sg-|sendgrid|mc-|mandrill|campaign|mailchimp|sendcloud|edm|newsletter|marketing|"
                         r"bulk|mailerlite|sib-|sendinblue|brevo|hubspot|marketo|emarsys|exacttarget|sfmc|braze|iterable|"
                         r"customerio|klaviyo|rpcampaign)", re.I)
_ESP_MAILER = re.compile(r"mailchimp|sendcloud|\bedm\b|webpower|mailerlite|sendinblue|brevo|hubspot|marketo|exacttarget|"
                         r"campaign|newsletter|群发|营销", re.I)
_URGENT_WORDS = re.compile(r"紧急|加急|尽快|立即|立刻|马上|十万火急|今天内|今日内|\burgent\b|\basap\b|immediately|"
                           r"action required|time.?sensitive", re.I)
_GITHUB_ME = {"review_requested", "mention", "team_mention", "assign", "security_alert"}  # GitHub 通知里点到你的
_KINDS = [(kind, level, re.compile(rx, re.I)) for kind, level, rx in [  # (什么事, 级别, 主题 / 正文开头里的字): 先对上的算
    ("安全提醒", "urgent", r"异地登录|新设备登录|登录提醒|登录异常|异常登录|账号异常|账户异常|账号被盗|被盗|冻结|"
                          r"密码(已经?|被)?(修改|重置|更改)|修改了?密码|安全提醒|安全警告|security alert|"
                          r"unusual (sign-?in|activity|login)|new (sign-?in|login|device)|suspicious (sign-?in|activity|login)|"
                          r"password (was |has been )?(changed|reset)|sign-?in attempt"),
    ("欠费逾期", "urgent", r"欠费|停机|逾期|催缴|催收|最后期限|即将(到期|过期|停用|关闭|注销|欠费)|今天到期|今日到期|明天到期|"
                          r"overdue|final notice|past due|account (will be )?suspended|service interruption"),
    ("验证码", "low", r"验证码|校验码|动态码|动态密码|verification code|security code|one-?time (pass)?code|\bOTP\b|"
                      r"login code|confirm your (email|account)"),
    ("工作", "important", r"面试|\boffer\b|录用|入职|转正|离职|劳动合同|签约|简历|内推|interview|onboarding|审批|待审|"
                         r"审核(结果|通过|未通过|不通过)|批准|驳回|approval|approve|review requested|requested your review|"
                         r"mentioned you|assigned (you|to you)"),
    ("会议日程", "important", r"会议|开会|日程|邀请您|邀请你|邀请函|邀约|预约成功|meeting|invitation|calendar|腾讯会议|"
                             r"飞书会议|钉钉会议|\bzoom\b|webex|teams meeting"),
    ("账单", "important", r"账单|还款|缴费|扣款失败|付款失败|续费失败|发票|报销|工资|薪资|invoice|\bbill\b|billing statement|"
                         r"payment (due|failed|reminder)|amount due"),
    ("截止", "important", r"截止|deadline|\bddl\b|due (date|today|tomorrow)|最后一天|到期"),
    ("学习考试", "important", r"考试|成绩|录取|通知书|报名|论文|答辩|导师|作业|\bexams?\b|\bgrades?\b|admission|thesis|"
                             r"manuscript|reviewer|camera-?ready|decision on your (manuscript|submission|paper)"),
    ("出行", "important", r"航班|机票|值机|登机|火车|高铁|车票|酒店|入住|行程单|行程变更|签证|护照|flight|boarding|"
                         r"itinerary|hotel|booking|reservation"),
    ("政务法律", "important", r"法院|律师|传票|起诉|仲裁|税务|个税|社保|公积金|公安|派出所|政务|court|legal notice|subpoena|"
                             r"\btax\b"),
    ("医疗", "important", r"医院|体检|挂号|报告单|检查结果|诊断|处方|medical|clinic|lab result"),
    ("快递订单", "low", r"快递|取件|包裹|物流|发货|签收|派送|订单|已支付|支付成功|付款成功|交易成功|\border\b|shipped|"
                        r"delivery|tracking|receipt|purchase"),
]]


def _listed(addr: str, items) -> bool:
    """addr 在不在名单里: 名单项写完整地址, 或 @域名 / 域名 (子域名也算)."""
    addr = str(addr or "").lower()
    domain = addr.rsplit("@", 1)[-1]
    for item in items or ():
        item = str(item).strip().lower()
        if not item:
            continue
        if "@" in item and not item.startswith("@"):
            if addr == item:
                return True
            continue
        d = item.lstrip("@")
        if d and (domain == d or domain.endswith("." + d)):
            return True
    return False


def _has(words, text: str) -> bool:
    low = text.lower()
    return any(w and w.lower() in low for w in words or ())


def _spam_flag(m: Mail) -> str:
    for k, v in (m.head.items() if m.head is not None else []):
        kl, vl = k.lower(), str(v).strip().lower()
        if kl in ("x-spam-flag", "x-spam") and vl.startswith(("yes", "true")):
            return f"服务器标成了垃圾 ({k})"
        if kl == "x-spam-status" and vl.startswith("yes"):
            return f"服务器标成了垃圾 ({k})"
    return "主题标着垃圾邮件" if _SPAM_SUBJECT.search(m.subject) else ""


def bulk_signals(m: Mail) -> tuple[int, list[str]]:
    """群发、推送的迹象 -> (分数, 理由). 3 分以上算推送."""
    h = m.head if m.head is not None else Message()
    score, why = 0, []

    def add(n, reason):
        nonlocal score
        score += n
        why.append(reason)

    if m.category:
        add(3, f"Gmail 把它归到了{m.category}")
    if h.get("List-Unsubscribe"):
        add(3, "带退订头 (List-Unsubscribe)")
    prec = str(h.get("Precedence") or "").strip().lower()
    if prec in ("bulk", "junk"):
        add(3, f"群发 (Precedence: {prec})")
    elif prec == "list":
        add(2, "邮件列表 (Precedence: list)")
    if h.get("List-Id") and not h.get("List-Unsubscribe"):
        add(1, "邮件列表 (List-Id)")
    if any(_ESP_HEADER.match(k) for k in h.keys()):
        add(2, "群发平台的信头")
    elif _ESP_MAILER.search(str(h.get("X-Mailer") or "")):
        add(2, "群发软件 (X-Mailer)")
    if h.get("Feedback-ID") or h.get("X-Feedback-ID"):
        add(1, "群发反馈头 (Feedback-ID)")
    local, _, domain = m.address.partition("@")
    if _MARKETING_LOCAL.match(local):
        add(2, f"营销发件人 ({local}@)")
    elif _BULK_DOMAIN.match(domain):
        add(1, f"群发域名 ({domain})")
    if _AD_LABEL.search(m.subject):
        add(3, "主题标着广告")
    elif _PROMO_STRONG.search(m.subject):
        add(2, "主题像广告")
    elif _PROMO_WEAK.search(m.subject):
        add(1, "主题像推送")
    if m.unsub:
        add(2, "正文里有退订")
    rcpt = m.to + m.cc
    if len(rcpt) > 10:
        add(1, f"收件人很多 ({len(rcpt)} 个)")
    if not rcpt or any("undisclosed" in a for a in rcpt):
        add(1, "没写收件人 (密送 / 群发)")
    return score, why


def _kind(m: Mail) -> tuple[str, str, str]:
    """像是什么事 -> (什么事, 级别, 在哪对上的: subject / body)."""
    for kind, level, rx in _KINDS:
        if rx.search(m.subject):
            return kind, level, "subject"
    if m.calendar:
        return "会议日程", "important", "body"
    head = m.text[:300]
    for kind, level, rx in _KINDS:
        if head and rx.search(head):
            return kind, level, "body"
    return "", "", ""


def suspicious(m: Mail) -> str:
    """像冒充的: 发件验证没过 (有 Authentication-Results 时), 或名字里写着别家的邮箱地址."""
    ar = " ".join(str(v) for v in (m.head.get_all("Authentication-Results") or [] if m.head is not None else [])).lower()
    if "dmarc=fail" in ar or ("spf=fail" in ar and "dkim=fail" in ar):
        return "发件人验证没通过, 可能是冒充的, 别点里面的链接"
    real = m.address.rsplit("@", 1)[-1]
    for named in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", m.sender):
        named = named.lower()  # 同一家的子域名 (mail.example.com 和 example.com) 不算对不上
        if real and named != real and not real.endswith("." + named) and not named.endswith("." + real):
            return "名字里写的地址和真正的发件地址对不上, 可能是冒充的"
    return ""


def classify(m: Mail, settings: dict) -> Mail:
    """按规则分拣 (填 level / kind / reasons / warn). 信头和正文都看过时再调一次就是最后的结果."""
    head = m.head if m.head is not None else Message()
    scan = m.subject + "\n" + m.text[:300]

    def done(level, kind, *reasons):
        if level == "urgent" and m.warn:  # 像冒充的: 不为它单独打扰你, 放进汇总 (排最后) 带一句提醒
            level = "important"
        m.level, m.kind, m.reasons = level, kind, [r for r in reasons if r]
        return m

    m.warn = suspicious(m)
    if _listed(m.address, settings.get("ignore_senders")) or _has(settings.get("ignore_keywords"), scan):
        return done("promo", "", "你说过不看")
    if flag := _spam_flag(m):
        return done("junk", "", flag)
    score, why = bulk_signals(m)
    kind, level, where = _kind(m)
    urgent_word = bool(_URGENT_WORDS.search(m.subject)) or str(head.get("X-Priority") or "").strip()[:1] == "1" \
        or str(head.get("Importance") or "").strip().lower() == "high"
    if _listed(m.address, settings.get("important_senders")):
        return done("urgent" if level == "urgent" or urgent_word else "important", kind or "重要发件人", "你说过重要的发件人")
    github = str(head.get("X-GitHub-Reason") or "").strip().lower()
    if github in _GITHUB_ME:
        return done("important", "工作", f"GitHub 上点到你 ({github})")
    if score >= 3:
        return done("promo", kind, *why)
    if _has(settings.get("important_keywords"), scan):
        return done("urgent" if urgent_word and score < 2 else "important", kind or "你关心的", "有你说要看的字", *why)
    auto = bool(_AUTO_LOCAL.match(m.address.partition("@")[0])) or \
        str(head.get("Auto-Submitted") or "no").strip().lower() != "no"
    personal = bool(m.address) and not auto and score <= 1 and len(m.to + m.cc) <= 10
    if kind:
        if level == "urgent" and (where == "body" or (score >= 2 and kind not in ("安全提醒", "欠费逾期"))):
            level = "important"  # 正文里才对上的、带群发迹象的 (安全提醒、欠费逾期除外) 不算急
        if level == "important" and score >= 2 and not personal:
            level = "low"  # 带群发迹象的"会议""账单"多半是推送
        if personal and level == "low":
            level = "important"  # 人写给你的: 提到快递、订单也算要看
        if personal and urgent_word:
            level = "urgent"
        return done(level, kind, f"像是{kind}" + ("" if where == "subject" else " (看正文)"), *why)
    if personal:
        return done("urgent" if urgent_word else "important", "私人来信", "像是人写给你的", *why)
    return done("low", "通知", *(why or ["自动发的通知"]))


# -- 一次读信 --------------------------------------------------------------------
@dataclass
class Batch:
    mails: list = field(default_factory=list)          # 新信 (分好类的)
    cursors: dict = field(default_factory=dict)        # 文件夹 -> {uidvalidity, last_uid}: 读到哪了
    junk_new: dict = field(default_factory=dict)       # 垃圾箱 / 广告文件夹 -> 新进了几封 (没打开看)
    junk_cursors: dict = field(default_factory=dict)   # 文件夹 -> {uidvalidity, uidnext}
    skipped: int = 0                                   # 太多没看的
    missing: list = field(default_factory=list)        # 设置里有、邮箱里找不到的文件夹
    first: list = field(default_factory=list)          # 这次起头的文件夹 (第一次读、UIDVALIDITY 变了)


def read_batch(account: dict, secret: str, settings: dict, state: dict, connector=None, timeout: float = TIMEOUT_S,
               now: float | None = None) -> Batch:
    """登录, 把设置里的文件夹从上次读到的地方往后看一遍 (只读), 分好类; 垃圾箱只数个数. 阻塞, 在线程里跑.
    state: {"folders": {名字: {uidvalidity, last_uid}}, "junk": {名字: {uidvalidity, uidnext}}} (mailbox_report 存的)."""
    now = time.time() if now is None else now
    out = Batch()
    with Reader(account, secret, connector, timeout) as r:
        gmail = account.get("preset") == "gmail" or "X-GM-EXT-1" in (getattr(r.imap, "capabilities", None) or ())
        folders = r.folders()
        by_name = {f.name.casefold(): f for f in folders}
        wanted = set()
        for name in settings.get("folders") or ["INBOX"]:
            f = by_name.get(name.casefold())
            if f is None:
                out.missing.append(name)
                continue
            wanted.add(f.name.casefold())
            uidvalidity, uidnext, _ = r.examine(f)
            cur = (state.get("folders") or {}).get(f.name) or {}
            if cur.get("uidvalidity") != uidvalidity or not isinstance(cur.get("last_uid"), int):
                window = now - settings.get("first_run_hours", 24) * 3600  # 起头: 只看最近这么久的, 不翻旧账
                uids = r.uids_since(datetime.fromtimestamp(window) - timedelta(days=1))
                # 读到哪了记成现在最大的 UID; 服务器不给 UIDNEXT 时问一遍全部的 UID (不然下次会把整个收件箱当新信)
                top = max([uidnext - 1 if uidnext else max(r.uids_after(0), default=0), *uids, 0])
                out.first.append(f.name)
            else:
                window = None
                uids = r.uids_after(cur["last_uid"])
                top = max([cur["last_uid"], *uids])
            if len(uids) > MAX_PER_POLL:
                out.skipped += len(uids) - MAX_PER_POLL
                uids = uids[-MAX_PER_POLL:]
            mails = [m for m in r.mails(uids, f.name) if window is None or m.date >= window]
            if mails and gmail:  # Gmail 自己的分类: 推广、社交直接算推送
                cats = r.gmail_categories([m.uid for m in mails])
                for m in mails:
                    m.category = cats.get(m.uid, "")
            for m in mails:  # 先只看信头: 一看就是推送、垃圾的不取正文
                classify(m, settings)
            need = [m for m in mails if m.level not in IGNORED]
            if need:
                r.bodies(need)
                for m in need:
                    classify(m, settings)
            out.cursors[f.name] = {"uidvalidity": uidvalidity, "last_uid": top}
            out.mails += mails
        for f in folders:  # 垃圾箱、广告文件夹: 不打开, 只数新进了几封
            if not f.junk or f.name.casefold() in wanted or "\\noselect" in f.flags:
                continue
            with contextlib.suppress(MailError, imaplib.IMAP4.error):
                st = r.status(f)
                prev = (state.get("junk") or {}).get(f.name) or {}
                if prev.get("uidvalidity") == st.get("uidvalidity") and st.get("uidnext") and prev.get("uidnext"):
                    out.junk_new[f.name] = max(0, st["uidnext"] - prev["uidnext"])
                out.junk_cursors[f.name] = {"uidvalidity": st.get("uidvalidity"), "uidnext": st.get("uidnext")}
    return out


def check_login(account: dict, secret: str, connector=None, timeout: float = TIMEOUT_S) -> dict:
    """设置时只读地试一次: 登录、列文件夹、只读打开收件箱数一下. 返回 {inbox, folders, junk}."""
    with Reader(account, secret, connector, timeout) as r:
        folders = r.folders()
        inbox = next((f for f in folders if f.name.casefold() == "inbox"), Folder("INBOX", "INBOX"))
        _, _, exists = r.examine(inbox)
        return {"inbox": exists, "folders": [f.name for f in folders][:50], "junk": [f.name for f in folders if f.junk]}
