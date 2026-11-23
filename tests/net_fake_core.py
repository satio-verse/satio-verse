"""假核心 (网络插件干跑用; 只在测试里): 一个独立的脚本, 假装是 mihomo 的样子被起来.

命令行和真核心一样只有两项: `-d <工作目录> -f <配置文件>`, 别的参数一律报错退出 (退出码 2). 只用标准库, 不联网, 不碰系统.

启动时做的事:
- 把自己看到的全部环境变量的名字 (不要值) 写进 <工作目录>/fake-env.json (一个按字母排的列表; Windows 上名字是大写的);
- 读配置文件, 把读到的原文字节的 SHA-256 写进 <工作目录>/fake-config.json ({"sha256": ..., "size": ...});
- 读 <工作目录>/fake-behavior.json (测试在起之前放好, 没有就用默认), 照它行动:
    adapter_delay_s   多久以后"建好网卡" (默认 0.2)
    never_adapter     true 就永远不建 (默认 false)
    exit_after_s      启动后多久自己退出 (默认不退)
    exit_code         自己退出时的退出码 (默认 0)
    matches           {进程名: 条数}: 建好网卡后打印这么多条 "走了上游" 的行
    direct            {进程名: 条数}: 建好网卡后打印这么多条 "直连" 的行 (看护方不该数它们)
    startup_lines     启动时 (等网卡之前) 原样打印的几行
    repeat            {"process": 进程名, "every_s": 秒}: 建好网卡以后每隔这么久再打印一条 "走了上游" 的行, 直到被结束
- "建好网卡" = 在工作目录里建一个标记文件 fake-adapter-WM-Ayane, 用 os.O_TEMPORARY 打开、文件描述符一直不关 ("关句柄就删": 最后一个句柄没了——
  进程正常退出、被强杀、被作业对象结束都一样——系统自己把文件删掉, 和真的网卡是核心进程持有的一个意思; 所以"网卡在不在"就是这个文件在不在,
  不用去查进程号, 也就不会碰上进程号被重用). 别的进程读不了它的内容 (共享冲突), 只看得到在不在, 所以里面什么也不写, 谁也不要去读它.

往标准输出打印的行用它自己的假格式, 都带 WMFAKE 前缀, 例如 `WMFAKE match process=nikke.exe via=upstream`; 每行打完就 flush, 字节一律按 UTF-8 写.
真核心的输出格式要到真机才核对, 所以看护方里"数连接"的解析是可以换的 (wm/net/guardian.py 的 LINE_PARSERS).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ADAPTER_MARKER = "fake-adapter-WM-Ayane"
DEFAULTS = {"adapter_delay_s": 0.2, "never_adapter": False, "exit_after_s": None, "exit_code": 0, "matches": {}, "direct": {},
            "startup_lines": [], "repeat": None}


def make_marker(path: Path) -> int:
    """建"网卡"标记: 关句柄就删的文件, 返回文件描述符 (调用方留着、一直不关). 路径上已经有一个老文件 (不是这么建的) 就先删了再建."""
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_TEMPORARY
    try:
        return os.open(str(path), flags)
    except FileExistsError:
        path.unlink()
        return os.open(str(path), flags)


def say(text: str) -> None:
    try:
        sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
    except (OSError, ValueError):     # 读的那一头没了: 不用管
        pass


def match_line(name: str, via: str) -> str:
    return f"WMFAKE match process={name} via={via}"


def load_behavior(workdir: Path) -> dict:
    behavior = dict(DEFAULTS)
    try:
        loaded = json.loads((workdir / "fake-behavior.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return behavior
    if isinstance(loaded, dict):
        behavior.update({key: loaded[key] for key in DEFAULTS if key in loaded})
    return behavior


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="net_fake_core", allow_abbrev=False, add_help=False)
    ap.add_argument("-d", dest="workdir", required=True)
    ap.add_argument("-f", dest="config", required=True)
    args = ap.parse_args(argv)           # 多一个参数、少一个参数、认不得的参数: argparse 报错并以 2 退出
    workdir = Path(args.workdir)
    started = time.monotonic()
    (workdir / "fake-env.json").write_text(json.dumps(sorted(os.environ), ensure_ascii=False), encoding="utf-8")
    try:
        config = Path(args.config).read_bytes()
    except OSError:
        say("WMFAKE error cannot read config")
        return 2
    (workdir / "fake-config.json").write_text(json.dumps({"sha256": hashlib.sha256(config).hexdigest(), "size": len(config)}),
                                              encoding="utf-8")
    behavior = load_behavior(workdir)
    say("WMFAKE core started")
    for text in behavior["startup_lines"]:
        say(str(text))
    adapter_up = False
    held: list[int] = []                 # 标记文件的描述符: 一直开着不关, 进程没了系统才删文件
    last_repeat = started
    while True:
        elapsed = time.monotonic() - started
        if behavior["exit_after_s"] is not None and elapsed >= behavior["exit_after_s"]:
            say("WMFAKE core exiting")
            return int(behavior["exit_code"])
        if not adapter_up and not behavior["never_adapter"] and elapsed >= behavior["adapter_delay_s"]:
            held.append(make_marker(workdir / ADAPTER_MARKER))
            adapter_up = True
            last_repeat = time.monotonic()
            say("WMFAKE adapter up")
            for name, count in behavior["matches"].items():
                for _ in range(int(count)):
                    say(match_line(name, "upstream"))
            for name, count in behavior["direct"].items():
                for _ in range(int(count)):
                    say(match_line(name, "direct"))
        repeat = behavior["repeat"]
        if adapter_up and repeat and time.monotonic() - last_repeat >= float(repeat["every_s"]):
            last_repeat = time.monotonic()
            say(match_line(repeat["process"], "upstream"))
        time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
