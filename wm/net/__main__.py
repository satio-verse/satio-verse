"""python -m wm.net: 只读网络体检的命令行 (2026-11-11 网络插件第 0 步).

默认: 采集 (只读, 起一个 PowerShell, 约 5-10 秒) -> 分析 -> 打印摘要.
  --json              打印完整 JSON ({"report": 体检报告, "snapshot": 采到的原始数据})
  --save 文件         把这次采到的快照存下来 (UTF-8 JSON; 里面没有命令行、路径、账号密码, 进程、服务、监听也只留分析用得上的)
  --from-file 文件    不采集, 用存下来的快照分析 (读进来也筛一遍, 见 trim.py)
  --probe 127.0.0.1:端口   单独做一次 SOCKS5 + UDP ASSOCIATE 握手探测 (只能 127.0.0.0/8, 不发业务数据; 体检默认不做这个)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from wm.privilege import is_elevated

from .checkup import analyze
from .collect import collect
from .snapshot import Snapshot
from .socks_probe import probe_socks5
from .trim import trim


def _host_port(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise argparse.ArgumentTypeError("要写成 地址:端口，比如 127.0.0.1:1082")
    return host, int(port)


def _probe(host: str, port: int, as_json: bool) -> int:
    result = asyncio.run(probe_socks5(host, port))
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"SOCKS5：{'是' if result['socks5'] else '不是 / 没确认'}；UDP：{'支持' if result['udp'] else '不支持 / 没确认'}")
        print(f"详情：{result['detail']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认不是 UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m wm.net", description="只读网络体检: 认出本机的代理客户端和端口、系统代理、虚拟网卡、默认路由、遗留网卡.")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON")
    ap.add_argument("--save", metavar="文件", help="把这次采到的快照存成 JSON 文件")
    ap.add_argument("--from-file", metavar="文件", help="不采集, 用存下来的快照分析")
    ap.add_argument("--probe", metavar="地址:端口", type=_host_port, help="单独探测一个 127.0.0.0/8 里的端口是不是 SOCKS5、支不支持 UDP")
    a = ap.parse_args(argv)
    if a.probe:
        if a.save or a.from_file:
            ap.error("--probe 要单独用 (最多再加 --json)")
        return _probe(*a.probe, a.json)
    if a.from_file:
        try:
            snap = Snapshot.from_json(Path(a.from_file).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"读不了快照文件：{exc}", file=sys.stderr)
            return 1
    else:
        print("正在只读地采集网络现状（约 10 秒）…", file=sys.stderr)
        snap = collect()
    snap = trim(snap)  # 存好的旧快照 (带着全部进程) 读进来也筛一遍; 刚采的已经筛过, 再筛不变
    if a.save:
        Path(a.save).write_text(snap.to_json(), encoding="utf-8", newline="\n")
    report = analyze(snap, elevated=is_elevated())
    if a.json:
        print(json.dumps({"report": report.to_dict(), "snapshot": snap.to_dict()}, ensure_ascii=False, indent=2))
    else:
        print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
