"""游戏世界·只看画面 的本机演示 (世界2). 样板: 主程序不引用. 只在本机跑, 不联网, 不存盘, 不调真模型.

  python tools/game_watch_demo.py --selftest
      合成的画面、假前台窗口、假模型、假时钟: 跑一遍"开局 -> 换幕 -> 你说不看了", 再跑 3 份双胞胎夹具; 打印时间线, 全部符合就退出码 0.
  python tools/game_watch_demo.py --live-dry
      在你自己的终端里用: 真的看前台窗口、真的取景 (SA.foreground / SA.grab, 就是桌宠陪看用的那两个), 但模型是**不联网**的假模型,
      只在终端里告诉你"这一眼取到了几张、拼图多大". 先开一个文字多的单机游戏, 再在这个终端里打"陪我玩游戏"开始、打"不看了"停.
      用来在真游戏上核对: 取的是不是游戏那个前台窗口、切去聊天 / 写代码时是不是不取、换幕时会不会触发一眼.
      截图只在内存里, 看完就扔; 不会发到任何地方.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wm.world import game_twin as T  # noqa: E402
from wm.world.game_watch import PLUGIN_DIR, load_package  # noqa: E402

SKIP_ZH = {"private": "聊天/密码/网银/登录类", "self": "桌宠自己", "work": "写代码/办公", "small": "太小的窗口", "none": "没有前台窗口"}


def fmt_events(w) -> list[str]:
    out = []
    for i in w.log:
        if i["k"] == "in":
            f = " ".join(f"{k}={v}" for k, v in i["fields"].items())
            out.append(f"    [#{i['seq']} {i['sense']} {i['type']}] {f}{': ' + i['text'] if i['text'] else ''}")
    return out


def selftest() -> int:
    manifest, spec, env = load_package()
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("  [通过] " if ok else "  [不符合] ") + what)
        if not ok:
            problems.append(what)

    base = json.loads((PLUGIN_DIR / "fixtures" / "twin-private-flicker.json").read_text(encoding="utf-8"))
    fx = {**base, "name": "demo", "run_s": 60, "variants": {"A": {}, "B": {}},
          "timeline": [{"at": 0, "window": "game", "scene": "dialog"}, {"at": 15, "scene": "battle"}, {"at": 30, "window": "qq"},
                       {"at": 36, "window": "game", "scene": "options"}],
          "owner_says": [{"at": 20, "text": "这个boss好难"}], "owner_calls": [{"at": 45, "text": "不看了"}]}
    print("== 演示: 开局 -> 换幕 -> 他说话 -> 切去聊天窗口 6 秒 -> 又换幕 -> 他说不看了")
    r = asyncio.run(T.run_variant(fx, "A", spec, env))
    for t, text in r.said:
        print(f"  第 {t:>4} 秒 她说: {text}")
    for line in fmt_events(r.watch):
        print(line)
    print(f"  取景 {len(r.screen.grabs)} 次, 窗口全是: {sorted({g[1] for g in r.screen.grabs})}")
    print("  面板: ", json.dumps(r.watch.panel()["she_did_not_see"], ensure_ascii=False))
    check(bool(r.said) and r.said[0][0] <= 5 and "夜晚的对话场景" in r.said[0][1], "开局 5 秒内说出对得上的一句")
    check(any(15 <= t <= 20 and "战斗" in s for t, s in r.said), "换幕 5 秒内说出对得上的一句")
    check(bool(r.calls) and r.calls[0][2] and "月下的信" in r.calls[0][3], "说'不看了'马上停, 收尾一句带着游戏标题")
    check({g[1] for g in r.screen.grabs} == {"vnreader.exe"}, "只取了游戏窗口, 没取聊天窗口")
    check(not r.watch.alive and r.watch.eye.frames == [], "结束后画面都扔了")
    print("== 双胞胎夹具")
    for p in sorted((PLUGIN_DIR / "fixtures").glob("*.json")):
        rep = T.run_game_twin(json.loads(p.read_text(encoding="utf-8")), spec, env)
        check(rep.ok, f"{p.stem}（期望 {rep.expect}）")
    print("通过" if not problems else f"有 {len(problems)} 项不符合")
    return 1 if problems else 0


class DryLLM:
    """不联网的假模型, 也看不懂图: 只在终端里报一眼取到了什么 (拼图的大小), 不说话."""

    async def stream(self, messages, schema=None, max_tokens=0, id_slot=None, meta=None):
        content = messages[-1]["content"]
        if isinstance(content, list):
            from PIL import Image
            url = next(p["image_url"]["url"] for p in content if p.get("type") == "image_url")
            im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            text = content[0]["text"]
            kind = "画面大变" if "change=big" in text else "第一眼" if "first=true" in text else "定时看"
            print(f"  [这一眼] {kind}: 取到一张 {im.width}x{im.height} 的拼图（只在内存里，没发出去）")
        yield json.dumps({"actions": []})


async def live(max_s: float) -> int:
    from wm.world.game_watch import GameWatch
    manifest, spec, env = load_package()

    async def speak(note, reply, emo) -> bool:
        print(f"  她说: {reply}")
        return True

    def trace(ev: dict) -> None:
        if ev.get("event") in ("away", "switch", "pause", "resume"):
            print(f"  [{ev['event']}] {ev.get('app', '')} {ev.get('why', '')}".rstrip())

    w = GameWatch(spec, DryLLM(), speak, env_prompt=env, trace=trace)
    print("先开好一个文字多的单机游戏。在这个终端里打: 陪我玩游戏 -> 开始; 不看了 -> 停; Ctrl+C -> 退出。")
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < max_s:
            text = (await asyncio.to_thread(input, "你对她说> ")).strip()
            ok, reply = await w.owner_call(text)
            if ok:
                print("  她回你:", reply)
            if w.eye is not None and not w.alive:
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if w.alive:
            await w.leave(speak_line=False)
    p = w.panel()
    print("== 这次: 看了", p["stats"]["looks"], "眼, 截了", p["stats"]["captures"], "张, 假装上云", p["stats"]["uploads"], "次（其实没发）")
    for k, n in p["she_did_not_see"]["not_captured"].items():
        print(f"   没取画面（{SKIP_ZH.get(k, k)}）: 巡视 {n} 次, 切去 {p['she_did_not_see']['episodes'].get(k, 0)} 回")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="游戏世界·只看画面 的本机演示 (样板, 不联网不存盘)")
    ap.add_argument("--selftest", action="store_true", help="合成画面 + 假模型, 跑一遍就退出")
    ap.add_argument("--live-dry", action="store_true", help="真前台窗口 + 真取景 + 不联网的假模型 (在你自己的终端里用)")
    ap.add_argument("--max-s", type=float, default=1800.0)
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.live_dry:
        return asyncio.run(live(args.max_s))
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
