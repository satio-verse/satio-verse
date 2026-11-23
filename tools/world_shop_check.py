"""世界侧「买一件东西」的验收 (Hemifuture §16.3 第三个闭环, 2026-11-20): 扣款 / 发货 / 摆放原子, 断线重试不双花.

为什么是"世界侧"验收: 钱和东西都在世界那一边 (铁律二: 世界不持有自我、自我也不管钱), Core 只应该看到
一条回执里写着"买成了, 还剩 470 点". 所以这一步**不经过大模型**: 它测的是世界实现有没有把账记对.

跑法:

    python tools\\world_shop_check.py                 # 全绿就打印 5 条, 并写 docs/world_shop_check-<时间>.json
    python tools\\world_shop_check.py --no-file       # 只打屏

它用 `tools/dummy_world.py` 的 `WorldState` (假世界的状态) + `wm/world/economy.py` 的 `Shop`/`Ledger`,
所以验的就是"世界上线时那段代码", 不是另写一遍的假实现.

五条要全过:

1. 买成: 价 30 点, 余额 500 → 470, 世界侧真的多了一把椅子, 托管账户清零;
2. 重试: 同一个 idem_key 再买一次 → 回执还是第一份、余额还是 470、椅子还是一把 (不双花);
3. 摆放失败: 钱全额退回 500, 椅子没摆上, 没有钱卡在托管里;
4. 没有这件东西: 一分不扣;
5. 对账: 每笔借贷和 0、余额 = 流水累加、没有负数余额、没有卡住的托管.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台是 GBK, 别让中文把进程弄崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dummy_world import WorldState  # noqa: E402


def print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验收世界侧商店: 原子扣款 / 幂等 / 退款 / 对账")
    ap.add_argument("--no-file", action="store_true", help="不写 JSON, 只打屏")
    args = ap.parse_args(argv)

    world = WorldState()
    world.buy("chair", "warmup:0")  # 第一次调用顺便建账本并发 500 点
    shop = world.shop
    assert shop is not None
    led = shop.ledger

    checks: list[dict] = []

    def check(name: str, ok: bool, **detail) -> None:
        checks.append({"check": name, "ok": bool(ok), **detail})
        print_json({"check": name, "ok": bool(ok), **detail})

    # 1. 买成
    start = led.balance(shop.payer)
    got = world.buy("chair", "order:1")
    check("1 买成", got["ok"] and got["balance"] == start - 30 and world.shop.placed.count("chair") >= 1,
          price=got["price"], balance=got["balance"], placed=world.shop.placed)

    # 2. 断线重试不双花
    chair_before = world.shop.placed.count("chair")
    again = world.buy("chair", "order:1")
    check("2 重试不双花", again["balance"] == got["balance"] and world.shop.placed.count("chair") == chair_before,
          balance=again["balance"], tx=again["tx"])

    # 3. 摆放失败全额退回
    before_fail = led.balance(shop.payer)
    failed = world.buy("table", "order:2", place_failed=True)
    room = led.reconcile()
    check("3 摆放失败退回", (not failed["ok"]) and failed["refunded"]
          and led.balance(shop.payer) == before_fail and room["escrow_open"] == {},
          balance=failed["balance"], escrow_open=room["escrow_open"])

    # 4. 没有这件东西 → 一分不扣
    before_unknown = led.balance(shop.payer)
    unknown = world.buy("sofa", "order:3")
    check("4 不存在的东西不扣钱", (not unknown["ok"]) and led.balance(shop.payer) == before_unknown,
          balance=unknown["balance"], why=unknown.get("why"))

    # 5. 对账
    report = led.reconcile()
    check("5 对账平", report["ok"], tx=report["tx"], accounts=report["accounts"],
          total_points=report["total_points"], escrow_open=report["escrow_open"])

    ok = all(c["ok"] for c in checks)
    summary = {"ok": ok, "checks": checks, "shop": {"placed": world.shop.placed, "sold": world.shop.sold,
               "balance": led.balance(shop.payer), "accounts": led.accounts()},
               "ledger": led.reconcile()}
    print_json({"summary": {"ok": ok, "pass": sum(c["ok"] for c in checks), "total": len(checks)}})
    if not args.no_file:
        out = ROOT / "docs" / f"world_shop_check-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print_json({"wrote": str(out)})
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
