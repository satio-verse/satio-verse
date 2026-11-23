"""主人自己设置绫音读邮箱 (见 wm/channels/mailbox.py). 授权码只在这里填: 输入不回显、不经过聊天, Windows DPAPI 加密后
存在 ~/.worldmodel/mail/ (只有你这个 Windows 用户解得开), 不进记忆库、日志、知识库. 最多 5 个邮箱 (QQ、Gmail、网易…).

  python tools/mail_setup.py setup [--address 地址] [--proxy auto|off|http://127.0.0.1:端口]
                                          加一个邮箱 (同一个地址就是重填): 填地址和授权码, 只读地试登录一次, 存好,
                                          加进工作台的保护名单, 问开不开. Gmail 这类国外的邮箱默认走这台电脑设的代理
  python tools/mail_setup.py status       看每个邮箱 (授权码不显示)、设置、保护着没有、最近几次读信记录
  python tools/mail_setup.py test [地址]   用存好的授权码只读地登录一次, 数数收件箱 (不改邮箱里任何东西; 不给地址就每个都试)
  python tools/mail_setup.py proxy 地址 auto|off|http://127.0.0.1:端口   改这个邮箱走不走代理 (授权码不用重填)
  python tools/mail_setup.py set k=v …    改设置: enabled=true interval_min=30 via=pet summary=cloud
                                          important_senders=boss@corp.com,@bank.com quiet=23:00-08:00 …
                                          (云端摘要 summary=cloud 只能在这里开, 会先说清楚发什么)
  python tools/mail_setup.py forget [地址]   删掉一个邮箱存好的授权码 (只存了一个时可以不写地址; 读信记录留着)

要在 PowerShell / Windows Terminal / cmd 里跑 (Git Bash 的窗口不能不回显地输入). 服务端每分钟看一次设置, 改了不用重启.
不想用命令行: 服务端开着时打开 http://127.0.0.1:8790/mail (连接邮箱的页面, wm/channels/mailbox_page.html), 做的是同样的事.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wm.channels import mailbox as M  # noqa: E402
from wm.config import load_config  # noqa: E402

CLOUD_NOTE = ("打开以后, 每次有要说的邮件, 会把那几封的发件人名字、主题和正文开头 (每封最多 600 字; 邮箱、手机号、证件号、"
              "长号码、验证码、链接先打码) 发给 QQ 渠道正在用的云端模型 (比如 DeepSeek) 写一句话摘要, 按 token 计费, "
              "记在账本的「其他」渠道. 不开就只按发件人、主题和关键词整理, 内容不出这台电脑 (发到 QQ / 微信的汇报除外).")
_LIST_KEYS = ("folders", "important_senders", "ignore_senders", "important_keywords", "ignore_keywords")


class IO:
    """输入输出 (测试换成假的)."""

    def __init__(self, inp=input, secret_in=getpass.getpass, out=print, tty=None, connector=None):
        self.inp, self.secret_in, self.out, self.connector = inp, secret_in, out, connector
        self.tty = tty if tty is not None else (lambda: sys.stdin.isatty())


def mail_home(cfg: dict) -> Path:
    conf = cfg.get("mail") or {}
    return Path(str(conf["home"])).expanduser() if conf.get("home") else M.HOME


def blacklist(cfg: dict):
    """工作台的黑名单 (和服务端同一份: agent.home, 默认 ~/.worldmodel/agent)."""
    from wm.agent import core
    from wm.agent.governance import BlacklistStore
    home = Path(str((cfg.get("agent") or {}).get("home") or core.HOME)).expanduser()
    return BlacklistStore(home, vault=(cfg.get("memory") or {}).get("vault") or None)


def _yes(answer: str, default: bool) -> bool:
    a = str(answer or "").strip().lower()
    return default if not a else a in ("y", "yes", "是", "好", "要", "开")


def _value(key: str, raw: str):
    """set 的值: 先按 JSON 认 (true、30、["a"]), 认不出当字; 名单类的也认逗号隔开的写法."""
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if key in _LIST_KEYS and isinstance(value, str):
        value = [v for v in (x.strip() for x in re.split(r"[,，、]", value)) if v]
    return value


def _when(t) -> str:
    return datetime.fromtimestamp(float(t)).strftime("%m-%d %H:%M") if t else "还没有"


def _proxy_line(account: dict) -> str:
    """这个邮箱现在怎么连: 直连 / 经哪个代理; overseas 又没找到代理时提醒一句."""
    hp = M.proxy_for(account)
    if hp:
        return f"经代理 {hp[0]}:{hp[1]}" + (" (这台电脑设的代理)" if str(account.get("proxy") or "auto") == "auto" else "")
    overseas = M.PRESETS.get(account.get("preset") or "", {}).get("overseas")
    if overseas and str(account.get("proxy") or "auto") == "auto":
        return "直连 (没找到这台电脑设的代理; 国内直连多半连不上, 可以加 --proxy http://127.0.0.1:端口)"
    return "直连"


def cmd_setup(args, cfg: dict, io: IO) -> int:
    home = mail_home(cfg)
    accounts = [a for a in M.load_accounts(home) if not a.get("_broken")]
    if accounts:
        io.out("现在存了: " + "、".join(f"{M.label_of(a, accounts)} {M.mask_address(a['address'])}" for a in accounts)
               + ". 填一个新地址就是再加一个邮箱, 填已有的地址就是重填它.")
    address = (args.address or io.inp("邮箱地址: ")).strip()
    try:
        address = M.check_address(address)
        key, preset = M.preset_for(address)
        proxy = M.check_proxy(args.proxy or "auto")
    except ValueError as exc:
        io.out(str(exc))
        return 2
    if len(accounts) >= M.MAX_ACCOUNTS and not any(a["address"].lower() == address.lower() for a in accounts):
        io.out(f"最多 {M.MAX_ACCOUNTS} 个邮箱: 先删掉一个 (python tools/mail_setup.py forget 地址).")
        return 2
    domain = address.rsplit("@", 1)[-1].lower()
    host = (args.host or preset.get("host") or io.inp(f"IMAP 服务器 (直接回车用 imap.{domain}): ").strip()
            or f"imap.{domain}")
    port = int(args.port or 993)
    guide = M.GUIDE.get(preset.get("guide") or key) or "在邮箱网页版的设置里开启 IMAP, 生成授权码 (或应用专用密码)."
    io.out(f"{preset.get('name', '这个邮箱')}: {guide}")
    account = {"address": address, "host": host, "port": port, "preset": key, "id_command": bool(preset.get("id")),
               "proxy": proxy}
    io.out(f"连法: {_proxy_line(account)}.")
    io.out("授权码只存在这台电脑上 (Windows 加密, 只有你这个 Windows 用户解得开), 不进记忆库、日志和聊天. 她只读不改: "
           "不标已读、不删、不移动、不发信.")
    if not io.tty():
        io.out("这个窗口没法不回显地输入授权码: 请在 PowerShell、Windows Terminal 或 cmd 里跑 (Git Bash 的窗口不行).")
        return 2
    secret = re.sub(r"\s+", "", io.secret_in("授权码 (输入时不显示, 填完回车): ") or "")
    if not secret:
        io.out("没填授权码, 什么都没改.")
        return 2
    io.out(f"只读地试登录 {host}:{port} …")
    try:
        info = M.check_login(account, secret, io.connector)
    except (M.MailError, M.ReadOnlyViolation) as exc:
        io.out(f"没登上: {getattr(exc, 'text', None) or exc}")
        if getattr(exc, "kind", "") == "auth":
            io.out("授权码对吗? 要填授权码 / 应用专用密码, 不是邮箱 / QQ 的登录密码; 刚生成的有时要等一两分钟才能用.")
        elif getattr(exc, "kind", "") == "network" and preset.get("overseas"):
            io.out("国外的邮箱在国内常要走代理: 看看代理开着没有, 或者用 --proxy http://127.0.0.1:端口 指定.")
        io.out("什么都没存.")
        return 1
    junk = f"; 垃圾箱 / 广告文件夹 (不读, 只数个数): {'、'.join(info['junk'])}" if info["junk"] else ""
    io.out(f"登上了: 收件箱里有 {info['inbox']} 封{junk}")
    M.save_account(home, address, secret, host, port, proxy)
    secret = ""
    try:
        ok, why = M.protect(blacklist(cfg), home)
    except (OSError, ValueError) as exc:
        ok, why = False, f"打不开工作台的保护名单: {exc}"
    io.out(f"工作台保护: {why}")
    if not ok:
        io.out("注意: 工作台的保护名单现在没挡住邮箱目录, 到工作台的约束设置里看看.")
    s = M.load_settings(home, cfg.get("mail"))
    if s["enabled"]:
        io.out(f"好了, 读信本来就开着: 她会在一分钟内开始看这个邮箱 (每 {s['interval_min']} 分钟一次; 第一次只看最近 "
               f"{s['first_run_hours']} 小时的). 现在一共 {len(M.load_accounts(home))} 个邮箱.")
        return 0
    enable = True if args.yes else _yes(io.inp(f"现在就让她每 {s['interval_min']} 分钟读一次新邮件吗? [Y/n] "), True)
    M.save_settings(home, {"enabled": enable}, local=True)
    io.out(("好了, 她会" if enable else "存好了, 先不读; 要读时跑 python tools/mail_setup.py set enabled=true. 开了以后她会")
           + f"每 {s['interval_min']} 分钟看一眼收件箱: 要紧的单独说, 别的攒着最多每 {s['report_gap_min']} 分钟说一次, "
             f"一次最多 {s['max_items']} 件; 广告、订阅、垃圾邮件不说. 免打扰 {s['quiet'] or '不设'}. 第一次只看最近 "
             f"{s['first_run_hours']} 小时的.")
    io.out("服务端每分钟看一次设置, 不用重启 (读邮箱这个功能是新加的, 服务端要重启过一次才有).")
    io.out(f"以后加邮箱、改怎么汇报也可以不用命令行: 打开 http://127.0.0.1:{(cfg.get('server') or {}).get('port', 8790)}/mail")
    return 0


def cmd_status(args, cfg: dict, io: IO) -> int:
    home = mail_home(cfg)
    accounts = M.load_accounts(home)
    st = M.read_json(home / "state.json", {}) or {}
    per = st.get("accounts") if st.get("version") == 2 else {st.get("account"): st} if st.get("account") else {}
    if not accounts:
        io.out("还没设置邮箱: python tools/mail_setup.py setup")
    for a in accounts:
        if a.get("_broken"):
            io.out(f"邮箱: {a['_broken']}")
            continue
        entry = (per or {}).get(a["address"]) or {}
        fail = entry.get("fail")
        io.out(f"邮箱: {M.label_of(a, accounts)} {M.mask_address(a['address'])} ({a['host']}:{a.get('port', 993)}, "
               f"{_proxy_line(a)}), {_when(a.get('saved_at'))} 设置的; 上次读 {_when(entry.get('last_poll_at'))}, 上次读成功 "
               f"{_when(entry.get('last_ok_at'))}" + (f"; 出错: {fail.get('kind')} {fail.get('text')}" if fail else ""))
    s = M.load_settings(home, cfg.get("mail"))
    io.out("设置: " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in s.items()))
    try:
        io.out("工作台保护: " + ("挡着" if M.protected(blacklist(cfg), home) else "没挡 (跑一次 setup 会加上)"))
    except (OSError, ValueError) as exc:
        io.out(f"工作台保护: 看不了 ({exc})")
    io.out(f"攒着没说的: {len(st.get('pending') or [])} 封")
    lines = _tail(home / "log.jsonl", args.log)
    if lines:
        io.out(f"最近 {len(lines)} 次:")
    for rec in lines:
        when = _when(rec.get("at"))
        if rec.get("kind") == "poll":
            for sub in rec.get("accounts") or [rec]:
                who = (sub.get("label") + " ") if sub.get("label") else ""
                if sub.get("ok"):
                    ig = sub.get("ignored") or {}
                    io.out(f"  {when} {who}读了 {sub.get('read', 0)} 封: 忽略 {ig.get('junk', 0) + ig.get('promo', 0)} "
                           f"(垃圾箱另有 {ig.get('junk_folder', 0)}), 普通 {sub.get('low', 0)}, 要看 {sub.get('important', 0)}, "
                           f"要紧 {sub.get('urgent', 0)}")
                else:
                    io.out(f"  {when} {who}没读成: {(sub.get('error') or {}).get('text', '')}")
        for r in rec.get("reported") or []:
            io.out(f"    说了 ({r.get('via')}): " + str(r.get("text", "")).replace("\n", " / ")[:160])
    return 0


def _tail(path: Path, n: int) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(0, n):] if n else []
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def cmd_test(args, cfg: dict, io: IO) -> int:
    home = mail_home(cfg)
    accounts = M.load_accounts(home)
    if args.address:
        accounts = [a for a in accounts if a["address"].lower() == args.address.strip().lower()]
    if not accounts:
        io.out(f"没有这个邮箱: {args.address}" if args.address else "还没设置邮箱: python tools/mail_setup.py setup")
        return 2
    bad = 0
    for a in accounts:
        name = f"{M.label_of(a, accounts)} {M.mask_address(a['address'])}" if "@" in a["address"] else a["address"]
        try:
            if a.get("_broken"):
                raise M.MailError("config", a["_broken"])
            started = time.monotonic()
            info = M.check_login(a, M.secret_of(a), io.connector)
        except (M.MailError, M.ReadOnlyViolation) as exc:
            io.out(f"{name}: 没登上: {getattr(exc, 'text', None) or exc}")
            bad += 1
            continue
        io.out(f"{name}: 登上了 ({time.monotonic() - started:.1f} 秒, {_proxy_line(a)}): 收件箱 {info['inbox']} 封; "
               f"文件夹: {'、'.join(info['folders'])}")
    return 1 if bad else 0


def cmd_proxy(args, cfg: dict, io: IO) -> int:
    try:
        account = M.set_proxy(mail_home(cfg), args.address, args.proxy)
    except ValueError as exc:
        io.out(str(exc))
        return 2
    io.out(f"改好了: {M.mask_address(account['address'])} 现在{_proxy_line(account)} (服务端下一次读信就用新的).")
    return 0


def cmd_set(args, cfg: dict, io: IO) -> int:
    home = mail_home(cfg)
    changes = {}
    for pair in args.pairs:
        if "=" not in pair:
            io.out(f"写成 键=值: {pair}")
            return 2
        key, raw = pair.split("=", 1)
        changes[key.strip()] = _value(key.strip(), raw.strip())
    if changes.get("summary") == "cloud" and not args.yes:
        io.out(CLOUD_NOTE)
        if not _yes(io.inp("确定打开云端摘要吗? [y/N] "), False):
            io.out("没改.")
            return 1
    try:
        changed = M.save_settings(home, changes, local=True)
    except ValueError as exc:
        io.out(str(exc))
        return 2
    io.out("改好了: " + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in changed.items()))
    return 0


def cmd_forget(args, cfg: dict, io: IO) -> int:
    home = mail_home(cfg)
    accounts = M.load_accounts(home)
    if not accounts:
        io.out("本来就没存授权码.")
        return 0
    if args.address is None and len(accounts) > 1:
        io.out("存了好几个邮箱, 说清楚删哪个: python tools/mail_setup.py forget 地址 ("
               + "、".join(a["address"] for a in accounts) + ")")
        return 2
    target = args.address or accounts[0]["address"]
    if not args.yes and not _yes(io.inp(f"删掉 {target} 存好的授权码, 她就不读这个邮箱了. 确定吗? [y/N] "), False):
        io.out("没删.")
        return 1
    if not M.forget_account(home, target):
        io.out(f"没有这个邮箱: {target}")
        return 2
    left = M.load_accounts(home)
    if not left:
        M.save_settings(home, {"enabled": False}, local=True)
    io.out("删掉了 (读信记录留着; 授权码本身还在邮箱那边有效, 不用了可以在邮箱设置里停用它)."
           + (f" 还剩 {len(left)} 个邮箱." if left else " 一个邮箱都没了, 读信关了."))
    return 0


def main(argv=None, io: IO | None = None, cfg: dict | None = None) -> int:
    if io is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # 终端的编码写不出的字换成 ?, 不崩
    p = argparse.ArgumentParser(description="设置绫音读邮箱 (只读; 授权码只在这里填)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="加一个邮箱 / 重填授权码")
    s.add_argument("--address", default="")
    s.add_argument("--host", default="")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--proxy", default="auto", help="auto (国外的邮箱用这台电脑设的代理) / off / http://127.0.0.1:端口")
    s.add_argument("--yes", action="store_true", help="不问, 存好就开始读")
    st = sub.add_parser("status", help="看每个邮箱、设置和最近的读信记录")
    st.add_argument("--log", type=int, default=5)
    t = sub.add_parser("test", help="只读地试登录一次")
    t.add_argument("address", nargs="?", default="")
    pr = sub.add_parser("proxy", help="改一个邮箱走不走代理")
    pr.add_argument("address")
    pr.add_argument("proxy")
    se = sub.add_parser("set", help="改设置 (键=值)")
    se.add_argument("pairs", nargs="+")
    se.add_argument("--yes", action="store_true")
    f = sub.add_parser("forget", help="删掉一个邮箱存好的授权码")
    f.add_argument("address", nargs="?", default=None)
    f.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config() if cfg is None else cfg
    io = io or IO()
    run = {"setup": cmd_setup, "status": cmd_status, "test": cmd_test, "proxy": cmd_proxy, "set": cmd_set,
           "forget": cmd_forget}
    return run[args.cmd](args, cfg, io)


if __name__ == "__main__":
    sys.exit(main())
