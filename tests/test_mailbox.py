"""绫音读邮箱 (wm/channels/mailbox*.py、tools/mail_setup.py): 全用本机的假 IMAP 服务端 (FakeIMAP), 不连真邮箱, 不调真模型.
测: 只读 (只发 EXAMINE / BODY.PEEK, 改邮箱的命令发不出去)、分拣 (垃圾 / 推送 / 普通 / 要看 / 要紧)、低打扰的汇报、开关、
失败时老实说、授权码不漏、工作台黑名单挡得住、只发给主人私聊、接口、设置工具."""
import asyncio
import email.utils
import importlib.util
import imaplib
import json
import os
import re
import socket
import socketserver
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.governance import BlacklistStore
from wm.channels import mailbox as M
from wm.channels import mailbox_http as H
from wm.channels import mailbox_report as R

ROOT = Path(__file__).resolve().parents[1]
ME, CODE = "me@qq.com", "abcdefghijklmnop"
T0 = time.time()
ADS = "&UXZO1mWHTvZZOQ-/&Xn9USpCuTvY-"  # 其他文件夹/广告邮件 (修改过的 UTF-7)


@pytest.fixture(autouse=True)
def _dpapi_off_windows(monkeypatch):
    if os.name != "nt":  # 不是 Windows 时换一个可逆的替身 (Windows 上用真的 DPAPI)
        monkeypatch.setattr(M, "_dpapi", lambda data, encrypt: bytes(b ^ 0x5A for b in data))


# ---------------------------------------------------------------------------
# 假的 IMAP 服务端: 够 imaplib 用的那一点 IMAP4rev1. 每条命令都记下来 (commands); 改邮箱的命令照样记、照样回 OK,
# 测试看 commands 就知道发没发过. 不带 PEEK 取正文时像真服务器一样把信标成已读 (只读打开时不标).
# ---------------------------------------------------------------------------
def _tokens(s: str) -> list[str]:
    out, i = [], 0
    while i < len(s):
        if s[i] == " ":
            i += 1
        elif s[i] == '"':
            j, buf = i + 1, []
            while s[j] != '"':
                if s[j] == "\\":
                    j += 1
                buf.append(s[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif s[i] == "(":
            depth, j = 0, i
            while True:
                depth += (s[j] == "(") - (s[j] == ")")
                j += 1
                if depth == 0:
                    break
            out.append(s[i:j])
            i = j
        else:
            j = s.find(" ", i)
            j = len(s) if j < 0 else j
            out.append(s[i:j])
            i = j
    return out


def _uidset(spec: str, top: int) -> set:
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition(":")
        lo = top if a == "*" else int(a)
        hi = lo if not b else top if b == "*" else int(b)
        out |= set(range(min(lo, hi), max(lo, hi) + 1))  # RFC 3501: N:* 在 N 比最大的还大时就是 最大:N
    return out


class FakeIMAP:
    def __init__(self, user=ME, password=CODE, need_id=False, uid_last=False, no_uidnext=False, gmail=False):
        self.user, self.password, self.need_id, self.uid_last = user, password, need_id, uid_last
        self.no_uidnext = no_uidnext  # 有的服务器打开文件夹时不给 UIDNEXT
        self.gmail = gmail  # 像 Gmail: 能力里有 X-GM-EXT-1, 认 X-GM-RAW "category:…" 搜索
        self.caps = "IMAP4rev1 ID" + (" X-GM-EXT-1" if gmail else "")
        self.boxes: dict = {}
        self.commands: list[str] = []
        self.logins = 0
        self.add_box("INBOX")

    def add_box(self, raw, flags=("\\HasNoChildren",), uidvalidity=7):
        self.boxes[raw] = {"uidvalidity": uidvalidity, "flags": flags, "msgs": [], "next": 1}

    def add(self, raw: bytes, *, at: float, box="INBOX", flags=(), category="") -> int:
        b = self.boxes[box]
        uid, b["next"] = b["next"], b["next"] + 1
        b["msgs"].append({"uid": uid, "date": at, "flags": set(flags), "raw": raw, "category": category})
        return uid

    def flags(self, box="INBOX"):
        return [set(m["flags"]) for m in self.boxes[box]["msgs"]]

    def names(self):
        """每条命令的名字, UID 带上子命令: ["CAPABILITY", "LOGIN", "UID FETCH", …]."""
        out = []
        for line in self.commands:
            words = line.upper().split(" ")
            out.append(" ".join(words[1:3]) if words[1] == "UID" else words[1])
        return out

    def start(self):
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                _Session(fake, self.rfile, self.wfile).run()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def connector(self, account, timeout):
        return imaplib.IMAP4("127.0.0.1", self.port, timeout=timeout)


class _Session:
    def __init__(self, fake, rfile, wfile):
        self.f, self.r, self.w = fake, rfile, wfile
        self.box, self.readonly, self.id_sent = None, True, False

    def send(self, line):
        self.w.write((line if isinstance(line, bytes) else line.encode("utf-8")) + b"\r\n")

    def run(self):
        self.send(f"* OK [CAPABILITY {self.f.caps}] fake imap ready")
        self.w.flush()
        while line := self.r.readline():
            line = line.rstrip(b"\r\n").decode("utf-8", "replace")
            self.f.commands.append(line)
            tag, _, rest = line.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            if cmd == "UID":
                sub, _, args = args.partition(" ")
                cmd = f"UID {sub.upper()}"
            try:
                going = self.dispatch(tag, cmd, args)
            except Exception as exc:  # noqa: BLE001
                self.send(f"{tag} BAD {type(exc).__name__}")
                going = True
            self.w.flush()
            if not going:
                return

    def dispatch(self, tag, cmd, args):
        f = self.f
        if cmd == "CAPABILITY":
            self.send(f"* CAPABILITY {f.caps}")
        elif cmd == "LOGIN":
            user, password = _tokens(args)[:2]
            f.logins += 1
            if (user, password) != (f.user, f.password):
                self.send(f"{tag} NO [AUTHENTICATIONFAILED] Login fail. password is incorrect or login frequency limited")
                return True
        elif cmd == "ID":
            self.id_sent = True
            self.send("* ID NIL")
        elif cmd == "LIST":
            for raw, b in f.boxes.items():
                self.send(f'* LIST ({" ".join(b["flags"])}) "/" "{raw}"')
        elif cmd in ("EXAMINE", "SELECT"):
            name = _tokens(args)[0]
            if f.need_id and not self.id_sent:
                self.send(f"{tag} NO Unsafe Login. Please contact kefu@188.com for help")
                return True
            b = f.boxes.get(name)
            if b is None:
                self.send(f"{tag} NO no such folder")
                return True
            self.box, self.readonly = name, cmd == "EXAMINE"
            for line in ("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)", f"* {len(b['msgs'])} EXISTS",
                         "* 0 RECENT", f"* OK [UIDVALIDITY {b['uidvalidity']}] UIDs valid",
                         "" if f.no_uidnext else f"* OK [UIDNEXT {b['next']}] next"):
                if line:
                    self.send(line)
            self.send(f"{tag} OK [{'READ-ONLY' if self.readonly else 'READ-WRITE'}] done")
            return True
        elif cmd == "STATUS":
            name = _tokens(args)[0]
            b = f.boxes[name]
            self.send(f'* STATUS "{name}" (UIDNEXT {b["next"]} UIDVALIDITY {b["uidvalidity"]} MESSAGES {len(b["msgs"])})')
        elif cmd == "UID SEARCH":
            self.send("* SEARCH" + "".join(f" {u}" for u in self.search(args)))
        elif cmd == "UID FETCH":
            self.fetch(args)
        elif cmd in ("UID STORE", "STORE"):
            for m in self.f.boxes[self.box]["msgs"]:
                m["flags"].add("\\Seen")
        elif cmd == "LOGOUT":
            self.send("* BYE bye")
            self.send(f"{tag} OK done")
            return False
        self.send(f"{tag} OK done")  # 别的 (COPY / MOVE / EXPUNGE / DELETE…) 照样回 OK: 测试看 commands
        return True

    def search(self, args):
        msgs, toks = self.f.boxes[self.box]["msgs"], _tokens(args)
        top = max((m["uid"] for m in msgs), default=0)
        if toks[0].upper() == "UID":
            want = _uidset(toks[1], top)
            got = [m for m in msgs if m["uid"] in want]
            if len(toks) > 3 and toks[2].upper() == "X-GM-RAW":
                if not self.f.gmail:
                    raise ValueError("不认识 X-GM-RAW")
                got = [m for m in got if m["category"] and f"category:{m['category']}" in toks[3]]
            return [m["uid"] for m in got]
        if toks[0].upper() == "SINCE":
            d = datetime.strptime(toks[1].replace(toks[1][3:6], str(M.MONTHS.index(toks[1][3:6]) + 1).zfill(2)), "%d-%m-%Y").date()
            return [m["uid"] for m in msgs if datetime.fromtimestamp(m["date"]).date() >= d]
        return [m["uid"] for m in msgs]

    def fetch(self, args):
        b = self.f.boxes[self.box]
        uidset, items = _tokens(args)[:2]
        top = max((m["uid"] for m in b["msgs"]), default=0)
        want, up = _uidset(uidset, top), items.upper()
        for seq, m in enumerate(b["msgs"], 1):
            if m["uid"] not in want:
                continue
            parts, literal = [], None
            if "RFC822.SIZE" in up:
                parts.append(f"RFC822.SIZE {len(m['raw'])}")
            if "INTERNALDATE" in up:
                parts.append(f"INTERNALDATE {imaplib.Time2Internaldate(m['date'])}")
            if "FLAGS" in up:
                parts.append(f"FLAGS ({' '.join(sorted(m['flags']))})")
            if "BODY.PEEK[HEADER]" in up:
                literal = ("BODY[HEADER]", m["raw"].split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
            elif got := re.search(r"BODY(\.PEEK)?\[\](<(\d+)\.(\d+)>)?", up):
                start = int(got[3] or 0)
                end = start + int(got[4]) if got[4] else len(m["raw"])
                literal = (f"BODY[]<{start}>" if got[2] else "BODY[]", m["raw"][start:end])
                if not got[1] and not self.readonly:
                    m["flags"].add("\\Seen")
            uid = f"UID {m['uid']}"
            head = " ".join(parts if self.f.uid_last and literal else [uid] + parts)
            if literal is None:
                self.send(f"* {seq} FETCH ({head})")
                continue
            name, data = literal
            self.w.write(f"* {seq} FETCH ({head} {name} {{{len(data)}}}\r\n".encode() + data
                         + (f" {uid})" if self.f.uid_last else ")").encode() + b"\r\n")


@pytest.fixture
def fake():
    server = FakeIMAP().start()
    yield server
    server.stop()


def mk(frm, subject, body="你好", *, to=ME, headers=None, html=None, calendar=False) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Date"] = email.utils.formatdate(T0, localtime=True)
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if calendar:
        m.add_attachment("BEGIN:VCALENDAR\nEND:VCALENDAR\n", subtype="calendar", filename="invite.ics")
    return m.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


class Owner:
    def __init__(self, ok=True):
        self.told, self.ok = [], ok

    async def tell(self, text):
        self.told.append(text)
        return self.ok


class Pet:
    def __init__(self):
        self.msgs, self.online = [], True

    async def notify(self, msg):
        self.msgs.append(msg)


def make_box(tmp_path, fake, settings=None, at=None, **kw):
    home = tmp_path / "mail"
    M.save_account(home, ME, CODE, "127.0.0.1", fake.port)
    M.save_settings(home, {"enabled": True, "quiet": "", "report_gap_min": 0, **(settings or {})}, local=True)
    owner, pet, clock = Owner(), Pet(), [at or T0]
    box = R.Mailbox(home, tell=owner.tell, notify=pet.notify, pet_online=lambda: pet.online, connector=fake.connector,
                    clock=lambda: clock[0], timeout=5, **kw)
    return box, owner, pet, clock


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 只读
# ---------------------------------------------------------------------------
def test_guard_stops_every_writing_command_before_it_is_sent(fake):
    fake.add(mk("a <a@b.com>", "hi"), at=T0)
    imap = M.guard(fake.connector({}, 5))
    imap.login(ME, CODE)
    imap.select('"INBOX"', readonly=True)
    typ, data = imap.uid("FETCH", "1", "(UID BODY.PEEK[HEADER])")  # 带 PEEK 的照常
    assert typ == "OK" and fake.flags() == [set()]
    before = list(fake.commands)
    attempts = [lambda: imap.store("1", "+FLAGS", "\\Seen"), lambda: imap.uid("STORE", "1", "+FLAGS", "(\\Seen)"),
                lambda: imap.uid("FETCH", "1", "(BODY[])"), lambda: imap.uid("FETCH", "1", "(RFC822)"),
                lambda: imap.uid("FETCH", "1", "(BODY[TEXT])"), lambda: imap.uid("COPY", "1", "Trash"),
                lambda: imap.uid("MOVE", "1", "Trash"), lambda: imap.fetch("1", "(BODY.PEEK[])"),
                lambda: imap.select('"INBOX"'), lambda: imap.expunge(), lambda: imap.copy("1", "Trash"),
                lambda: imap.delete("INBOX"), lambda: imap.create("x"), lambda: imap.rename("INBOX", "x"),
                lambda: imap.append("INBOX", None, None, b"Subject: x\r\n\r\nx"), lambda: imap.search(None, "ALL"),
                lambda: imap.close()]  # close 放最后: imaplib 拦下了也会把状态改成 AUTH
    for attempt in attempts:
        with pytest.raises(M.ReadOnlyViolation):
            attempt()
    assert fake.commands == before and fake.flags() == [set()]  # 一条都没发出去
    assert imap.literal is None  # append 挂的字面量丢掉了, 不会跟着下一条命令出去
    imap.logout()
    assert fake.commands[-1].split(" ", 1)[1] == "LOGOUT"


def test_poll_reads_only_sorts_and_reports_briefly(tmp_path, fake):
    fake.add_box("Junk", ("\\HasNoChildren", "\\Junk"))
    fake.add_box(ADS)
    fake.add_box("Sent Messages")
    for i in range(2):
        fake.add(mk("x <x@spam.biz>", f"发票代开 {i}"), at=T0 - 300, box="Junk")
    fake.add(mk("y <y@ad.cn>", "广告"), at=T0 - 300, box=ADS)
    rows = [  # (发件人, 主题, 信头, 正文, 别的)
        ("老同学 <old@163.com>", "三天前的信", {}, "好久不见", {"days": 3}),
        ("极客周刊 <newsletter@geek.com>", "本周精选: 10 篇好文", {"List-Unsubscribe": "<mailto:u@geek.com>"}, "精选", {}),
        ("某商城 <service@shop.com>", "【广告】双11大促, 全场五折", {}, "快来买", {}),
        ("活动通知 <notice@events.com>", "活动报名开始", {"Precedence": "bulk"}, "报名", {}),
        ("x <x@spam.biz>", "你中奖了", {"X-Spam-Flag": "YES"}, "领奖", {}),
        ("张三 <zhangsan@163.com>", "周末一起吃饭吗", {}, "周六中午有空吗?", {}),
        ("Google <no-reply@accounts.google.com>", "安全提醒: 有新设备登录了你的账号", {}, "如果不是你", {}),
        ("招商银行信用卡 <ccsvc@message.cmbchina.com>", "招商银行信用卡电子账单 (2026年10月)", {}, "本期应还 2,345.00 元", {}),
        ("某服务 <noreply@svc.com>", "您的验证码是 384756", {}, "5 分钟内有效", {}),
        ("octo <notifications@github.com>", "[org/repo] Fix login bug (PR #12)",
         {"X-GitHub-Reason": "review_requested", "List-Unsubscribe": "<mailto:x@github.com>", "List-Id": "org/repo"}, "review", {}),
        ("octo <notifications@github.com>", "[org/repo] Release v2.0 published",
         {"X-GitHub-Reason": "subscribed", "List-Unsubscribe": "<mailto:x@github.com>", "List-Id": "org/repo"}, "release", {}),
        ("Medium <noreply@medium.com>", "Stories for you", {}, "story\n\nUnsubscribe from these emails", {}),
        ("李四 <lisi@corp.com>", "自动回复: 周末一起吃饭吗", {"Auto-Submitted": "auto-replied"}, "我在休假", {}),
        ("王五 <wangwu@corp.com>", "项目同步", {}, "下周二同步一下", {"calendar": True}),
        ("老板 <boss@corp.com>", "紧急: 明早九点前把报告发我", {}, "别忘了", {}),
        ("QQ邮箱团队 <security@qq-mail-safe.cn>", "账号异常, 请立即验证",
         {"Authentication-Results": "mx.qq.com; spf=pass; dmarc=fail"}, "点这里 https://evil.example/x", {}),
    ]
    for i, (frm, subject, headers, body, extra) in enumerate(rows):
        fake.add(mk(frm, subject, body, headers=headers, calendar=extra.get("calendar", False)),
                 at=T0 - extra.get("days", 0) * 86400 - 600 + i * 10)
    box, owner, pet, clock = make_box(tmp_path, fake)

    async def main():
        rec = await box.poll()
        await box.close()
        return rec
    rec = run(main())
    assert rec["ok"] and rec["read"] == 15 and rec["first"] == ["INBOX"]
    assert rec["ignored"] == {"junk": 1, "promo": 4, "junk_folder": 0}
    assert (rec["low"], rec["important"], rec["urgent"]) == (3, 5, 2)
    # 只读: 只发了只读的命令, 取信都带 PEEK, 信一封都没被标成已读
    assert set(fake.names()) <= {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "STATUS", "LOGOUT"}
    assert all("PEEK" in c for c in fake.commands if " UID FETCH " in f" {c.upper()} ")
    assert not any("\\Seen" in f for f in fake.flags())
    # 垃圾箱和广告文件夹没打开过, 只问了个数
    assert not any(c.split(" ", 2)[1] == "EXAMINE" and ("Junk" in c or ADS in c) for c in fake.commands)
    assert {c.split(" ", 2)[2].split(" ")[0].strip('"') for c in fake.commands if c.split(" ", 2)[1] == "STATUS"} == {"Junk", ADS}
    # 要紧的单独说, 要看的攒成一份, 最多念 3 件; 像冒充的不单独打扰, 排在汇总最后 (这次没念到, 只算在个数里)
    urgent, digest = owner.told[:2], owner.told[2]
    assert len(owner.told) == 3 and all(t.startswith("主人, 有封急的邮件") for t in urgent)
    assert any("安全提醒" in t and "别点邮件里的链接" in t for t in urgent)
    assert any("老板" in t for t in urgent)
    phish = next(i for i in rec["items"] if i["subject"].startswith("账号异常"))
    assert phish["level"] == "important" and "可能是冒充的" in phish["warn"]
    assert digest.startswith("主人~ 邮箱我接好啦") and digest.count("\n") == 4  # 开头 + 3 件 + 末尾
    assert "还有 2 封也算要看的" in digest and "3 封普通通知" in digest and "5 封广告和垃圾邮件我没管" in digest
    assert "账单" in digest and "Fix login bug" in digest and "会议日程" in digest
    for text in owner.told:  # 推送、垃圾的主题一个字都不说; 冒充信里的链接不转述
        for word in ("本周精选", "双11", "活动报名", "中奖", "Release v2.0", "验证码", "384756", "Stories for you", "三天前",
                     "evil.example"):
            assert word not in text
    assert [m["channel"] for m in pet.msgs] == ["mail"] * 3 and pet.msgs[2]["text"] == digest
    assert all(r["via"] == "chat+pet" for r in rec["reported"])
    # 留痕
    log = box.log_tail(5)
    assert log[0]["read"] == 15 and len(log[0]["reported"]) == 3
    assert {x["why"] for x in log[0]["ignored_from"]} >= {"带退订头 (List-Unsubscribe)", "主题标着广告"}
    assert all("subject" not in x for x in log[0]["ignored_from"])  # 忽略的不记主题
    st = box.status()
    assert st["today"]["read"] == 15 and st["today"]["reported"] == 3 and st["pending"] == 0


def test_later_polls_only_see_new_mail(tmp_path, fake):
    fake.add_box("Junk", ("\\HasNoChildren", "\\Junk"))
    fake.add(mk("张三 <zhangsan@163.com>", "第一封"), at=T0 - 60)
    box, owner, pet, clock = make_box(tmp_path, fake)

    async def main():
        one = await box.poll()
        clock[0] += 21 * 60
        fake.add(mk("x <x@spam.biz>", "垃圾"), at=clock[0], box="Junk")
        two = await box.poll()  # 没有新信: 服务器对 "2:*" 照样回最后一封, 不能当新信
        clock[0] += 21 * 60
        fake.add(mk("李雷 <lilei@126.com>", "下周的会挪到周三"), at=clock[0])
        three = await box.poll()
        await box.close()
        return one, two, three
    one, two, three = run(main())
    assert one["read"] == 1 and two["read"] == 0 and three["read"] == 1
    assert two["ignored"]["junk_folder"] == 1 and two["reported"] == []
    assert len(owner.told) == 2 and "下周的会挪到周三" in owner.told[1] and "第一封" not in owner.told[1]
    assert "另外 1 封广告和垃圾邮件我没管" in owner.told[1]


def test_uidvalidity_change_starts_over_without_backlog(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "旧信"), at=T0 - 5 * 86400)
    fake.add(mk("张三 <zhangsan@163.com>", "近的信"), at=T0 - 60)
    box, owner, _, clock = make_box(tmp_path, fake)

    async def main():
        await box.poll()
        fake.boxes["INBOX"]["uidvalidity"] = 99  # 服务器重建了文件夹, UID 全变了
        clock[0] += 3600
        rec = await box.poll()
        await box.close()
        return rec
    rec = run(main())
    assert rec["first"] == ["INBOX"] and rec["read"] == 1  # 又起了一次头: 只看最近一天的, 旧信不翻
    st = json.loads((tmp_path / "mail" / "state.json").read_text(encoding="utf-8"))
    assert st["version"] == 2 and st["accounts"][ME]["folders"]["INBOX"]["uidvalidity"] == 99


def test_without_uidnext_old_mail_is_not_read_later(tmp_path):
    fake = FakeIMAP(no_uidnext=True).start()
    try:
        for i in range(3):
            fake.add(mk("张三 <zhangsan@163.com>", f"旧信 {i}"), at=T0 - 5 * 86400)
        box, owner, _, clock = make_box(tmp_path, fake)

        async def main():
            one = await box.poll()
            clock[0] += 3600
            two = await box.poll()  # 起头时记下了最大的 UID: 旧信不会在第二次被当成新信
            await box.close()
            return one, two
        one, two = run(main())
        assert one["read"] == 0 and two["read"] == 0 and owner.told == []
    finally:
        fake.stop()


def test_uid_after_literal_and_163_id_command(tmp_path):
    fake = FakeIMAP(need_id=True, uid_last=True).start()  # 网易: 不先报客户端就打不开文件夹; UID 放在字面量后面
    try:
        fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
        box, owner, _, _ = make_box(tmp_path, fake)
        rec = run(box.poll())
        assert not rec["ok"] and "Unsafe Login" in rec["error"]["text"]
        account = json.loads((tmp_path / "mail" / "account.json").read_text(encoding="utf-8"))
        account["id_command"] = True
        M.write_json(tmp_path / "mail" / "account.json", account)

        async def again():
            rec = await box.poll(manual=True)
            await box.close()
            return rec
        rec = run(again())
        assert rec["ok"] and rec["read"] == 1 and "周末一起吃饭吗" in owner.told[-1]
        assert any(c.split(" ", 2)[1] == "ID" for c in fake.commands)
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# 分拣规则
# ---------------------------------------------------------------------------
def sort_one(raw, settings=None):
    m = M.mail_from(1, {}, raw.split(b"\r\n\r\n", 1)[0], "INBOX")
    m.text, m.attachments, m.calendar, m.unsub = M.body_of(raw)
    return M.classify(m, {**M.DEFAULTS, **(settings or {})})


def test_owner_lists_win_over_rules():
    news = mk("银行 <news@bank.com>", "11 月理财精选", headers={"List-Unsubscribe": "<mailto:x@bank.com>"})
    assert sort_one(news).level == "promo"
    vip = sort_one(news, {"important_senders": ["@bank.com"]})
    assert vip.level == "important" and vip.reasons == ["你说过重要的发件人"]
    friend = mk("张三 <zhangsan@163.com>", "周末一起吃饭吗")
    assert sort_one(friend, {"ignore_senders": ["zhangsan@163.com"]}).level == "promo"
    assert sort_one(friend, {"ignore_keywords": ["吃饭"]}).level == "promo"
    note = mk("系统 <noreply@ci.example.com>", "构建完成: worldmodel #812")
    assert sort_one(note).level == "low"
    assert sort_one(note, {"important_keywords": ["worldmodel"]}).level == "important"


@pytest.mark.parametrize("raw, level, kind", [
    (mk("Apple <no_reply@email.apple.com>", "Apple ID 的密码已重置"), "urgent", "安全提醒"),
    (mk("中国移动 <10086@139.com>", "您的号码已欠费停机"), "urgent", "欠费逾期"),
    (mk("HR <hr@corp.com>", "面试邀请: 周四下午三点"), "important", "工作"),
    (mk("携程 <service@ctrip.com>", "航班变更通知 MU5101"), "important", "出行"),
    (mk("系统 <noreply@x.com>", "订单已发货"), "low", "快递订单"),
    (mk("营销 <edm@shop.com>", "限时折扣"), "promo", ""),
    (mk("小红 <xiaohong@qq.com>", "看看这个", html="<p>好玩</p><style>p{}</style>"), "important", "私人来信"),
    (mk("同事 <tongshi@corp.com>", "周会", calendar=True), "important", "会议日程"),
    (mk("同事 <tongshi@corp.com>", "资料", "第一行\n> 引用的旧信\n在 2026-10-31 写道:\n旧的"), "important", "私人来信"),
    (mk("小明 <xiaoming@163.com>", "紧急: 快递帮我取一下"), "urgent", "快递订单"),  # 人写的: 提到快递也不降成普通
    (mk("某平台 <hello@platform.com>", "本周回顾", "你的账号异常吗? 看看这些新功能"), "important", "安全提醒"),  # 只在正文里: 不算急
])
def test_rule_table(raw, level, kind):
    m = sort_one(raw)
    assert (m.level, m.kind) == (level, kind), m.reasons


def test_headers_in_chinese_charsets_and_spoofed_names():
    from email.header import Header
    raw = (b"From: =?gb2312?B?1cXI/Q==?= <zhangsan@163.com>\r\nTo: me@qq.com\r\nSubject: "
           + Header("周末一起吃饭吗?", "gbk").encode().encode("ascii") + b"\r\n\r\nbody")
    m = M.mail_from(1, {}, raw, "INBOX")
    assert (m.sender, m.address, m.subject) == ("张三", "zhangsan@163.com", "周末一起吃饭吗?")
    raw8 = "From: 李四 <lisi@corp.com>\r\nSubject: 直接塞的 GBK 字节\r\n\r\n".encode("gbk")  # 没按 RFC 2047 编码
    m = M.mail_from(1, {}, raw8, "INBOX")
    assert m.sender == "李四" and m.subject == "直接塞的 GBK 字节"
    raw = b'From: "=?utf-8?B?5byg5LiJLCDnu4/nkIY=?=" <zs@corp.com>\r\nSubject: =?x-unknown?B?aGk=?=\r\n\r\n'
    m = M.mail_from(1, {}, raw, "INBOX")
    assert (m.sender, m.address, m.subject) == ("张三, 经理", "zs@corp.com", "hi")  # 名字里有逗号也拆得对
    for frm in (b'"service@icbc.com.cn" <x@evil.cn>', b"service@icbc.com.cn <x@evil.cn>"):  # 加不加引号都认得出
        spoof = M.mail_from(1, {}, b"From: " + frm + b"\r\nSubject: s\r\n\r\n", "INBOX")
        assert spoof.address == "x@evil.cn" and "对不上" in M.suspicious(spoof)
    same = M.mail_from(1, {}, b'From: "support@mail.example.com" <x@example.com>\r\nSubject: s\r\n\r\n', "INBOX")
    assert M.suspicious(same) == ""  # 同一家的子域名不算


def test_folder_names_and_junk_detection():
    assert M.mutf7_decode("&UXZO1mWHTvZZOQ-") == "其他文件夹"
    assert M.mutf7_decode(ADS) == "其他文件夹/广告邮件" and M.mutf7_encode("其他文件夹/广告邮件") == ADS
    assert M.mutf7_encode("A&B") == "A&-B" and M.mutf7_decode("A&-B") == "A&B"
    junk = M.parse_list(b'(\\HasNoChildren \\Junk) "/" "Spam Box"')
    assert junk.junk and junk.name == "Spam Box"
    assert M.parse_list(f'(\\HasNoChildren) "/" "{ADS}"'.encode()).junk
    assert not M.parse_list(b'(\\HasNoChildren) "/" "INBOX"').junk
    lit = M.parse_list((b'(\\HasNoChildren) "/" {11}', b'Sent "Mail"'))
    assert lit.name == 'Sent "Mail"' and not lit.junk


def test_settings_are_checked_and_cloud_only_opens_locally(tmp_path):
    for bad in ({"interval_min": 1}, {"interval_min": True}, {"via": "group"}, {"quiet": "25:00-08:00"},
                {"folders": []}, {"nope": 1}, {"important_senders": ["x" * 200]}):
        with pytest.raises(ValueError):
            M.clean_settings(bad, local=True)
    with pytest.raises(ValueError, match="本机终端"):
        M.save_settings(tmp_path, {"summary": "cloud"})
    assert M.load_settings(tmp_path, {"summary": "cloud", "interval_min": 30})["summary"] == "off"  # 配置文件也开不了
    M.save_settings(tmp_path, {"summary": "cloud"}, local=True)
    s = M.load_settings(tmp_path, {"interval_min": 30})
    assert s["summary"] == "cloud" and s["interval_min"] == 30
    M.save_settings(tmp_path, {"summary": "off"})  # 关哪里都能关
    assert M.load_settings(tmp_path)["summary"] == "off"


def test_quiet_hours_wrap_midnight():
    at = lambda h, m=0: datetime(2026, 11, 2, h, m).timestamp()
    assert M.in_quiet(at(23, 30), "23:00-08:00") and M.in_quiet(at(7, 59), "23:00-08:00")
    assert not M.in_quiet(at(8), "23:00-08:00") and not M.in_quiet(at(12), "23:00-08:00")
    assert M.in_quiet(at(13), "12:00-14:00") and not M.in_quiet(at(13), "") and not M.in_quiet(at(13), "08:00-08:00")


def test_report_text_is_cleaned_and_model_input_masked():
    text = R.plain("看【表情包:开心】这个 https://evil.example/a?b \u202egpj.exe\u200b  好", 60)
    assert text == "看这个 [链接] gpj.exe 好"
    raw = ("张三 zhang.san@corp.com 13812345678 身份证 11010119900307123X 卡号 6222 0212 3456 7890 123 "
           "验证码: 384756 座机 010-62345678 金额 2,345.00 元 http://x.cn/p 11 月 8 日前")
    out = R.masked(raw)
    for secret in ("zhang.san@corp.com", "13812345678", "11010119900307123X", "6222 0212 3456 7890 123", "384756",
                   "010-62345678", "http://x.cn/p"):
        assert secret not in out
    assert "2,345.00 元" in out and "11 月 8 日前" in out and "[码]" in out


# ---------------------------------------------------------------------------
# 低打扰、开关
# ---------------------------------------------------------------------------
def test_digests_keep_their_gap_and_quiet_hours_hold_them(tmp_path, fake):
    night = datetime(2026, 11, 2, 23, 30).timestamp()
    fake.add(mk("张三 <zhangsan@163.com>", "晚上的信"), at=night - 60)
    fake.add(mk("Google <no-reply@accounts.google.com>", "安全提醒: 新设备登录"), at=night - 30)
    box, owner, _, clock = make_box(tmp_path, fake, {"quiet": "23:00-08:00", "report_gap_min": 120}, at=night)

    async def main():
        await box.poll()
        told_at_night = list(owner.told)
        clock[0] = datetime(2026, 11, 3, 8, 1).timestamp()  # 免打扰过了: 不用等下一次读信, tick 就把攒着的发出去
        M.save_settings(box.home, {"interval_min": 1440}, local=True)
        await box.tick()
        after_quiet = list(owner.told)
        clock[0] += 600
        fake.add(mk("李雷 <lilei@126.com>", "又一封"), at=clock[0])
        await box.poll()  # 离上一份汇总才 10 分钟: 攒着
        within_gap = list(owner.told)
        clock[0] += 120 * 60
        await box.tick()
        await box.close()
        return told_at_night, after_quiet, within_gap, list(owner.told)
    night_told, after_quiet, within_gap, final = run(main())
    assert len(night_told) == 1 and "安全提醒" in night_told[0]  # 夜里只说了要紧的
    assert len(after_quiet) == 2 and "晚上的信" in after_quiet[1]
    assert within_gap == after_quiet
    assert len(final) == 3 and "又一封" in final[2]


def test_quiet_urgent_off_holds_urgent_too(tmp_path, fake):
    night = datetime(2026, 11, 2, 23, 30).timestamp()
    fake.add(mk("Google <no-reply@accounts.google.com>", "安全提醒: 新设备登录"), at=night - 30)
    box, owner, _, _ = make_box(tmp_path, fake, {"quiet": "23:00-08:00", "quiet_urgent": False}, at=night)

    async def main():
        rec = await box.poll()
        await box.close()
        return rec
    rec = run(main())
    assert owner.told == [] and rec["pending"] == 1


def test_urgent_capped_per_hour(tmp_path, fake):
    for i in range(5):
        fake.add(mk(f"老板 <boss{i}@corp.com>", f"紧急: 第 {i} 件事"), at=T0 - 60 + i)
    box, owner, _, _ = make_box(tmp_path, fake)

    async def main():
        await box.poll()
        await box.close()
    run(main())
    assert len(owner.told) == 4 and sum(t.startswith("主人, 有封急的邮件") for t in owner.told) == 3
    assert owner.told[3].count("【急】") == 2  # 剩下两件并进汇总, 排在前面


def test_off_or_not_configured_never_connects(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "hi"), at=T0 - 60)
    box, owner, _, _ = make_box(tmp_path, fake, {"enabled": False})
    empty = R.Mailbox(tmp_path / "empty", connector=fake.connector)

    async def main():
        out = await box.tick(), await empty.tick()
        with pytest.raises(M.MailError, match="还没设置邮箱"):
            await empty.poll()
        await box.close()
        await empty.close()
        return out
    assert run(main()) == (None, None)
    assert fake.commands == [] and owner.told == []


def test_via_pet_keeps_mail_on_this_computer_and_retries_undelivered(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
    box, owner, pet, clock = make_box(tmp_path, fake, {"via": "pet"})
    pet.online = False

    async def main():
        rec = await box.poll()
        pet.online = True
        clock[0] += 60
        await box.tick()
        await box.close()
        return rec
    rec = run(main())
    assert rec["reported"] == [] and rec["pending"] == 1  # 桌宠没连着: 留着
    assert owner.told == [] and len(pet.msgs) == 1 and "周末一起吃饭吗" in pet.msgs[0]["text"]
    assert box.log_tail(1)[0]["kind"] == "report"


def test_only_one_process_reads_a_mailbox(tmp_path, fake):
    box, _, _, _ = make_box(tmp_path, fake)
    other = R.Mailbox(box.home, connector=fake.connector, startup_delay=3600)

    async def main():
        await box.start()
        await other.start()
        with pytest.raises(M.MailError, match="另一个服务端"):
            await other.poll()
        why, here = other.why_off, other.status()["here"]
        await box.close()
        rec = await other.poll()  # 那边停了: 这边拿到锁接着读
        await other.close()
        return why, here, rec
    why, here, rec = run(main())
    assert "另一个服务端" in why and not here and rec["ok"]


# ---------------------------------------------------------------------------
# 失败时老实说
# ---------------------------------------------------------------------------
def test_login_refused_is_said_once_and_not_retried_until_fixed(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "hi"), at=T0 - 60)
    fake.password = "the-right-one"
    box, owner, _, clock = make_box(tmp_path, fake)

    async def main():
        first = await box.poll()
        for _ in range(3):
            clock[0] += 3600
            await box.tick()
        logins = fake.logins
        waiting = box.status()["waiting_for_owner"]
        fake.password = CODE
        await asyncio.sleep(0.01)
        M.save_account(box.home, ME, CODE, "127.0.0.1", fake.port)  # 你重新填了授权码
        clock[0] += 3600
        fixed = await box.tick()
        await box.close()
        return first, logins, waiting, fixed
    first, logins, waiting, fixed = run(main())
    assert not first["ok"] and first["error"]["kind"] == "auth" and "AUTHENTICATIONFAILED" in first["error"]["text"]
    assert logins == 1 and waiting  # 不自动重试, 免得把号试锁
    assert fixed["ok"] and owner.told[0].startswith("主人, 我登不上邮箱了")
    assert M.PAGE_URL in owner.told[0] and "工作台左栏的「邮箱」" in owner.told[0]  # 指向页面: 装好的电脑上不一定有 python
    assert sum("登不上" in t for t in owner.told) == 1 and "又能读了" in owner.told[1]


def test_broken_account_file_is_said_once(tmp_path, fake):
    box, owner, _, clock = make_box(tmp_path, fake)
    (box.home / "account.json").write_text("{坏了", encoding="utf-8")

    async def main():
        first = await box.tick()
        clock[0] += 3600
        again = await box.tick()
        await box.close()
        return first, again
    first, again = run(main())
    assert not first["ok"] and first["error"]["kind"] == "config" and again is None
    assert len(owner.told) == 1 and "存好的邮箱设置用不了了" in owner.told[0] and fake.logins == 0


def test_network_trouble_said_only_after_hours(tmp_path, fake):
    box, owner, _, clock = make_box(tmp_path, fake)

    def refused(account, timeout):  # 断网 / 服务器不理: 和连关着的端口一样抛 OSError (不真去连, Windows 上要等一秒多)
        raise ConnectionRefusedError(10061, "由于目标计算机积极拒绝, 无法连接")
    box.connector = refused

    async def main():
        recs = [await box.poll()]
        for _ in range(4):
            clock[0] += 3600
            recs.append(await box.poll())
        await box.close()
        return recs
    recs = run(main())
    assert all(not r["ok"] and r["error"]["kind"] == "network" for r in recs)
    assert len(owner.told) == 1 and "个多小时我一直读不到邮箱" in owner.told[0]
    assert recs[3]["reported"] and not recs[4]["reported"]
    st = box.status()
    assert st["error"]["kind"] == "network" and st["error"]["count"] == 5 and not st["waiting_for_owner"]


def test_secret_never_leaves_the_account_file(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
    box, owner, pet, _ = make_box(tmp_path, fake)

    async def main():
        await box.poll()
        await box.close()
    run(main())
    blob = json.loads((box.home / "account.json").read_text(encoding="utf-8"))["secret"]
    assert M.secret_of({"secret": blob}) == CODE
    for path in box.home.rglob("*"):
        if path.is_file():
            assert CODE not in path.read_text(encoding="utf-8", errors="replace"), path
    shown = json.dumps([box.status(), box.log_tail(10), owner.told, pet.msgs], ensure_ascii=False)
    assert CODE not in shown and ME not in shown and M.mask_address(ME) in shown


# ---------------------------------------------------------------------------
# 一句话摘要 (默认关; 测试里是假模型, 不花钱)
# ---------------------------------------------------------------------------
class FakeModel:
    def __init__(self, reply=None, fail=False):
        self.calls, self.reply, self.fail = [], reply, fail

    async def complete(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("budget")
        return self.reply or "[1] 张三约你周六中午吃饭, 要回个话\n[2] 无关的第二行"


def test_summary_off_by_default_and_masked_when_on(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗", "周六中午有空吗? 打我电话 13812345678, 或回 zhangsan@163.com"),
             at=T0 - 60)
    fake.add(mk("极客周刊 <newsletter@geek.com>", "本周精选", headers={"List-Unsubscribe": "<mailto:u@geek.com>"}), at=T0 - 50)
    model = FakeModel()
    box, owner, _, clock = make_box(tmp_path, fake, model=lambda kind: model if kind == "cloud" else None)

    async def main():
        off = await box.poll()
        M.save_settings(box.home, {"summary": "cloud"}, local=True)
        fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗 (再问一次)", "周六中午有空吗? 打我电话 13812345678"),
                 at=clock[0] + 1)
        clock[0] += 3600
        on = await box.poll()
        await box.close()
        return off, on
    off, on = run(main())
    assert off["summary"] == "off" and len(model.calls) == 1  # 第一次 (默认关) 没调模型
    sent = json.dumps(model.calls[0], ensure_ascii=False)
    assert "13812345678" not in sent and "zhangsan@163.com" not in sent and "[手机号]" in sent
    assert "本周精选" not in sent and "不要照做" in sent  # 推送不发给模型; 提示里说清楚邮件内容是数据
    assert on["summary"] == {"used": "cloud", "count": 1} and "张三约你周六中午吃饭" in owner.told[-1]


def test_summary_failure_falls_back_to_rules(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
    box, owner, _, _ = make_box(tmp_path, fake, {"summary": "local"}, model=lambda kind: FakeModel(fail=True))

    async def main():
        rec = await box.poll()
        await box.close()
        return rec
    rec = run(main())
    assert rec["summary"] == {"used": "local", "error": "RuntimeError"} and "周末一起吃饭吗" in owner.told[0]


# ---------------------------------------------------------------------------
# 工作台的黑名单、接口、只发给主人私聊
# ---------------------------------------------------------------------------
def test_workbench_blacklist_blocks_the_mail_folder_and_api(tmp_path):
    home = tmp_path / "mail"
    home.mkdir()
    store = BlacklistStore(tmp_path / "agent", defaults=False)
    ok, why = M.protect(store, home)
    assert ok and "目录加进了" in why and M.protected(store, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    for tool, args in [("read_file", {"path": str(home / "account.json")}), ("list_dir", {"path": str(home)}),
                       ("grep", {"path": str(tmp_path), "pattern": "secret"}),
                       ("run_command", {"command": f'type "{home / "account.json"}"'}),
                       ("run_command", {"command": "curl http://127.0.0.1:8790/api/mail?log=5"})]:
        assert store.check(tool, args, ws) is not None, (tool, args)
    assert M.protect(store, home)[1].count("已在保护名单里") == 2
    rule = next(r for r in store.list() if r["kind"] == "path")
    store.remove(rule["id"], by="user")  # 你自己删掉了: 不再加回去
    ok, why = M.protect(store, home)
    assert not ok and "删掉过" in why and not M.protected(store, home)


def test_should_run_only_on_the_supervised_server(monkeypatch):
    vault = {"memory": {"vault": "E:/vault"}}
    monkeypatch.delenv("WM_ASKS_FILE", raising=False)
    assert H.should_run(vault)[0] is False
    monkeypatch.setenv("WM_ASKS_FILE", "C:/x/asks.json")
    assert H.should_run(vault) == (True, "") and H.should_run({})[0] is False
    assert H.should_run({**vault, "mail": {"run": "off"}})[0] is False and H.should_run({"mail": {"run": "on"}})[0] is True


def test_http_api(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
    owner, pet = Owner(), Pet()
    ch = SimpleNamespace(_tell_owner=owner.tell, notify=pet.notify, listeners=[object()], cloud=None)
    home = tmp_path / "mail"
    M.save_account(home, ME, CODE, "127.0.0.1", fake.port)

    async def main():
        app = web.Application()
        box = H.register(app, {"mail": {"home": str(home), "run": "on", "quiet": ""}},
                         lambda r: "Host 不对" if r.headers.get("X-Bad") else None, SimpleNamespace(channels=ch))
        box.connector = fake.connector
        async with TestClient(TestServer(app)) as client:
            st = await (await client.get("/api/mail")).json()
            bad = await client.get("/api/mail", headers={"X-Bad": "1"})
            cloud = await client.post("/api/mail", json={"action": "settings", "changes": {"summary": "cloud"}})
            on = await client.post("/api/mail", json={"action": "settings", "changes": {"enabled": True, "report_gap_min": 0}})
            check = await (await client.post("/api/mail", json={"action": "check"})).json()
            logged = await (await client.get("/api/mail?log=5")).json()
            junk = await client.post("/api/mail", json={"action": "nope"})
            return st, bad.status, cloud.status, await cloud.json(), on.status, check, logged, junk.status, box.running
    st, bad, cloud, cloud_body, on, check, logged, junk, running = run(main())
    assert st["ok"] and st["here"] and st["configured"] and st["account"]["address"] == "me***@qq.com"
    assert CODE not in json.dumps(st) and ME not in json.dumps(st)
    assert bad == 403 and cloud == 400 and "本机终端" in cloud_body["error"] and on == 200 and junk == 400
    assert check["ok"] and check["record"]["read"] == 1 and "周末一起吃饭吗" in owner.told[0]
    assert logged["log"][0]["read"] == 1 and logged["settings"]["enabled"] is True
    assert running


def test_real_server_wires_mail_but_test_servers_never_read(tmp_path, monkeypatch):
    """app.py 挂上了 /api/mail (查 Host 和 Origin); 测试起的服务端 (没有看守给的 WM_ASKS_FILE、没开记忆库) 不读信."""
    from test_pipeline import make_cfg
    from wm.app import create_app
    monkeypatch.setattr(M, "HOME", tmp_path / "mail")
    M.save_account(tmp_path / "mail", ME, CODE, "127.0.0.1", 9)
    M.save_settings(tmp_path / "mail", {"enabled": True}, local=True)

    async def main():
        app = create_app(make_cfg(tmp_path / "logs"))
        async with TestClient(TestServer(app)) as client:
            st = await (await client.get("/api/mail")).json()
            evil = await client.get("/api/mail", headers={"Origin": "http://evil.example"})
            check = await client.post("/api/mail", json={"action": "check"})
            return app[H.KEY], st, evil.status, check.status, await check.json()
    box, st, evil, status, body = run(main())
    assert box.home == tmp_path / "mail" and not box.run and box._task is None
    assert st["ok"] and st["configured"] and not st["here"] and "正式服务端" in st["why_off"]
    assert evil == 403 and status == 400 and "不读邮箱" in body["error"]
    assert not (tmp_path / "mail" / "state.json").exists()  # 一次都没读


def test_reports_only_go_to_the_owner_private_chat(tmp_path, fake):
    """真的渠道管理器 (假 OneBot): 汇报走 _tell_owner, 只发主人的私聊, 群里一个字都没有; 桌宠那边收到 channel_notice."""
    from test_channels import GA, OWNER, run_qq
    from wm.app import ENGINES
    fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
    fake.add(mk("Google <no-reply@accounts.google.com>", "安全提醒: 新设备登录"), at=T0 - 30)

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        ch = bot.app[ENGINES].channels
        home = tmp_path / "mail"
        M.save_account(home, ME, CODE, "127.0.0.1", fake.port)
        M.save_settings(home, {"enabled": True, "quiet": "", "report_gap_min": 0}, local=True)
        box = R.Mailbox(home, tell=ch._tell_owner, notify=ch.notify, pet_online=lambda: bool(ch.listeners),
                        connector=fake.connector, timeout=5)
        rec = await box.poll()
        await box.close()
        await inbox.get("channel_notice", lambda m: m.get("channel") == "mail" and "周末一起吃饭吗" in m["text"])
        return rec, list(bot.sent)
    rec, sent = run_qq(tmp_path, scenario)
    assert [r["via"] for r in rec["reported"]] == ["chat+pet", "chat+pet"]
    assert [a for a, _ in sent] == ["send_private_msg", "send_private_msg"]
    assert all(p["user_id"] == OWNER for _, p in sent) and not any(p.get("group_id") == GA for _, p in sent)


# ---------------------------------------------------------------------------
# 设置工具 tools/mail_setup.py (输入换成假的; 连本机的假服务端)
# ---------------------------------------------------------------------------
def _setup_tool():
    spec = importlib.util.spec_from_file_location("mail_setup", ROOT / "tools" / "mail_setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_setup_tool(tmp_path, fake):
    tool = _setup_tool()
    cfg = {"mail": {"home": str(tmp_path / "mail")}, "agent": {"home": str(tmp_path / "agent")}}
    said, answers = [], iter([ME, "y"])
    io = tool.IO(inp=lambda prompt="": next(answers), secret_in=lambda prompt="": " abcd efgh ijkl mnop ", out=said.append,
                 tty=lambda: True, connector=fake.connector)
    assert tool.main(["setup", "--host", "127.0.0.1", "--port", str(fake.port)], io, cfg) == 0
    text = "\n".join(said)
    assert "登上了: 收件箱里有 0 封" in text and "目录加进了工作台的保护名单" in text and "授权码不是 QQ 密码" in text
    home = tmp_path / "mail"
    assert CODE not in (home / "account.json").read_text(encoding="utf-8")
    assert M.load_settings(home)["enabled"] is True
    assert M.protected(BlacklistStore(tmp_path / "agent"), home)
    assert any(c.split(" ", 2)[1] == "EXAMINE" for c in fake.commands) and "SELECT" not in fake.names()

    said.clear()
    no = tool.IO(inp=lambda prompt="": "n", out=said.append)
    assert tool.main(["set", "summary=cloud"], no, cfg) == 1 and M.load_settings(home)["summary"] == "off"
    assert "打码" in said[0]
    yes = tool.IO(inp=lambda prompt="": "y", out=said.append)
    assert tool.main(["set", "summary=cloud", "important_senders=boss@corp.com,@bank.com", "interval_min=30"], yes, cfg) == 0
    s = M.load_settings(home)
    assert s["summary"] == "cloud" and s["important_senders"] == ["boss@corp.com", "@bank.com"] and s["interval_min"] == 30
    assert tool.main(["set", "via=group"], yes, cfg) == 2

    said.clear()
    assert tool.main(["status"], tool.IO(out=said.append), cfg) == 0
    assert "me***@qq.com" in "\n".join(said) and CODE not in "\n".join(said) and "工作台保护: 挡着" in "\n".join(said)
    assert tool.main(["test"], tool.IO(out=said.append, connector=fake.connector), cfg) == 0

    said.clear()
    pipe = tool.IO(inp=lambda prompt="": ME, secret_in=lambda prompt="": CODE, out=said.append, tty=lambda: False)
    assert tool.main(["setup", "--host", "127.0.0.1", "--port", str(fake.port)], pipe, cfg) == 2
    assert "PowerShell" in said[-1]
    assert tool.main(["forget", "--yes"], tool.IO(out=said.append), cfg) == 0
    assert M.load_account(home) is None and M.load_settings(home)["enabled"] is False


def test_setup_tool_refuses_bad_codes_and_outlook(tmp_path, fake):
    tool = _setup_tool()
    cfg = {"mail": {"home": str(tmp_path / "mail")}, "agent": {"home": str(tmp_path / "agent")}}
    said = []
    io = tool.IO(inp=lambda prompt="": ME, secret_in=lambda prompt="": "wrong-code", out=said.append, tty=lambda: True,
                 connector=fake.connector)
    assert tool.main(["setup", "--host", "127.0.0.1", "--port", str(fake.port)], io, cfg) == 1
    assert "没登上" in "\n".join(said) and "不是邮箱 / QQ 的登录密码" in "\n".join(said)
    assert M.load_account(tmp_path / "mail") is None
    said.clear()
    out = tool.IO(inp=lambda prompt="": "someone@outlook.com", out=said.append, tty=lambda: True)
    assert tool.main(["setup"], out, cfg) == 2 and "OAuth" in said[0]


# ---------------------------------------------------------------------------
# 几个邮箱 (2026-11-01 用户: "还要加入gmail等邮箱")、Gmail、代理
# ---------------------------------------------------------------------------
GMAIL, GCODE = "me@gmail.com", "abcdabcdabcdabcd"


def by_port(account, timeout):  # 几个假服务端都在本机, 按账号里的端口连
    return imaplib.IMAP4("127.0.0.1", int(account["port"]), timeout=timeout)


class FakeProxy:
    """假的 HTTP 代理: 认 CONNECT, 记下要连哪, 两头对拷. allow=False 时回 403."""

    def __init__(self, allow=True):
        self.allow, self.requests = allow, []

    def start(self):
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock, head = self.request, b""
                while not head.endswith(b"\r\n\r\n"):
                    byte = sock.recv(1)
                    if not byte:
                        return
                    head += byte
                line = head.split(b"\r\n", 1)[0].decode()
                proxy.requests.append(line)
                if not proxy.allow:
                    sock.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return
                host, port = line.split()[1].rsplit(":", 1)
                upstream = socket.create_connection((host, int(port)))
                sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

                def pump(a, b):
                    try:
                        while data := a.recv(65536):
                            b.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            b.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                back = threading.Thread(target=pump, args=(upstream, sock), daemon=True)
                back.start()
                pump(sock, upstream)
                back.join(2)
                upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def system_proxy(monkeypatch):
    """这台电脑设的代理: 环境变量里只有 NO_PROXY (urllib.getproxies() 这时会整个不看系统代理), 系统代理是 127.0.0.1:1082."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"no": "localhost,127.0.0.1"})
    monkeypatch.setattr(urllib.request, "getproxies_registry",
                        lambda: {"http": "http://127.0.0.1:1082", "https": "https://127.0.0.1:1082"})
    return ("127.0.0.1", 1082)


def test_first_mailbox_stays_in_account_json_and_more_go_to_accounts(tmp_path):
    home = tmp_path / "mail"
    M.save_account(home, ME, CODE, "imap.qq.com")
    M.save_account(home, GMAIL, "app pass word", "")  # 主机按预设: imap.gmail.com
    M.save_account(home, ME.upper(), "new-code", "imap.qq.com")  # 同一个地址重填: 还在 account.json, 不多一个
    assert [p.name for p in M.account_files(home)] == ["account.json", "me@gmail.com.json"]
    assert [a["address"].lower() for a in M.load_accounts(home)] == [ME, GMAIL]
    assert M.secret_of(M.find_account(home, ME)) == "new-code" and M.find_account(home, GMAIL)["host"] == "imap.gmail.com"
    assert M.find_account(home, GMAIL)["proxy"] == "auto" and M.find_account(home, GMAIL)["preset"] == "gmail"
    for i in range(3):
        M.save_account(home, f"u{i}@163.com", "x")
    with pytest.raises(ValueError, match="最多 5 个"):
        M.save_account(home, "u9@163.com", "x")
    with pytest.raises(ValueError, match="说清楚删哪个"):
        M.forget_account(home)
    assert M.forget_account(home, GMAIL) and not M.forget_account(home, GMAIL)
    assert M.set_proxy(home, "u0@163.com", "http://127.0.0.1:7890")["proxy"] == "http://127.0.0.1:7890"
    assert M.secret_of(M.find_account(home, "u0@163.com")) == "x"  # 改代理不动授权码
    (home / "account.json").write_text("{坏了", encoding="utf-8")
    assert M.load_accounts(home)[0]["_broken"]
    M.save_account(home, "fix@qq.com", "y", "imap.qq.com")  # 坏掉的 account.json 直接写回好的
    assert M.find_account(home, "fix@qq.com")["_path"].endswith("account.json")
    assert not M.load_accounts(home)[0].get("_broken")
    with pytest.raises(ValueError, match="本机的邮件网桥"):
        M.save_account(home, "x@qq.com", "y", "imap.qq.com", tls=False)


@pytest.mark.parametrize("address, host", [
    ("a@qq.com", "imap.qq.com"), ("a@foxmail.com", "imap.qq.com"), ("a@163.com", "imap.163.com"),
    ("a@vip.163.com", "imap.vip.163.com"), ("a@gmail.com", "imap.gmail.com"), ("a@googlemail.com", "imap.gmail.com"),
    ("a@icloud.com", "imap.mail.me.com"), ("a@yahoo.com", "imap.mail.yahoo.com"), ("a@sina.com", "imap.sina.com"),
    ("a@aliyun.com", "imap.aliyun.com"), ("a@139.com", "imap.139.com"), ("a@189.cn", "imap.189.cn"),
])
def test_presets(address, host):
    key, preset = M.preset_for(address)
    assert preset["host"] == host and M.GUIDE.get(preset.get("guide") or key)


def test_microsoft_personal_mail_says_oauth_only():
    for address in ("a@outlook.com", "a@hotmail.com", "a@hotmail.co.uk", "a@live.cn", "a@msn.com"):
        with pytest.raises(ValueError, match="OAuth"):
            M.preset_for(address)
    assert M.preset_for("a@corp-outlook.example") == ("", {})


def test_proxy_choice(monkeypatch, system_proxy):
    import urllib.request
    gmail = {"preset": "gmail", "proxy": "auto"}
    assert M.proxy_for(gmail) == system_proxy  # 只设了 NO_PROXY 也照样认出系统代理
    assert M.proxy_for({"preset": "qq", "proxy": "auto"}) is None  # 国内的直连
    assert M.proxy_for({"preset": "qq", "proxy": "http://10.0.0.2:7890"}) == ("10.0.0.2", 7890)
    assert M.proxy_for({"preset": "gmail", "proxy": "off"}) is None
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"https": "http://127.0.0.1:7897"})
    assert M.proxy_for(gmail) == ("127.0.0.1", 7897)  # 环境变量优先
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: {"https": "socks=127.0.0.1:1080"})
    assert M.proxy_for(gmail) is None  # socks 不认: 直连 (连不上会照实说)
    for bad in ("socks5://127.0.0.1:1080", "127.0.0.1:7890", "abc"):
        with pytest.raises(ValueError, match="HTTP 代理"):
            M.check_proxy(bad)


def test_connect_tunnels_tls_through_the_proxy_for_gmail(monkeypatch, system_proxy):
    made = []
    monkeypatch.setattr(M, "_TunnelIMAP4SSL", lambda host, port, proxy, ctx, timeout: made.append((host, port, proxy, ctx)))
    M.connect({"address": GMAIL, "host": "imap.gmail.com", "port": 993, "preset": "gmail", "proxy": "auto"})
    host, port, proxy, ctx = made[0]
    assert (host, port, proxy) == ("imap.gmail.com", 993, system_proxy)
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED  # 走代理也照样校验证书和主机名


def test_login_through_an_http_proxy_and_proxy_refusal(fake):
    proxy, denied = FakeProxy().start(), FakeProxy(allow=False).start()
    try:
        account = {"address": ME, "host": "127.0.0.1", "port": fake.port, "preset": "qq", "tls": False,
                   "proxy": f"http://127.0.0.1:{proxy.port}"}
        info = M.check_login(account, CODE)  # 用真的 connect: 先打隧道, 再在隧道里说 IMAP
        assert info["inbox"] == 0 and proxy.requests == [f"CONNECT 127.0.0.1:{fake.port} HTTP/1.1"]
        with pytest.raises(M.MailError, match="不让连") as err:
            M.check_login({**account, "proxy": f"http://127.0.0.1:{denied.port}"}, CODE)
        assert err.value.kind == "network"
    finally:
        proxy.stop()
        denied.stop()


def test_two_mailboxes_one_digest_and_gmail_categories(tmp_path, fake):
    gm = FakeIMAP(user=GMAIL, password=GCODE, gmail=True).start()
    try:
        home = tmp_path / "mail"
        fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
        gm.add(mk("Bob <bob@example.org>", "Lunch on Friday?", to=GMAIL), at=T0 - 50)
        gm.add(mk("Shop <hello@shop.example>", "Our autumn picks for you", to=GMAIL), at=T0 - 40, category="promotions")
        gm.add(mk("Feed <notify@social.example>", "You have 3 new likes", to=GMAIL), at=T0 - 30, category="social")
        M.save_account(home, ME, CODE, "127.0.0.1", fake.port)
        M.save_account(home, GMAIL, GCODE, "127.0.0.1", gm.port)
        M.save_settings(home, {"enabled": True, "quiet": "", "report_gap_min": 0}, local=True)
        owner = Owner()
        box = R.Mailbox(home, tell=owner.tell, connector=by_port, clock=lambda: T0, timeout=5)

        async def main():
            rec = await box.poll()
            st = box.status()
            await box.close()
            return rec, st
        rec, st = run(main())
    finally:
        gm.stop()
    assert rec["ok"] and rec["read"] == 4 and rec["ignored"]["promo"] == 2
    assert [(a["label"], a["read"]) for a in rec["accounts"]] == [("QQ邮箱", 1), ("Gmail", 3)]
    digest = owner.told[0]
    assert "[QQ邮箱] 张三「周末一起吃饭吗」" in digest and "[Gmail] Bob「Lunch on Friday?」" in digest
    assert "autumn picks" not in digest and "likes" not in digest
    assert {x["why"] for x in rec["ignored_from"]} >= {"Gmail 把它归到了推广", "Gmail 把它归到了社交"}
    assert any("X-GM-RAW" in c for c in gm.commands) and not any("X-GM-RAW" in c for c in fake.commands)
    assert [a["label"] for a in st["accounts"]] == ["QQ邮箱", "Gmail"] and st["configured"]
    assert ME not in json.dumps(st) and GMAIL not in json.dumps(st)


def test_one_mailbox_failing_does_not_stop_the_other(tmp_path, fake):
    gm = FakeIMAP(user=GMAIL, password="the-right-one", gmail=True).start()
    try:
        home = tmp_path / "mail"
        fake.add(mk("张三 <zhangsan@163.com>", "周末一起吃饭吗"), at=T0 - 60)
        M.save_account(home, ME, CODE, "127.0.0.1", fake.port)
        M.save_account(home, GMAIL, GCODE, "127.0.0.1", gm.port)  # 授权码填错了
        M.save_settings(home, {"enabled": True, "quiet": "", "report_gap_min": 0}, local=True)
        owner, clock = Owner(), [T0]
        box = R.Mailbox(home, tell=owner.tell, connector=by_port, clock=lambda: clock[0], timeout=5)

        async def main():
            first = await box.poll()
            clock[0] += 3600
            fake.add(mk("李雷 <lilei@126.com>", "下周的会挪到周三"), at=clock[0])
            later = await box.tick()  # QQ 照读; Gmail 等你重新填授权码, 不再去试
            st = box.status()
            await box.close()
            return first, later, st
        first, later, st = run(main())
    finally:
        gm.stop()
    assert not first["ok"] and first["error"]["kind"] == "auth" and first["read"] == 1
    assert [a["ok"] for a in first["accounts"]] == [True, False]
    fails = [t for t in owner.told if "登不上" in t]
    assert len(fails) == 1 and fails[0].startswith("主人, 我登不上Gmail (me***@gmail.com)了")
    assert gm.logins == 1 and later["read"] == 1 and len(later["accounts"]) == 1
    assert [a["waiting_for_owner"] for a in st["accounts"]] == [False, True]
    assert any("周末一起吃饭吗" in t for t in owner.told) and any("下周的会挪到周三" in t for t in owner.told)


def test_first_version_state_keeps_its_place(tmp_path, fake):
    fake.add(mk("张三 <zhangsan@163.com>", "旧的一封"), at=T0 - 3600)
    box, owner, _, _ = make_box(tmp_path, fake)
    M.write_json(box.home / "state.json", {  # 第一版 (只有一个邮箱) 写的: 已经读到 UID 1
        "account": ME, "folders": {"INBOX": {"uidvalidity": 7, "last_uid": 1}}, "junk": {}, "pending": [],
        "since": {"low": 0, "ignored": 0}, "urgent_at": [], "today": {}, "reports": 3, "last_poll_at": T0 - 7200,
        "last_ok_at": T0 - 7200, "last_digest_at": T0 - 7200})
    fake.add(mk("李雷 <lilei@126.com>", "新的一封"), at=T0 - 60)

    async def main():
        rec = await box.poll()
        await box.close()
        return rec
    rec = run(main())
    assert rec["read"] == 1 and rec["first"] == []  # 接着第一版读到的地方往后读, 没有重新起头
    assert "新的一封" in owner.told[0] and "旧的一封" not in owner.told[0] and "接好啦" not in owner.told[0]
    st = json.loads((box.home / "state.json").read_text(encoding="utf-8"))
    assert st["version"] == 2 and st["accounts"][ME]["folders"]["INBOX"]["last_uid"] == 2 and st["reports"] == 4
    assert "folders" not in st and "account" not in st


def test_setup_tool_adds_gmail_next_to_qq(tmp_path, fake, system_proxy):
    gm = FakeIMAP(user=GMAIL, password=GCODE, gmail=True).start()
    try:
        tool = _setup_tool()
        cfg = {"mail": {"home": str(tmp_path / "mail")}, "agent": {"home": str(tmp_path / "agent")}}
        said, answers = [], iter([ME, "y"])
        qq_io = tool.IO(inp=lambda prompt="": next(answers), secret_in=lambda prompt="": CODE, out=said.append,
                        tty=lambda: True, connector=by_port)
        assert tool.main(["setup", "--host", "127.0.0.1", "--port", str(fake.port)], qq_io, cfg) == 0
        said.clear()
        gm_io = tool.IO(inp=lambda prompt="": pytest.fail("不该再问"), secret_in=lambda prompt="": "abcd abcd abcd abcd",
                        out=said.append, tty=lambda: True, connector=by_port)
        assert tool.main(["setup", "--address", GMAIL, "--host", "127.0.0.1", "--port", str(gm.port)], gm_io, cfg) == 0
        text = "\n".join(said)
        assert "现在存了: QQ邮箱 me***@qq.com" in text and "应用专用密码" in text
        assert "经代理 127.0.0.1:1082 (这台电脑设的代理)" in text and "读信本来就开着" in text
        home = tmp_path / "mail"
        assert [a["address"] for a in M.load_accounts(home)] == [ME, GMAIL]
        said.clear()
        assert tool.main(["status"], tool.IO(out=said.append), cfg) == 0
        text = "\n".join(said)
        assert "QQ邮箱 me***@qq.com" in text and "Gmail me***@gmail.com" in text and CODE not in text
        said.clear()
        assert tool.main(["test"], tool.IO(out=said.append, connector=by_port), cfg) == 0 and len(said) == 2
        assert tool.main(["proxy", GMAIL, "off"], tool.IO(out=said.append), cfg) == 0 and "现在直连" in said[-1]
        assert tool.main(["proxy", GMAIL, "socks5://x:1"], tool.IO(out=said.append), cfg) == 2
        assert tool.main(["forget"], tool.IO(out=said.append), cfg) == 2 and "说清楚删哪个" in said[-1]
        assert tool.main(["forget", GMAIL, "--yes"], tool.IO(out=said.append), cfg) == 0 and "还剩 1 个" in said[-1]
        assert M.load_settings(home)["enabled"] is True and [a["address"] for a in M.load_accounts(home)] == [ME]
    finally:
        gm.stop()


def test_fixing_a_mailbox_retries_it_right_away(tmp_path, fake):
    gm = FakeIMAP(user=GMAIL, password=GCODE, gmail=True).start()
    try:
        home = tmp_path / "mail"
        M.save_account(home, ME, CODE, "127.0.0.1", fake.port)
        M.save_account(home, GMAIL, "wrong-code", "127.0.0.1", gm.port)
        gm.add(mk("Bob <bob@example.org>", "Lunch on Friday?", to=GMAIL), at=T0 - 60)
        M.save_settings(home, {"enabled": True, "quiet": "", "report_gap_min": 0}, local=True)
        owner, clock = Owner(), [T0]
        box = R.Mailbox(home, tell=owner.tell, connector=by_port, clock=lambda: clock[0], timeout=5)

        async def main():
            await box.poll()
            clock[0] += 300
            idle = await box.tick()  # 5 分钟: 谁都没到间隔, Gmail 在等你
            time.sleep(0.02)
            M.save_account(home, GMAIL, GCODE, "127.0.0.1", gm.port)  # 你重新填对了
            fixed = await box.tick()  # 不等 20 分钟的间隔, 马上试 Gmail (QQ 还没到间隔, 不读)
            await box.close()
            return idle, fixed
        idle, fixed = run(main())
    finally:
        gm.stop()
    assert idle is None and gm.logins == 2
    assert fixed["ok"] and [a["label"] for a in fixed["accounts"]] == ["Gmail"] and fixed["read"] == 1
    assert any(t.startswith("主人, Gmail (me***@gmail.com)又能读了") for t in owner.told)
    assert any("[Gmail] Bob「Lunch on Friday?」" in t for t in owner.told)


# ---------------------------------------------------------------------------
# 连接邮箱的页面 /mail 和它用的接口 (2026-11-01 用户: "能不能通过桌宠 (工作台) 进行登录、链接呢? … 不是人人都是懂电脑的")
# ---------------------------------------------------------------------------
def test_mail_page_and_connect_flow(tmp_path, fake, system_proxy):
    gm = FakeIMAP(user=GMAIL, password=GCODE, gmail=True).start()
    home = tmp_path / "mail"
    store = BlacklistStore(tmp_path / "agent", defaults=False)
    owner = Owner()
    ch = SimpleNamespace(_tell_owner=owner.tell, notify=None, listeners=[], cloud=None)

    async def main():
        app = web.Application()
        box = H.register(app, {"mail": {"home": str(home), "run": "on"}},
                         lambda r: "Host 不对" if r.headers.get("X-Bad") else None, SimpleNamespace(channels=ch),
                         SimpleNamespace(blacklist=store))
        box.connector = by_port
        out = {}
        async with TestClient(TestServer(app)) as client:
            async def post(**body):
                resp = await client.post("/api/mail", json=body)
                text = await resp.text()
                out.setdefault("texts", []).append(text)
                return resp.status, json.loads(text)
            page = await client.get("/mail")
            out["page"] = (page.status, await page.text(), dict(page.headers))
            out["bad_page"] = (await client.get("/mail", headers={"X-Bad": "1"})).status
            out["st"] = await (await client.get("/api/mail?providers=1")).json()
            out["outlook"] = await post(action="connect", address="a@outlook.com", secret="x")
            out["wrong"] = await post(action="connect", address=GMAIL, secret="nope", host="127.0.0.1", port=gm.port)
            out["after_wrong"] = [a["address"] for a in M.load_accounts(home)]
            out["gmail"] = await post(action="connect", address=GMAIL, secret="abcd abcd abcd abcd", host="127.0.0.1",
                                      port=gm.port)
            out["qq"] = await post(action="connect", address=ME, secret=CODE, host="127.0.0.1", port=fake.port)
            listed = await (await client.get("/api/mail")).json()
            out["listed"] = listed
            ids = {a["label"]: a["id"] for a in listed["accounts"]}
            out["tested"] = await post(action="test", id=ids["Gmail"])
            out["proxied"] = await post(action="proxy", id=ids["Gmail"], proxy="off")
            out["badproxy"] = await post(action="proxy", id=ids["Gmail"], proxy="socks5://x:1")
            out["gone"] = await post(action="forget", id=ids["Gmail"])
            out["missing"] = await post(action="test", id="0123456789ab")
            out["settings"] = (await client.get("/api/mail")).status
        return out
    try:
        out = run(main())
    finally:
        gm.stop()
    status, html, headers = out["page"]
    assert status == 200 and "<title>连接邮箱</title>" in html and out["bad_page"] == 403
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"
    assert "innerHTML" not in html  # 邮件里来的字一律当文本放
    gmail = next(p for p in out["st"]["providers"] if p["key"] == "gmail")
    assert gmail["url"] == "https://myaccount.google.com/apppasswords" and gmail["overseas"] and "两步验证" in gmail["steps"]
    assert out["st"]["system_proxy"] == "127.0.0.1:1082" and out["st"]["max_accounts"] == 5
    assert out["outlook"][0] == 400 and "OAuth" in out["outlook"][1]["error"]
    code, wrong = out["wrong"]
    assert code == 200 and not wrong["ok"] and wrong["kind"] == "auth" and wrong["error"].startswith("授权码不对")
    assert "AUTHENTICATIONFAILED" in wrong["detail"] and "不是登录密码" in wrong["hint"] and out["after_wrong"] == []
    code, ok = out["gmail"]
    assert ok["ok"] and ok["account"]["label"] == "Gmail" and ok["account"]["proxy_now"] == "127.0.0.1:1082"
    assert ok["protected"] and ok["enabled"] and M.protected(store, home)
    assert out["qq"][1]["ok"] and [a["label"] for a in out["listed"]["accounts"]] == ["Gmail", "QQ邮箱"]
    assert all(re.fullmatch(r"[0-9a-f]{12}", a["id"]) for a in out["listed"]["accounts"])
    assert out["tested"][1] == {"ok": True, "inbox": 0, "junk": []}
    assert out["proxied"][1]["proxy"] == "off" and out["proxied"][1]["proxy_now"] is None
    assert out["badproxy"][0] == 400 and out["gone"][1] == {"ok": True, "left": 1} and out["missing"][0] == 400
    assert [a["address"] for a in M.load_accounts(home)] == [ME] and M.load_settings(home)["enabled"] is True
    for text in out["texts"] + [json.dumps(out["listed"])]:  # 授权码、完整地址都不从接口出去
        assert GCODE not in text and "abcd abcd" not in text and CODE not in text and GMAIL not in text
    for path in home.rglob("*.json"):
        assert GCODE not in path.read_text(encoding="utf-8") and CODE not in path.read_text(encoding="utf-8")


def test_fix_hints_point_to_the_page_not_only_the_terminal(tmp_path, monkeypatch):
    """装好的电脑上不一定有 python 命令, 安装包也不带 tools/mail_setup.py: 让你去哪修都先说连接邮箱的页面."""
    monkeypatch.setattr(M, "PAGE_URL", M.PAGE_URL)  # register 会按端口改它, 测完还原
    app = web.Application()
    H.register(app, {"mail": {"home": str(tmp_path / "mail"), "run": "off"}, "server": {"port": 8899}}, lambda r: None)
    assert M.PAGE_URL == "http://127.0.0.1:8899/mail"
    texts = [R.failure_text({"kind": "auth", "text": "x", "since": 0}, 0),
             R.failure_text({"kind": "config", "text": "x", "since": 0}, 0),
             R.failure_text({"kind": "secret", "text": "x", "since": 0}, 0, "Gmail (me***@gmail.com)")]
    with pytest.raises(M.MailError) as err:
        M.secret_of({"secret": "不是 base64"})
    texts.append(err.value.text)
    home = tmp_path / "mail"
    home.mkdir(parents=True)
    (home / "account.json").write_text("{坏了", encoding="utf-8")
    texts.append(M.load_accounts(home)[0]["_broken"])
    for i in range(5):
        M.save_account(tmp_path / "full", f"u{i}@163.com", "x")
    with pytest.raises(ValueError) as full:
        M.save_account(tmp_path / "full", "u9@163.com", "x")
    texts.append(str(full.value))
    box = R.Mailbox(tmp_path / "empty")
    with pytest.raises(M.MailError) as none:
        run(box.poll())
    run(box.close())
    texts.append(none.value.text)
    for text in texts:
        assert "http://127.0.0.1:8899/mail" in text, text
        assert text.find("127.0.0.1:8899/mail") < text.find("mail_setup.py") or "mail_setup.py" not in text, text
