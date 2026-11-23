"""启动: python -m wm [--llm llamacpp] [--tts sherpa] [--asr sherpa] [--rvc mock] [--set a.b=c ...]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import beat

beat.start()  # 看守在等: 从导入代码起就报心跳 (机器忙时导入加载入模型要一两分钟, 看守据此知道还在动, 不当成起不来)

from aiohttp import web  # noqa: E402

from .app import create_app  # noqa: E402
from .config import load_config  # noqa: E402
from .privilege import warn_if_elevated  # noqa: E402


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m wm", description="意图先行的流式语音角色原型")
    ap.add_argument("--config", help="额外的 JSON 配置文件, 覆盖 default.json / local.json")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="覆盖单个配置项, 可重复")
    ap.add_argument("--llm", help="mock | llamacpp")
    ap.add_argument("--asr", help="none | sherpa | vad_sensevoice")
    ap.add_argument("--tts", help="mock | sherpa | kokoro | gpt_sovits | cosyvoice | http")
    ap.add_argument("--rvc", help="none | mock | http")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    overrides = list(args.set)
    for key in ("llm", "asr", "tts", "rvc"):
        if getattr(args, key):
            overrides.append(f"{key}.engine={getattr(args, key)}")
    if args.host:
        overrides.append(f"server.host={args.host}")
    if args.port:
        overrides.append(f"server.port={args.port}")
    cfg = load_config(args.config, overrides)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    warn_if_elevated()  # 以管理员身份在跑: 日志里写一行明显的警告 (wm/privilege.py)
    if os.environ.get("WM_SUPERVISOR_KEY"):  # 看守起的 (带 CREATE_NEW_PROCESS_GROUP): 终端里的子进程收得到 Ctrl+C,
        from .agent.terminal import let_children_take_ctrl_c  # 服务端自己照旧不理 (2026-11-01 "中断"停不下命令)
        let_children_take_ctrl_c()
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    web.run_app(create_app(cfg), host=host, port=port,
                print=lambda *_: print(f"已启动: http://{host}:{port}/  (Ctrl+C 退出)"))


if __name__ == "__main__":
    main()
