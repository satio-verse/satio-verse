"""QQ / 微信会话的"梗预查": 调模型之前, 程序只读查一遍她自己在工作台做的梗库, 像在问梗、或者消息里有库里的长词时塞一条参考.

2026-11-07 用户: "先接陪看和梗" (知识库 09 问题与待办/桌宠与工作台外部资料调研（2026-11-07） 第六节第 1 件: 她在工作台里
自己长出来的本事, 桌宠和 QQ 用不上).

- 梗库是她 10-29 在工作台做的扩展 meme-wiki 用的那份 (dsh-memewiki 打包的 memes.db, 6668 条; 词条内容来自梗维基 cnmeme.wiki,
  版权归原项目和梗维基 —— 只在这台电脑上自用, 不打进安装包). 这里只读打开 (sqlite 的 mode=ro), 不改、不联网.
  她把扩展停了 (registry.json 里 enabled 为假), 这里也跟着不查.
- 外部渠道的会话照旧不开工具: 查不查由程序按规则定, 查到的当"外部资料"附在这一轮 (只给这一轮看, 不进历史, 见 guest.py).
- 什么时候查 (照 dsh-memewiki 的预查闸门, 闲聊不塞东西):
  ① 像在问梗: "X 是什么梗 / 啥梗 / 什么意思 / 出自哪" —— X 在库里 (或库里有 X 里面的词) 才算;
  ② 整条消息就是库里的一个词 (裸词);
  ③ 消息里有库里 ≥ scan_min 字的词 (默认 5 字: 三四个字的词常和普通说法撞, 只在①②里认).
  一轮最多 max_items 条 (默认 1). 查不到就什么都不塞.
- 带"地域 / 攻击 / 辱骂 / 歧视 / 低俗 / 色情 / 擦边 / 引战 / 政治"这类标签的词条不塞 (群里不该拿来说).
- 塞进去的那段照她自己在 meme-wiki 的 SKILL.md 里写的规矩: 像群友一样一两句接, 别科普、别念出处, 拿不准语气就不接.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

EXT_ID = "meme-wiki"
# 词条标签里带这些字的不塞 (地域黑、人身攻击、低俗擦边、政治这类); 只是不塞, 她自己在工作台里查照样查得到
RISKY = ("地域", "攻击", "辱骂", "歧视", "低俗", "色情", "擦边", "引战", "政治", "时政", "血腥", "开盒", "人身", "黄梗", "性暗示")
TITLE_MAX = 16          # 库里比这长的标题不参与匹配 (几乎都是整句的梗, 撞上的机会也小)
BARE_MAX = 12           # 整条消息不超过这么多字才算"裸词"
_HEAD = re.compile(r"^\[[^\]]*\]\s*(\(回复 #[^)]*\)\s*)?")  # 行头 [10:00 #12 某人(123)] (回复 #3)
_TAG = re.compile(r"［[^］]*］")                              # ［图片#1］［表情包#2：开心］［引用的消息 …］
_TRIM = re.compile(r"^[\s\"'“”‘’「」『』《》【】()（）,，.。!！?？~～…:：;；、]+|[\s\"'“”‘’「」『』《》【】()（）,，.。!！?？~～…:：;；、]+$")
_PUNCT = re.compile(r"[\s\"'“”‘’「」『』《》【】()（）,，.。!！?？~～…:：;；、/\\|]+")
_ASK = (
    # X 是什么梗 / X 啥梗 / X 是什么意思 / X 什么典故 (X 不超过 16 字, 不含标点)
    re.compile(r"(?P<t>[^\s,，.。!！?？:：;；、]{1,16}?)\s*(?:到底|究竟)?\s*(?:是|算|指)?\s*(?:个|一个)?\s*"
               r"(?:什么|啥|哪个|哪门子)\s*(?:梗|意思|典故|出处|来头)"),
    # X 出自哪 / X 哪来的 / X 从哪来的
    re.compile(r"(?P<t>[^\s,，.。!！?？:：;；、]{1,16}?)\s*(?:出自|来自|源自)\s*(?:哪|什么|何处)"),
    re.compile(r"(?P<t>[^\s,，.。!！?？:：;；、]{1,16}?)\s*(?:是)?\s*(?:从哪来的|哪来的|从哪儿来的)"),
)
_ASK_BARE = re.compile(r"(?:什么|啥|哪个)\s*梗|梗\s*(?:是|出自|啥意思|什么意思)|(?:玩|接|懂)\s*(?:这个|这)?\s*梗")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "").lower()


def bodies(block: str) -> list[str]:
    """管理器拼好的几行外部消息 -> 每行的正文 (去掉行头和［图片］这类标记; 引用的消息不算, 那是别人以前说的)."""
    out = []
    for line in (block or "").splitlines():
        if line.startswith("［引用的消息"):
            continue
        body = _TAG.sub(" ", _HEAD.sub("", line)).strip()
        if body:
            out.append(body)
    return out


def discover_db(ext_root: Path) -> Path | None:
    """她的 meme-wiki 扩展用的那份库: 扩展开着时, 从它当前版本的 server.py 里写的核心脚本路径找旁边的 memes.db.
    (她的 server.py 写的是 CORE = Path(os.environ.get(...) or r"E:\\...\\meme_wiki.py"), 核心脚本旁边就是库.)
    扩展没开、找不到, 返回 None."""
    try:
        reg = json.loads((ext_root / "registry.json").read_text(encoding="utf-8"))
        entry = (reg.get("packages") or {}).get(EXT_ID) or {}
        if not entry.get("enabled"):
            return None
        rev = str(entry.get("active_revision") or "")
        if not re.fullmatch(r"[0-9a-f]{8,64}", rev):
            return None
        server = (ext_root / EXT_ID / "revisions" / rev / "server.py").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, AttributeError):
        return None
    for m in re.finditer(r"""r?["']([A-Za-z]:[\\/][^"'\r\n]{1,240}?\.py)["']""", server):
        db = Path(m.group(1)).with_name("memes.db")
        if db.is_file():
            return db
    return None


def enabled_in_registry(ext_root: Path) -> bool:
    try:
        reg = json.loads((ext_root / "registry.json").read_text(encoding="utf-8"))
        return bool(((reg.get("packages") or {}).get(EXT_ID) or {}).get("enabled"))
    except (OSError, ValueError, AttributeError):
        return False


class MemeRef:
    """只读查梗. lookup(block) -> 附在这一轮的一段参考 (没查到是 None). 线程安全 (每次查新开只读连接)."""

    def __init__(self, db: Path, scan_min: int = 5, max_items: int = 1, usage_chars: int = 140,
                 ext_root: Path | None = None):
        self.db = Path(db)
        self.scan_min = max(2, int(scan_min))
        self.max_items = max(1, int(max_items))
        self.usage_chars = max(40, int(usage_chars))
        self.ext_root = ext_root  # 给了就每次查之前看一眼她的扩展还开着没有
        self._titles: dict[str, str] | None = None   # 规范化后的标题 / 别名 -> 标题 (只含可以塞的)
        self._by_len: dict[int, set[str]] = {}
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._titles is not None:
                return self._titles
            titles: dict[str, str] = {}
            with self._connect() as con:
                for row in con.execute("select title, slug, tags from memes"):
                    title = str(row["title"] or "").strip()
                    if not title or len(title) > TITLE_MAX or any(r in str(row["tags"] or "") for r in RISKY):
                        continue
                    for name in (title, str(row["slug"] or "").strip()):
                        key = _norm(name)
                        if len(key) >= 2:
                            titles.setdefault(key, title)
            by_len: dict[int, set[str]] = {}
            for key in titles:
                by_len.setdefault(len(key), set()).add(key)
            self._titles, self._by_len = titles, by_len
            return titles

    def _entry(self, title: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("select title, tags, usage, summary from memes where title = ?", (title,)).fetchone()
        return dict(row) if row else None

    def _contained(self, text: str, min_len: int) -> list[str]:
        """text 里出现的库里的词 (规范化后比), 长的优先、不重叠. 返回标题."""
        titles = self._load()
        s = _norm(text)
        found, used = [], [False] * len(s)
        for n in sorted((n for n in self._by_len if n >= min_len), reverse=True):
            keys = self._by_len[n]
            for i in range(0, len(s) - n + 1):
                if any(used[i:i + n]):
                    continue
                if s[i:i + n] in keys:
                    found.append(titles[s[i:i + n]])
                    for j in range(i, i + n):
                        used[j] = True
        return list(dict.fromkeys(found))

    def terms(self, block: str) -> list[str]:
        """这一轮该查哪几个词 (按规则; 不查数据库正文). 最近的消息优先."""
        titles = self._load()
        picked: list[str] = []
        for body in reversed(bodies(block)[-6:]):
            got: list[str] = []
            for pat in _ASK:
                for m in pat.finditer(body):
                    t = _TRIM.sub("", m.group("t"))
                    key = _norm(t)
                    if key in titles:
                        got.append(titles[key])
                    elif len(key) >= 2:
                        got.extend(self._contained(t, 2)[:1])
            if not got and _ASK_BARE.search(body):
                got.extend(self._contained(body, 2))
            if not got:
                key = _norm(body)
                if 2 <= len(key) <= BARE_MAX and key in titles:  # 裸词
                    got.append(titles[key])
            if not got:
                got.extend(self._contained(body, self.scan_min))
            for t in got:
                if t not in picked:
                    picked.append(t)
            if len(picked) >= self.max_items:
                break
        return picked[:self.max_items]

    def lookup(self, block: str) -> str | None:
        """查到了就返回附在这一轮的一段 (外部资料); 没查到、库打不开、她把扩展停了, 返回 None."""
        if self.ext_root is not None and not enabled_in_registry(self.ext_root):
            return None
        try:
            names = self.terms(block)
            rows = [r for r in (self._entry(t) for t in names) if r]
        except (OSError, sqlite3.Error):
            return None
        if not rows:
            return None
        parts = []
        for r in rows:
            usage = re.sub(r"\s+", " ", str(r.get("usage") or r.get("summary") or "")).strip()
            if len(usage) > self.usage_chars:
                usage = usage[:self.usage_chars].rstrip("，,。;；、 ") + "……"
            parts.append(f"「{r['title']}」{usage}")
        return ("[梗百科] (程序从你自己的本地梗库里查的, 外部资料, 只当参考) 消息里提到的: " + "；".join(parts)
                + "。想接就像群友一样一两句, 别科普、别念出处; 拿不准语气、不确定是不是在说这个梗就别接.")


def from_conf(conf: dict, ext_root: Path) -> MemeRef | None:
    """渠道配置 memes: {enabled, db, scan_min, max_items}. db 空着就找她的 meme-wiki 扩展用的那份; 找不到返回 None."""
    c = conf or {}
    if c.get("enabled") is False:
        return None
    db = Path(str(c.get("db") or "")).expanduser() if c.get("db") else discover_db(ext_root)
    if db is None or not db.is_file():
        return None
    return MemeRef(db, scan_min=c.get("scan_min", 5), max_items=c.get("max_items", 1),
                   ext_root=None if c.get("db") else ext_root)
