"""下载队列端到端验收 (2026-11-11; 不连外网、不花钱). 本机起两个假下载站 (tests/dl_fakes.py)、一个假模型 (tests/test_cloud.FakeCloud)、
一个测试服务端 (python -m wm, 另挑端口、临时家目录、替身引擎, 和 serve.py 试跑同一套参数), 照知识库《下载队列》那一页的验收走:
  1 没点头时传输量为 0, 点了才开始 (发起的会话是完全授权档)
  2 下到一半杀掉下载服务: 看守几秒内拉起、从断点接着下, 哈希对; 再把测试服务端整个杀掉重起: 下载不断
  3 哈希不对: 文件删掉、任务标失败
  4 另一个会话看得到、停不了
  5 卡片上没列的来源 (跳转) 先停下再问、不去连; 内网地址、下载目录外面的路径不收
  6 下完 (没成、没同意的也算) 都回到发起的会话
每条打印数字, 写进 <目录>/结果.json. 跑完留一张没点的卡片和测试服务端, 方便在页面上看 (--no-keep 就全停掉);
留着的进程记在 <目录>/procs.json, 用 --stop 停掉.

  python tools/downloads_e2e.py <临时目录> [--no-keep]
  python tools/downloads_e2e.py <临时目录> --stop
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
from dl_fakes import blob, read_log, sha256  # noqa: E402
from test_cloud import KEY, FakeCloud, call, say  # noqa: E402
from wm.workbench import TRIAL_ARGS, child_env  # noqa: E402

SIZE, RATE = 2_500_000, 250_000          # 每个 2.5 MB, 假下载站每条连接每秒发 250 KB: 一个要下 10 秒
FILES = {"a.bin": (SIZE, 11), "b.bin": (SIZE, 12), "c.bin": (SIZE, 13), "d.bin": (200_000, 16), "bad.bin": (300_000, 14)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(pid) -> None:
    try:
        p = psutil.Process(int(pid))
        for child in p.children(recursive=True):
            child.kill()
        p.kill()
    except (psutil.Error, TypeError, ValueError):
        pass


def start_site(out: Path, name: str, files: dict, rate: int = 0, redirects=()) -> tuple[subprocess.Popen, int, Path]:
    log = out / f"{name}.jsonl"
    cmd = [sys.executable, str(ROOT / "tests" / "dl_fakes.py"), "--port", "0", "--rate", str(rate), "--log", str(log)]
    for fname, (size, seed) in files.items():
        cmd += ["--file", f"{fname}:{size}:{seed}"]
    for r in redirects:
        cmd += ["--redirect", r]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
    return p, int(p.stdout.readline().split()[1]), log


def start_server(out: Path, port: int, allow: str, n: int) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "wm", "--port", str(port), *TRIAL_ARGS,
           "--set", f"agent.home={(out / 'agent').as_posix()}", "--set", f"trace.log_dir={(out / 'logs').as_posix()}",
           "--set", f"agent.workspace={(out / 'ws').as_posix()}", "--set", f"downloads.dir={(out / 'files').as_posix()}",
           "--set", "downloads.port=0"]
    env = child_env(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", WM_DL_TEST_ALLOW=allow)
    env.pop("DEEPSEEK_API_KEY", None)  # 环境里有真的 key: 测试服务端拿不到
    log = open(out / f"server-{n}.log", "wb")  # noqa: SIM115 (交给子进程)
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def enqueue(port: int, name: str, label: str, **more) -> tuple[str, str]:
    size, seed = FILES[name]
    args = {"urls": [f"http://127.0.0.1:{port}/{name}"], "name": label, "size": size, "license": "MIT",
            "sha256": sha256(blob(size, seed)), "dest": f"models/{name}", **more}
    return "download_enqueue", json.dumps({k: v for k, v in args.items() if v is not None}, ensure_ascii=False)


class Run:
    def __init__(self, out: Path):
        self.out = out
        self.res: dict = {}
        self.procs: dict = {}
        self.port = free_port()

    # ---- 假模型: 照最后一句用户消息回 ----
    def plan(self, last: str):
        site = self.site_port
        if last.startswith("[下载有结果了"):
            return [say("看到下载的结果了。")]
        if "三个模型文件" in last:
            return [call(enqueue(site, "a.bin", "测试模型 A"), enqueue(site, "b.bin", "测试模型 B"), enqueue(site, "c.bin", "测试模型 C"),
                         preamble="三个都排进下载队列。"), say("三个都排好了，等你在卡片上点头。")]
        if "暂停 D1" in last:
            return [call(("download_status", "{}"), ("download_pause", '{"id": "D1"}'), ("download_cancel", '{"id": "D1"}')),
                    say("D1 不是这个会话排的：看得到，停不了。")]
        if "坏的那个" in last:
            return [call(enqueue(site, "bad.bin", "哈希写错的文件", sha256="0" * 64)), say("排好了。")]
        if "会跳转的" in last:
            return [call(("download_enqueue", json.dumps({"urls": [f"http://127.0.0.1:{site}/go.bin"], "name": "会跳转的文件",
                                                          "dest": "models/go.bin"}, ensure_ascii=False))), say("排好了。")]
        if "内网和目录外" in last:
            return [call(("download_enqueue", json.dumps({"urls": ["http://127.0.0.1:9/x.bin"], "name": "本机的"})),
                         ("download_enqueue", json.dumps({"urls": ["http://192.168.1.10/x.bin"], "name": "内网的"})),
                         ("download_enqueue", json.dumps({"urls": [f"http://127.0.0.1:{site}/a.bin"], "name": "目录外",
                                                          "dest": "..\\..\\evil.bin"})),
                         ("download_enqueue", json.dumps({"urls": [f"http://127.0.0.1:{site}/a.bin"], "name": "转去内网",
                                                          "dest": "models/x.bin", "redirect_hosts": ["10.0.0.1"]}))),
                    say("这几个都没收。")]
        if "先别点" in last:
            return [call(enqueue(site, "d.bin", "留给页面看的")), say("排好了，卡片在上面。")]
        return [say("好的。")]

    async def reply(self, request, body):
        msgs = body.get("messages", [])
        at = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        last = str(msgs[at].get("content") or "") if at >= 0 else ""
        steps = self.plan(last)
        rounds = sum(m.get("role") == "assistant" for m in msgs[at + 1:])
        return await steps[min(rounds, len(steps) - 1)](request, body)

    # ---- 和测试服务端说话 ----
    @property
    def H(self):
        return {"Origin": f"http://127.0.0.1:{self.port}"}

    async def wait_up(self, proc, timeout=240.0) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if proc.poll() is not None:
                raise RuntimeError("测试服务端起不来, 看 server-*.log")
            try:
                async with self.http.get(f"http://127.0.0.1:{self.port}/api/status", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        return time.monotonic() - t0
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(f"测试服务端 {timeout:.0f} 秒没起来")

    async def register_model(self) -> None:
        async with self.http.ws_connect(f"http://127.0.0.1:{self.port}/ws") as ws:
            await ws.send_json({"type": "channel_config", "model": {"provider": "custom", "base_url": self.model_base, "api_key": KEY,
                                                                     "model": "fake-chat"}})
            while (await asyncio.wait_for(ws.receive_json(), 60)).get("type") != "channel_config_done":
                pass

    async def post(self, path, body):
        async with self.http.post(f"http://127.0.0.1:{self.port}{path}", json=body, headers=self.H) as r:
            return await r.json()

    async def get(self, path):
        async with self.http.get(f"http://127.0.0.1:{self.port}{path}", headers=self.H) as r:
            return await r.json()

    async def idle(self, sid: str, timeout: float = 120.0) -> None:
        """等这个会话没在做事 (下完回报会让她接着做一轮; 机器慢时上一轮还没完就发下一句会被挡回)."""
        await self.until(lambda: self._idle(sid), timeout, 0.3, f"会话 {sid} 空闲")

    async def _idle(self, sid: str):
        return (await self.get(f"/api/agent?id={sid}")).get("status") == "idle"

    async def turn(self, sid: str, text: str, timeout: float = 90.0) -> list[dict]:
        await self.idle(sid)
        events = []
        async with self.http.ws_connect(f"http://127.0.0.1:{self.port}/agent/ws?id={sid}", headers=self.H) as conn:
            await conn.send_json({"type": "send", "text": text, "context_ids": []})
            while True:
                ev = await asyncio.wait_for(conn.receive_json(), timeout)
                events.append(ev)
                if ev.get("type") == "done":
                    return events
                if ev.get("type") == "refused":
                    raise RuntimeError(f"这一句没发出去: {ev.get('text')}")

    async def answer(self, sid: str, card: str, how: str) -> None:
        """像你在页面上点卡片那样 (同一条 WS 上行)."""
        async with self.http.ws_connect(f"http://127.0.0.1:{self.port}/agent/ws?id={sid}", headers=self.H) as conn:
            await conn.send_json({"type": "answer", "id": card, "how": how})
            await asyncio.sleep(0.3)

    async def jobs(self) -> dict:
        got = await self.get("/api/agent/downloads")
        return {j["id"]: j for j in got.get("jobs", [])}

    async def until(self, pred, timeout=60.0, step=0.2, what="条件"):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            got = pred()
            got = await got if asyncio.iscoroutine(got) else got
            if got:
                return got
            await asyncio.sleep(step)
        raise RuntimeError(f"{timeout:.0f} 秒没等到: {what}")

    def site_rows(self, kind="request", log=None):
        return [r for r in read_log(log or self.site_log) if r["ev"] == kind]

    def parts(self) -> dict:
        return {p.name: p.stat().st_size for p in (self.out / "files").rglob("*.part")}

    def dl_home(self) -> Path:
        return self.out / "serve" / "downloads"

    async def session_events(self, sid: str) -> list[dict]:
        return (await self.get(f"/api/agent?id={sid}")).get("events", [])

    # ---- 一遍 ----
    async def main(self, keep: bool) -> dict:
        out = self.out
        (out / "ws").mkdir(parents=True, exist_ok=True)
        cdn, self.cdn_port, self.cdn_log = start_site(out, "cdn", {"real.bin": (100_000, 15)})
        site, self.site_port, self.site_log = start_site(out, "site", FILES, RATE, [f"go.bin=http://127.0.0.1:{self.cdn_port}/real.bin"])
        self.procs.update(cdn=cdn.pid, site=site.pid)
        allow = f"127.0.0.1:{self.site_port},127.0.0.1:{self.cdn_port}"
        fake = FakeCloud(self.reply)
        self.model_base = await fake.start()
        self.http = aiohttp.ClientSession()
        try:
            server = start_server(out, self.port, allow, 1)
            self.procs["server"] = server.pid
            up = await self.wait_up(server)
            await self.register_model()
            print(f"测试服务端起来了: 127.0.0.1:{self.port} ({up:.1f} 秒); 假下载站 {self.site_port}, 跳转去的站 {self.cdn_port}", flush=True)
            ws = str(out / "ws")
            await self.post("/api/agent", {"action": "workspace", "path": ws, "name": "下载验收"})
            A = (await self.post("/api/agent", {"action": "new", "workspace": ws, "mode": "unrestricted"}))["id"]
            B = (await self.post("/api/agent", {"action": "new", "workspace": ws, "mode": "workspace"}))["id"]
            self.res["sessions"] = {"A": A, "A_mode": "unrestricted", "B": B, "B_mode": "workspace"}

            # 1 没点头时传输量为 0
            ev = await self.turn(A, "帮我下三个模型文件")
            asks = [e for e in ev if e.get("type") == "ask"]
            cards = [e["id"] for e in asks]
            t_cards = time.time()
            await asyncio.sleep(5.0)
            before = self.site_rows()
            sent_before = sum(r["bytes"] for r in self.site_rows("sent"))
            card_ok = all(all(k in a["text"] for k in ("名称：", "来源：", "大小：", "许可：")) for a in asks)
            self.res["1_no_nod_no_transfer"] = {"cards": len(asks), "card_has_name_source_size_license": card_ok,
                                                "waited_s": round(time.time() - t_cards, 1), "requests_before_nod": len(before),
                                                "bytes_before_nod": sent_before, "card_sample": asks[0]["text"] if asks else ""}
            print(f"[1] 三张卡片弹出来了, 等了 {time.time() - t_cards:.1f} 秒: 假下载站收到 {len(before)} 个请求、发出 {sent_before} 字节", flush=True)
            t_nod = time.time()
            for cid in cards:
                await self.answer(A, cid, "once")
            await self.until(lambda: self.site_rows(), 20, 0.05, "点头以后的第一个请求")
            first = min(r["t"] for r in self.site_rows())
            self.res["1_no_nod_no_transfer"]["first_request_after_nod_s"] = round(first - t_nod, 2)
            print(f"[1] 点头以后 {first - t_nod:.2f} 秒发出第一个请求", flush=True)

            # 4 另一个会话看得到、停不了
            evb = await self.turn(B, "暂停 D1")
            results = [e for e in evb if e.get("type") == "tool_result"]
            seen = json.loads(results[0]["text"]) if results and results[0].get("ok") else {}
            d1 = next((j for j in seen.get("jobs", []) if j["id"] == "D1"), {})
            jobs = await self.jobs()
            self.res["4_other_session"] = {"sees_D1": bool(d1), "D1_mine_for_B": d1.get("mine"),
                                           "pause": {"ok": results[1]["ok"], "text": results[1]["text"][:120]} if len(results) > 1 else None,
                                           "cancel": {"ok": results[2]["ok"], "text": results[2]["text"][:120]} if len(results) > 2 else None,
                                           "D1_state_after": jobs["D1"]["state"]}
            print(f"[4] 会话 B 看得到 D1 (mine={d1.get('mine')}), 暂停/取消都被拒; D1 还是 {jobs['D1']['state']}", flush=True)

            # 2a 下到一半杀掉下载服务 (等检查点真的落了盘: 记录里 checkpoint ≥ 40%)
            await self.until(self._checkpointed, 60, 0.1, "D1 的检查点落盘 (≥ 40%)")
            daemon = json.loads((self.dl_home() / "daemon.json").read_text("utf-8"))
            watch = json.loads((self.dl_home() / "watch" / "state.json").read_text("utf-8"))
            ck = {i: json.loads((self.dl_home() / "jobs" / f"{i}.json").read_text("utf-8")).get("checkpoint") for i in ("D1", "D2")}
            on_disk = self.parts()
            t_kill = time.time()
            psutil.Process(daemon["pid"]).kill()
            back = await self.until(lambda: self._daemon_back(daemon["pid"]), 30, 0.05, "下载服务被看守拉起来")
            took = time.time() - t_kill
            resumed = await self.until(lambda: self._resumed(t_kill, 2), 20, 0.1, "两个在下的都发出续传请求")
            starts = {r["path"]: int(r["range"].removeprefix("bytes=").rstrip("-")) for r in resumed}
            self.res["2a_kill_daemon"] = {"restarted_in_s": round(took, 2), "limit_s": 10, "old_pid": daemon["pid"], "new_pid": back["pid"],
                                          "watch_pid": watch["pid"], "checkpoint_on_disk_at_kill": ck, "part_bytes_at_kill": on_disk,
                                          "resume_requests": [{"path": r["path"], "range": r["range"], "status": r["status"],
                                                               "if_range": r["if_range"]} for r in resumed],
                                          "resume_start_not_before_checkpoint": all(
                                              starts.get(f"{n}.bin", -1) >= (ck.get(i) or 1) for n, i in (("a", "D1"), ("b", "D2")))}
            print(f"[2] 杀掉下载服务 (进程 {daemon['pid']}), {took:.2f} 秒后看守拉起新的 (进程 {back['pid']}); 杀的时候磁盘上的检查点 {ck}; "
                  f"续传 {[(r['path'], r['range']) for r in resumed]}", flush=True)

            # 2b 把测试服务端整个杀掉 (连进程树), 过一阵再起: 下载不断
            await asyncio.sleep(1.5)
            t_down = time.time()
            kill_tree(server.pid)
            server.wait(15)
            p1 = self.parts()
            await asyncio.sleep(3.0)
            p2 = self.parts()
            alive = {"daemon": psutil.pid_exists(back["pid"]), "watch": psutil.pid_exists(watch["pid"])}
            grew = {k: p2.get(k, 0) - v for k, v in p1.items()}
            server = start_server(out, self.port, allow, 2)
            self.procs["server"] = server.pid
            up2 = await self.wait_up(server)
            await self.register_model()
            gap = time.time() - t_down
            print(f"[2] 测试服务端停了 {gap:.1f} 秒 (重起用了 {up2:.1f} 秒); 停着的时候 3 秒里多下了 {grew} 字节; "
                  f"下载服务和看守还在: {alive}", flush=True)
            final = await self.until(lambda: self._all_state(("D1", "D2", "D3"), "done"), 120, 0.3, "三个都下完")
            hashes = {n: hashlib.sha256((out / "files" / "models" / n).read_bytes()).hexdigest() == sha256(blob(*FILES[n]))
                      for n in ("a.bin", "b.bin", "c.bin")}
            fresh = [r for r in self.site_rows() if r["path"] in ("a.bin", "b.bin", "c.bin") and not r["range"]]
            during = [r for r in self.site_rows() if t_down <= r["t"] <= t_down + gap and not r["range"]]
            self.res["2b_restart_server"] = {"server_down_s": round(gap, 1), "server_boot_s": round(up2, 1),
                                             "bytes_while_server_down_3s": grew, "daemon_and_watch_alive": alive,
                                             "from_scratch_requests_per_file": {n: sum(r["path"] == n for r in fresh) for n in ("a.bin", "b.bin", "c.bin")},
                                             "new_connections_from_scratch_while_down": len(during),
                                             "all_done": {k: v["state"] for k, v in final.items()}, "sha256_ok": hashes}
            print(f"[2] 三个都下完了, 哈希 {hashes}; 每个文件从头要过几次: {self.res['2b_restart_server']['from_scratch_requests_per_file']}", flush=True)

            # 6 下完回到发起的会话
            reports = await self.until(lambda: self._reports(A, 3), 60, 0.5, "三条回报")
            self.res["6_reported"] = {"A_job_done": [{"id": r["job"]["id"], "how": r["job"]["how"], "delivered": r["delivered"]} for r in reports],
                                      "B_job_done": len([e for e in await self.session_events(B) if e.get("type") == "job_done"])}
            print(f"[6] 会话 A 收到 {len(reports)} 条回报: {[(r['job']['id'], r['delivered']) for r in reports]}", flush=True)

            # 3 哈希不对
            ev = await self.turn(A, "下坏的那个")
            card = next(e["id"] for e in ev if e.get("type") == "ask")
            await self.answer(A, card, "once")
            bad = await self.until(lambda: self._state("D4", "failed"), 60, 0.2, "D4 没成")
            leftovers = sorted(p.name for p in (out / "files").rglob("bad.bin*"))
            r4 = await self.until(lambda: self._reports(A, 4), 60, 0.5, "D4 的回报")
            self.res["3_bad_hash"] = {"state": bad["state"], "error": bad.get("error", "")[:160], "files_left": leftovers,
                                      "reported": r4[-1]["job"]["text"]}
            print(f"[3] D4 {bad['state']}: {bad.get('error', '')[:80]}; 留下的文件 {leftovers}", flush=True)

            # 5 卡片上没列的来源 / 内网 / 目录外
            ev = await self.turn(A, "下会跳转的那个")
            card = next(e["id"] for e in ev if e.get("type") == "ask")
            await self.answer(A, card, "once")
            ask2 = await self.until(lambda: self._ask_for("D5", 2), 30, 0.2, "D5 的第二张卡片")
            cdn_hits = len(self.site_rows(log=self.cdn_log))
            await self.answer(A, ask2["ask"]["card"], "deny")
            d5 = await self.until(lambda: self._state("D5", "denied"), 30, 0.2, "D5 没同意")
            n_jobs = len(await self.jobs())
            ev = await self.turn(A, "下内网和目录外的")
            refused = [{"ok": e["ok"], "text": e["text"][:100]} for e in ev if e.get("type") == "tool_result"]
            self.res["5_refused"] = {"redirect_card": {"why": ask2["ask"]["why"], "host": ask2["ask"].get("host"),
                                                       "cdn_requests_before_answer": cdn_hits, "after_deny": d5["state"],
                                                       "cdn_requests_after": len(self.site_rows(log=self.cdn_log))},
                                     "enqueue_refused": refused, "new_jobs": len(await self.jobs()) - n_jobs,
                                     "new_cards": len([e for e in ev if e.get("type") == "ask"])}
            print(f"[5] 跳转去没列的来源: 停下再问 ({ask2['ask']['why']} {ask2['ask'].get('host')}), 那个站收到 {cdn_hits} 个请求; "
                  f"内网/目录外 {len(refused)} 个全被拒: {[x['ok'] for x in refused]}", flush=True)

            # 留一张没点的卡片给页面看
            await self.turn(A, "再排一个先别点")
            self.res["page"] = f"http://127.0.0.1:{self.port}/agent?s={A}"
            self.res["ok"] = True
        except Exception as exc:  # noqa: BLE001 (把出错写进结果, 进程照样记下来)
            self.res["ok"] = False
            self.res["error"] = f"{type(exc).__name__}: {exc}"
            print("出错:", self.res["error"], flush=True)
        finally:
            for name in ("daemon.json", "watch/state.json"):
                got = json.loads((self.dl_home() / name).read_text("utf-8")) if (self.dl_home() / name).exists() else {}
                if got.get("pid"):
                    self.procs["dl_" + name.split("/")[0].split(".")[0]] = got["pid"]
            (out / "procs.json").write_text(json.dumps(self.procs), encoding="utf-8")
            (out / "结果.json").write_text(json.dumps(self.res, ensure_ascii=False, indent=1), encoding="utf-8")
            await self.http.close()
            await fake.close()
            if not keep or not self.res.get("ok"):
                stop(out)
        return self.res

    def _checkpointed(self):
        """D1 和 D2 的任务记录 (磁盘上) 里检查点都到了 40% 以上."""
        try:
            return all(json.loads((self.dl_home() / "jobs" / f"{i}.json").read_text("utf-8")).get("checkpoint", 0) >= SIZE * 0.4
                       for i in ("D1", "D2"))
        except (OSError, ValueError):
            return None

    def _resumed(self, since: float, n: int):
        """杀掉以后带着 Range 来要的请求 (每个文件取第一个), 够 n 个了才算."""
        got = {}
        for r in self.site_rows():
            if r["t"] > since and r["range"] and r["path"] not in got:
                got[r["path"]] = r
        return list(got.values()) if len(got) >= n else None

    def _daemon_back(self, old_pid):
        path = self.dl_home() / "daemon.json"
        try:
            got = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if got.get("pid") == old_pid or not psutil.pid_exists(got.get("pid") or 0):
            return None
        try:
            with socket.create_connection(("127.0.0.1", got["port"]), timeout=0.5):
                return got
        except OSError:
            return None

    async def _all_state(self, ids, state):
        jobs = await self.jobs()
        return {i: jobs[i] for i in ids} if all(jobs.get(i, {}).get("state") == state for i in ids) else None

    async def _state(self, jid, state):
        j = (await self.jobs()).get(jid)
        return j if j and j["state"] == state else None

    async def _ask_for(self, jid, seq):
        j = (await self.jobs()).get(jid)
        return j if j and j["state"] == "awaiting" and (j.get("ask") or {}).get("seq") == seq else None

    async def _reports(self, sid, n):
        got = [e for e in await self.session_events(sid) if e.get("type") == "job_done"]
        return got if len(got) >= n else None


def stop(out: Path) -> None:
    """停掉这次起的所有进程. 顺序: 先小看守 (不然它会把刚杀掉的下载服务再拉起来)、再下载服务, 然后是测试服务端和假下载站;
    下载服务和看守的进程号按现在的记录找 (被杀过一次重起的, procs.json 里的是旧的)."""
    try:
        procs = json.loads((out / "procs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        procs = {}
    home = out / "serve" / "downloads"

    def pid_in(rel: str):
        try:
            return json.loads((home / rel).read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            return None
    for pid in (pid_in("watch/state.json"), procs.get("dl_watch"), pid_in("daemon.json"), procs.get("dl_daemon"),
                procs.get("server"), procs.get("site"), procs.get("cdn")):
        if pid:
            kill_tree(pid)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python tools/downloads_e2e.py", description="下载队列端到端验收")
    ap.add_argument("out", type=Path)
    ap.add_argument("--no-keep", action="store_true", help="跑完全停掉 (不留页面)")
    ap.add_argument("--stop", action="store_true", help="停掉上次留着的进程")
    a = ap.parse_args()
    out = a.out.resolve()
    if a.stop:
        stop(out)
        print("停掉了")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(Run(out).main(keep=not a.no_keep))
    print(json.dumps({"ok": res.get("ok"), "page": res.get("page"), "error": res.get("error")}, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
