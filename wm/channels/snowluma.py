"""Start an already installed SnowLuma, and (2026-11-07) hook the configured QQ account for the owner.

Startup half (unchanged rules): this helper never accepts agreements, changes SnowLuma's settings, sends messages, installs software,
or stops an existing process. Its status is separate from QQ's status.

Attach half (2026-11-07, "不用用户再手动重启 SnowLuma"): SnowLuma's hookAutoLoad stays off (it would hook every QQ on this PC), so after
SnowLuma starts or QQ logs in again, OneBot's port stays closed until somebody clicks "挂接" in SnowLuma's web page. When the vault
(vault.py) holds the page password and the owner allowed the purpose, snowluma_hook.attach_account logs in to that page, clicks "挂接"
only on the process whose account is `self_id`, and logs out. Everything else stays with the owner: no password in the vault, a rejected
password (not retried: SnowLuma locks the whole PC out after 5 wrong tries), two-factor, agreements, a QQ that is not running or not
logged in -> a status the panel and the pet can show, and a notice. A QQ sitting at its login window gets its QR code read (read-only,
see qq_window.py) so it can be shown on the vault page. SnowLuma itself that dies is restarted with backoff; 5 starts in an hour stop.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

from . import qq_window as QW
from . import snowluma_hook as H
from .onebot import check_endpoint
from . import vault as _vault

CRASH_WINDOW_S, CRASH_MAX = 3600, 5   # an hour: at most this many starts, then stop restarting
ATTACH_GRACE_S = 45                    # after attaching, wait this long for OneBot's port
NAG_AFTER_S = 600                      # her QQ still not logged in after this long: tell the owner
NAG_EVERY_S = 3 * 3600
QR_EVERY_S = 60


class _CrashLoop(RuntimeError):
    pass


@contextlib.contextmanager
def _launch_lock(directory: Path):
    """Cross-process launch lock; released by the OS if this service crashes."""
    with (directory / "config" / ".worldmodel-launch.lock").open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if not f.tell():
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def _existing_process(directory: Path) -> int | None:
    # psutil is a packaged dependency. Refuse to launch if enumeration fails;
    # otherwise a second service could create a duplicate QQ bridge.
    # (2026-11-07: 不再用 process_iter 扫全部进程取 exe / cmdline / cwd —— 在这台电脑上要 3–8 秒、期间握着 GIL 拖慢主线程,
    # 而这里每次 OneBot 重连前都要走一遍; 改成先一次系统调用拿名字, 只细查 node 进程.)
    entry = H._norm(directory / "index.mjs")
    for pid, script in H.node_scripts():
        if script is None:
            raise RuntimeError("cannot identify an existing Node process")
        if H._norm(script) == entry:
            return pid
    return None


def _environment() -> dict[str, str]:
    # Do not hand model API keys, Node preload hooks or acceptance flags to a
    # third-party process. Windows account/profile/runtime paths are sufficient.
    allowed = {"path", "systemroot", "windir", "systemdrive", "comspec", "pathext",
               "userprofile", "home", "homedrive", "homepath", "appdata", "localappdata",
               "temp", "tmp", "programdata", "programfiles", "programfiles(x86)",
               "commonprogramfiles", "commonprogramfiles(x86)", "allusersprofile",
               "username", "computername", "lang", "lc_all"}
    return {k: v for k, v in os.environ.items() if k.lower() in allowed}


def _spawn(directory: Path) -> subprocess.Popen:
    executable = directory / ("node.exe" if os.name == "nt" else "node")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    env = _environment()
    checker = directory / "check-node-version.cjs"
    if checker.is_file():
        result = subprocess.run([str(executable), str(checker)], cwd=directory, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags, timeout=10)
        if result.returncode:
            raise RuntimeError("bundled Node version is unsupported")
    # SnowLuma maintains its own logs. Never capture tokens, QR codes or raw
    # third-party stdout into workbench/channel logs.
    return subprocess.Popen([str(executable), str(directory / "index.mjs")], cwd=directory,
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags,
                            start_new_session=os.name != "nt")


async def _listening(endpoint: str) -> bool:
    u = urlsplit(check_endpoint(endpoint))
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or (443 if u.scheme == "wss" else 80)), 0.8)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


class SnowLumaLauncher:
    """Idempotent pre-connect hook. All slow work runs outside the event loop.
    vault / bridge (vault.Vault, vault_bridge.Bridge) switch the attach half on; without them it is the old start-only helper."""
    def __init__(self, vault=None, bridge=None, clock=time.monotonic):
        self._lock = asyncio.Lock()
        self._key = None
        self._process = None
        self._started = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._status = {"state": "off", "detail": "QQ 未启用"}
        self.vault, self.bridge, self.clock = vault, bridge, clock
        self._spawns: deque[float] = deque(maxlen=CRASH_MAX)
        self._attach: dict | None = None      # last attach result for the vault page: {state, detail, at}
        self._attach_at = 0.0                 # earliest next attach attempt
        self._attach_fail = 0
        # 被管理页拒过 (401) 的那一版密码 (保管箱账号的 updated 值). 磁盘上的"被拒过"标记写不进去、或 state.json 读不出来
        # (_flags_all 读失败当空) 时, 光靠它会每轮拿同一个错密码再登录; 内存里再记一份, 用户在 /vault 存了新密码 (updated 变) 才放行
        self._refused_for = None
        self._attach_miss = 0                 # attach steps that said "attached" but the OneBot port never opened
        self._waiting_since = 0.0             # since when her QQ has not been logged in
        self._qr: QW.Shot | None = None
        self._qr_at = 0.0
        self._waiting_pids: list[int] = []

    def status(self) -> dict:
        out = dict(self._status)
        if self._attach:
            out["attach"] = dict(self._attach)
        if self._qr is not None and time.time() - self._qr.at <= QW.TTL_S:
            out["qr"] = {"at": self._qr.at, "ttl_s": QW.TTL_S}
        return out

    def qr_png(self) -> bytes | None:
        """The QR code read from QQ's login window, while it is still fresh (memory only, never written anywhere)."""
        if self._qr is not None and time.time() - self._qr.at <= QW.TTL_S:
            return self._qr.png
        return None

    def inactive(self):
        self._status = {"state": "off", "detail": "QQ 未启用；已有 SnowLuma 进程保持运行"}
        self._attach, self._qr = None, None

    def _set(self, state: str, detail: str, pid: int | None = None):
        self._status = {"state": state, "detail": detail, **({"pid": pid} if pid else {})}

    def _set_attach(self, state: str, detail: str, pid: int | None, wait: float) -> None:
        self._set(state, detail, pid)
        if self._attach is None or self._attach["state"] != state or self._attach["detail"] != detail:
            self._attach = {"state": state, "detail": detail, "at": time.time()}  # 没变就不动: 状态一变就会推给桌宠, 别每轮都推
        self._attach_at = self.clock() + wait

    def _ensure_process(self, directory: Path, allow_spawn: bool = True):
        with _launch_lock(directory):
            if self._process is not None and self._process.poll() is None:
                return None, self._process.pid
            pid = _existing_process(directory)
            if pid:
                return None, pid
            # An exit since the last attempt is a real failure, not an invitation
            # to restart on every OneBot retry.
            if self._process is not None and self._process.poll() is not None:
                self._process = None
                raise RuntimeError("SnowLuma exited before OneBot became available")
            if not allow_spawn:
                raise _CrashLoop()
            process = _spawn(directory)
            return process, process.pid

    async def attach_now(self, conf: dict) -> dict:
        """The vault page's "check now" button: skip the waiting, run one pass. A password SnowLuma refused stays refused until
        the owner enters a new one."""
        self._attach_at = 0.0
        self._qr_at = 0.0
        return await self.ensure(conf)

    async def refresh_qr(self) -> bool:
        """The vault page asked for a fresh look at QQ's login window."""
        if not self._waiting_pids:
            return False
        return await self._capture(self._waiting_pids)

    async def ensure(self, conf: dict) -> dict:
        async with self._lock:
            if self.bridge is not None:
                self.bridge.bind_loop()
            if not conf.get("enabled"):
                self.inactive()
                return self.status()
            if conf.get("snowluma_autostart") is False:
                self._set("manual", "自动启动已关闭；继续连接手动运行的协议端")
                return self.status()
            endpoint = conf.get("endpoint", "ws://127.0.0.1:3001/")
            directory_text = str(conf.get("snowluma_dir") or "").strip()
            key = (directory_text, endpoint)
            if key != self._key:
                self._key, self._process = key, None
                self._retry_at = self._started = 0.0
                self._failures = 0
            if await _listening(endpoint):
                self._failures = self._attach_fail = self._attach_miss = 0
                self._waiting_since = 0.0
                self._attach_at = 0.0
                self._waiting_pids = []
                if self._attach is not None or self._qr is not None:
                    self._attach, self._qr = None, None
                    if self.bridge is not None:
                        self.bridge.reset("qq_")
                self._set("ready", "协议端口已就绪，QQ 登录状态以账号核对结果为准")
                return self.status()
            if not directory_text:
                self._set("not_configured", "填写已安装的 SnowLuma 目录后可随桌宠自动启动")
                return self.status()
            directory = Path(directory_text).expanduser().resolve()
            executable = directory / ("node.exe" if os.name == "nt" else "node")
            if not executable.is_file() or not (directory / "index.mjs").is_file():
                self._set("missing", "目录中缺少 SnowLuma 的 node 和 index.mjs，请选择完整安装目录")
                return self.status()
            now = self.clock()
            if now < self._retry_at:
                return self.status()
            # Consent is owned by SnowLuma. We only recognize a prior explicit
            # acceptance; SnowLuma still revalidates its agreement version.
            try:
                consent = json.loads((directory / "config" / "consent.json").read_text(encoding="utf-8"))
                accepted = isinstance(consent, dict) and bool(consent.get("version")) and bool(consent.get("acceptedAt"))
            except (OSError, ValueError):
                accepted = False
            if not accepted:
                self._set("setup_required", "请先手动启动一次 SnowLuma，完成首次设置和条款选择")
                return self.status()
            recent = [t for t in self._spawns if now - t < CRASH_WINDOW_S]
            try:
                process, pid = await asyncio.to_thread(self._ensure_process, directory, len(recent) < CRASH_MAX)
                if process is not None:
                    self._process, self._started = process, now
                    self._spawns.append(now)
                    self._set("starting", "已在后台启动 SnowLuma，等待 QQ 协议连接", pid)
                    self._attach_at = 0.0
                if process is not None:
                    pass  # just started: the next pass looks for her QQ
                elif self._attach_enabled(conf):
                    await self._attach_step(conf, directory, pid, now)
                elif self._started and now - self._started < 60:
                    self._set("starting", "SnowLuma 正在启动，等待 QQ 协议连接", pid)
                else:
                    self._set("waiting_account", "SnowLuma 已运行；请确认指定 QQ 已登录并在 SnowLuma 中挂接", pid)
            except _CrashLoop:
                self._retry_at = recent[0] + CRASH_WINDOW_S
                self._set("crash_loop", f"SnowLuma 一小时里已经起了 {CRASH_MAX} 次还是没连上，先不再自动重启了；"
                                        "请看 SnowLuma 目录里的 logs，处理好后重启桌宠服务再试")
                await self._tell("qq_crash_loop", "SnowLuma 一小时里反复起不来 (起了 5 次还是没连上)，我先不再自动重启它了。"
                                 "请看 SnowLuma 目录里的 logs 目录，处理好后重启桌宠服务。", cooldown_s=CRASH_WINDOW_S)
            except BlockingIOError:
                self._set("starting", "另一实例正在启动 SnowLuma，稍后重试连接")
            except Exception as exc:
                self._failures += 1
                delay = min(300, 30 * 2 ** min(self._failures - 1, 4))
                self._retry_at = now + delay
                self._set("error", f"SnowLuma 启动未成功（{type(exc).__name__}），{delay} 秒后重试；可查看 SnowLuma 的 logs 目录")
            return self.status()

    # -- 替她挂接 -------------------------------------------------------------
    def _attach_enabled(self, conf: dict) -> bool:
        return self.vault is not None and conf.get("snowluma_attach") is not False and bool(conf.get("self_id"))

    async def _tell(self, kind: str, text: str, cooldown_s: float = 6 * 3600, tell: bool = True, key: str | None = None,
                    image: bytes | None = None) -> None:
        if self.bridge is not None:
            await self.bridge.notice(kind, text, key=key or kind, cooldown_s=cooldown_s, tell=tell, image=image)

    async def _attach_step(self, conf: dict, directory: Path, pid: int | None, now: float) -> None:
        """SnowLuma is running but OneBot's port is closed: hook her QQ (once per wait period). Sets the launcher status."""
        uin = int(conf["self_id"])
        if now < self._attach_at:
            return  # keep the last status
        qq = await asyncio.to_thread(H.qq_main_pids)
        if not qq:
            self._waiting_since = self._waiting_since or now
            self._set_attach("waiting_qq", f"SnowLuma 在跑；电脑上的 QQ 没开，用她的号（{uin}）登录后我会自己挂接", pid, 20)
            await self._nag(now, f"她的 QQ（{uin}）没开 / 没登录，所以 QQ 暂时连不上。请在电脑上把 QQ 打开并用她的号登录，我会自己挂接。")
            return
        port = await H.find_webui(directory)
        if port is None:
            self._set_attach("starting", "SnowLuma 正在启动，管理页还没起来", pid, 4)
            return
        lease = self.vault.lease("snowluma", "qq_attach", by="launcher")
        if not lease.ok:
            await self._blocked(lease, pid)
            return
        acct = self.vault.get("snowluma") or {}
        if acct.get("rejected") or (self._refused_for is not None and acct.get("updated") == self._refused_for):
            lease.done(False, "这个密码之前被管理页拒过, 没再试")
            self._set_attach("login_rejected", f"SnowLuma 管理页拒绝过保管箱里的密码，我没再试（试多了会把本机锁 15 分钟）；"
                                               f"到 {_vault.PAGE_URL} 重新填一次", pid, 300)
            return
        try:
            res = await H.attach_account(port, lease.get("password"), uin)
        except H.HookError as exc:
            lease.done(False, exc.text)
            await self._failed(exc, acct, pid, now)
            return
        except asyncio.CancelledError:
            lease.done(False, "取消了")
            raise
        except Exception as exc:  # a bug in us must not stop OneBot's reconnect loop
            lease.done(False, f"出错了: {type(exc).__name__}")
            self._attach_fail += 1
            self._set_attach("attach_error", f"挂接时出错了（{type(exc).__name__}），稍后重试", pid, 60)
            return
        # 登上去了、她的号还没登录: 每隔十几秒来一次, 使用记录里别刷屏
        lease.done(True, res.detail, quiet_s=0 if res.state in ("attached", "refreshed") else 900)
        await self._attached(res, uin, pid, now)

    async def _blocked(self, lease, pid: int | None) -> None:
        if lease.why == "none":
            text = f"SnowLuma 在跑，但没人把她的 QQ 挂上。到 {_vault.PAGE_URL} 填一次 SnowLuma 管理页的密码，以后就不用手动挂了。"
            self._set_attach("need_password", text, pid, 60)
            await self._tell("qq_need_password", f"QQ 连不上：{text}")
        elif lease.why == "ask":
            self._set_attach("need_consent", f"等你点头：保管箱里的 SnowLuma 管理页密码要用来挂接她的 QQ"
                                             f"（桌宠上有申请卡片，也可以到 {_vault.PAGE_URL} 里允许）", pid, 30)
        elif lease.why == "deny":
            self._set_attach("attach_denied", f"你在保管箱里没允许程序挂接她的 QQ，我就不挂了；要恢复到 {_vault.PAGE_URL} 改", pid, 300)
        else:
            self._set_attach("vault_broken", lease.text or "保管箱里的 SnowLuma 密码取不出来", pid, 300)
            await self._tell("qq_vault_broken", f"QQ 连不上：{lease.text or '保管箱里的 SnowLuma 密码取不出来'}")

    async def _failed(self, exc: "H.HookError", acct: dict, pid: int | None, now: float) -> None:
        kind = exc.kind
        if kind == "rejected":
            self._refused_for = acct.get("updated")  # 先记内存里的, 再写磁盘标记 (写失败不影响不再试)
            with contextlib.suppress(Exception):
                self.vault.set_flags("snowluma", rejected_for=acct.get("updated"), rejected_at=time.time())
            text = (f"SnowLuma 管理页说保管箱里存的密码不对。我没再试（试多了管理页会把这台电脑锁 15 分钟）；"
                    f"到 {_vault.PAGE_URL} 重新填一次。")
            self._set_attach("login_rejected", text, pid, 300)
            await self._tell("qq_login_rejected", f"QQ 连不上：{text}")
        elif kind == "locked":
            self._set_attach("attach_locked", f"{exc.text}，过了再试", pid, min(1200.0, exc.wait_s + 5))
        elif kind in ("totp", "consent", "must_change"):
            text = f"{exc.text}；请在 SnowLuma 管理页处理（或手动挂接一次），我先不试了。"
            self._set_attach("need_user_webui", text, pid, 600)
            await self._tell("qq_need_webui", f"QQ 连不上：{text}")
        elif kind == "ambiguous":
            self._set_attach("attach_error", exc.text, pid, 600)
            await self._tell("qq_ambiguous", f"QQ 连不上：{exc.text}")
        else:
            self._attach_fail += 1
            wait = min(300, 20 * 2 ** min(self._attach_fail - 1, 4))
            self._set_attach("attach_error", f"挂接没成功：{exc.text}；{wait} 秒后重试", pid, wait)
            if self._attach_fail >= 4:
                await self._tell("qq_attach_error", f"QQ 一直连不上，替她挂接也失败了（{exc.text}）。可以到 SnowLuma 的管理页手动挂接。",
                                 cooldown_s=3 * 3600)

    async def _attached(self, res: "H.AttachResult", uin: int, pid: int | None, now: float) -> None:
        if res.state != "login_window":  # 登录窗口不在了 (登录上了、关了): 之前读到的二维码作废
            self._qr, self._waiting_pids = None, []
        if res.state in ("attached", "refreshed", "already"):
            self._attach_fail = 0
            self._attach_miss += 1
            self._waiting_since = 0.0
            if self._attach_miss >= 3:
                self._set_attach("attach_error", "管理页说她的 QQ 已经挂着了，但 OneBot 的端口一直没开：请看 SnowLuma 管理页里的 OneBot 网络"
                                                 "是不是开着、端口对不对", pid, 300)
                await self._tell("qq_port_closed", "QQ 连不上：SnowLuma 说她的 QQ 已经挂着了，但 OneBot 的端口一直没开，请看 SnowLuma 管理页里的 OneBot 网络设置。")
                return
            self._set_attach("attaching", ("已替她挂接 QQ" if res.state != "already" else "她的 QQ 已经挂着") + "，等 OneBot 端口就绪",
                             pid, ATTACH_GRACE_S)
            return
        self._attach_miss = 0
        self._waiting_since = self._waiting_since or self.clock()
        if res.state == "login_window":
            self._waiting_pids = list(res.waiting)
            await self._scan_step(res.waiting, uin, pid)
            return
        if res.state == "no_account":
            self._set_attach("waiting_qq", f"电脑上的 QQ 登录的都不是她的号（{uin}），我没动；用她的号登录后我会自己挂接", pid, 30)
        else:
            self._set_attach("waiting_qq", f"SnowLuma 没看到 QQ 进程，等她的号（{uin}）登录", pid, 20)
        await self._nag(self.clock(), f"她的 QQ（{uin}）没登录，所以 QQ 暂时连不上。请在电脑上用她的号登录 QQ，我会自己挂接。")

    async def _nag(self, now: float, text: str) -> None:
        if self._waiting_since and now - self._waiting_since >= NAG_AFTER_S:
            await self._tell("qq_waiting", text, cooldown_s=NAG_EVERY_S)

    async def _scan_step(self, pids: list[int], uin: int, pid: int | None) -> None:
        """QQ is at its login window: read the QR code off it (read-only) so the owner can scan it from the vault page."""
        now = self.clock()
        if now < self._qr_at and self.qr_png() is not None:
            self._set_attach("need_scan", f"有一个 QQ 停在登录界面等扫码；用手机 QQ 扫（最好是她的号 {uin}），二维码在 {_vault.PAGE_URL}", pid, 20)
            return
        if await self._capture(pids):
            self._set_attach("need_scan", f"有一个 QQ 停在登录界面等扫码；用手机 QQ 扫（最好是她的号 {uin}），二维码在 {_vault.PAGE_URL}", pid, 20)
            await self._tell("qq_scan", f"QQ 掉线了，电脑上有个 QQ 停在登录界面等扫码。用手机 QQ 扫二维码登录（用她的号 {uin}）：{_vault.PAGE_URL}",
                             cooldown_s=20 * 60, image=self.qr_png())
        else:
            self._set_attach("waiting_login", f"有一个 QQ 停在登录界面，但我没认出二维码（可能要点「登录」、验证或收在托盘里）；"
                                              f"请在电脑上把它登录成她的号（{uin}）", pid, QR_EVERY_S)
            await self._nag(now, f"她的 QQ（{uin}）停在登录界面，请在电脑上登录一下，我会自己挂接。")

    async def _capture(self, pids: list[int]) -> bool:
        self._qr_at = self.clock() + QR_EVERY_S
        try:
            shot = await asyncio.wait_for(asyncio.to_thread(QW.snapshot_qr, pids), 20)
        except (asyncio.TimeoutError, OSError, ValueError, ImportError):
            shot = None
        self._qr = shot
        return shot is not None
