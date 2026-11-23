"""面板自检用 (paneltest.ps1): 往测试服务的账本目录写两周的假记录, 再放一个假的 DeepSeek 余额文件
(python -m http.server 把它当余额接口 /user/balance 发出去). 只写到参数给的目录, 不碰真账本.

python desktop/scripts/billing_seed.py <账本目录> [--balance-dir <假余额接口的根目录>]
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from wm import ledger as L  # noqa: E402
from wm.config import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("billing_dir")
    ap.add_argument("--balance-dir")
    a = ap.parse_args()
    led = L.Ledger(Path(a.billing_dir), load_config()["billing"])
    rnd = random.Random(7)
    now = time.time()
    rows = 0
    for day in range(13, -1, -1):
        n = rnd.randint(3, 14) if day else 10
        for k in range(n):
            t = now - day * 86400 - rnd.uniform(0, 10 * 3600) if day else now - (n - k) * 420
            who = rnd.choice(["pet", "pet", "channels", "channels", "workbench"])
            model = "deepseek-v4-pro" if who == "workbench" and rnd.random() < 0.3 else "deepseek-flash"
            usage = {"hit": rnd.randint(4000, 40000), "miss": rnd.randint(300, 5000), "out": rnd.randint(80, 900)}
            with L.charging(rnd.choice(["qq", "bili"]) if who == "channels" else ""):  # 外部渠道的一半算 QQ、一半算 B站
                rows += led.record(who, "deepseek", model, usage, t=t) is not None
        if not day:
            for k in range(4):  # 本机模型: KV cache 复用
                led.record("pet", "local", "llama.cpp", {"cache_n": 1500 + k * 40, "prompt_n": 30 + k, "predicted_n": 40},
                           t=now - k * 300)
    # 按渠道的额度: 按今天真用了多少定上限, 截图里 QQ 快到了 (85%)、B站 用完了、桌宠 40%、其他不限
    used = {r["id"]: r["tokens"] for r in led.budget_state()["rows"]}
    led.set_budget({"unit": "tokens", "limits": {"qq": round(used["qq"] / 0.85), "bili": max(1, round(used["bili"] * 0.9)),
                                                 "pet": round(used["pet"] / 0.4), "total": round(used["total"] / 0.6)}})
    if a.balance_dir:
        f = Path(a.balance_dir) / "user" / "balance"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "108.52", "granted_balance": "6.00", "topped_up_balance": "102.52"}]}),
            encoding="utf-8")
    print(f"账本假记录 {rows} 条 -> {a.billing_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
