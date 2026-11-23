"""经济账本最小件 (`wm/world/economy.py`) 的单测: 复式 / 幂等 / 托管 / 对账 / 落盘重放.

这一层的价值全在"不许悄悄错": 借贷不平、断线重试双花、钱卡在托管里对不上, 都必须在测试里先炸一次,
否则到了世界里就是有人白拿了一件家具。
"""
from __future__ import annotations

import json

import pytest

from wm.world.economy import CENTRAL, DuplicateKey, Entry, Ledger, LedgerError, Shop


def test_double_entry_moves_money_and_keeps_sum_zero():
    led = Ledger()
    led.open_account("user:master")
    led.open_account(CENTRAL, "central")
    tx = led.post([Entry.of(CENTRAL, -100), Entry.of("user:master", 100)], idem_key="grant-1", memo="发行 100 点")
    assert tx.tx_id == "tx00000001"
    assert led.balance("user:master") == 100
    assert led.balance(CENTRAL) == -100  # 只有 central 允许负数
    assert sum(led.balances.values()) == 0
    assert led.reconcile()["ok"] is True


def test_unbalanced_post_is_refused_and_nothing_lands():
    led = Ledger()
    led.open_account("user:master")
    with pytest.raises(LedgerError, match="借贷不平"):
        led.post([Entry.of("user:master", 100)], idem_key="bad")
    assert led.txs == [] and led.balance("user:master") == 0


def test_money_must_be_integer():
    with pytest.raises(LedgerError, match="整数"):
        Entry.of("user:master", 1.5)  # type: ignore[arg-type]


def test_same_idem_key_twice_does_not_double_spend():
    led = Ledger()
    led.open_account("user:master")
    led.open_account("shop:shoes")
    led.post([Entry.of(CENTRAL, -100), Entry.of("user:master", 100)], idem_key="grant")
    led.post([Entry.of("user:master", -30), Entry.of("shop:shoes", 30)], idem_key="buy-1")
    again = led.post([Entry.of("user:master", -30), Entry.of("shop:shoes", 30)], idem_key="buy-1")
    assert again.tx_id == "tx00000002"  # 拿到的是第一笔, 不是新的一笔
    assert led.balance("user:master") == 70 and led.balance("shop:shoes") == 30
    assert len(led.txs) == 2


def test_same_key_with_different_entries_is_a_bug_not_a_retry():
    led = Ledger()
    led.open_account("user:master")
    led.post([Entry.of(CENTRAL, -10), Entry.of("user:master", 10)], idem_key="grant")
    led.post([Entry.of("user:master", -1), Entry.of("shop:a", 1)], idem_key="k")
    with pytest.raises(DuplicateKey):
        led.post([Entry.of("user:master", -2), Entry.of("shop:a", 2)], idem_key="k")


def test_no_overdraft_for_users():
    led = Ledger()
    led.open_account("user:master")
    with pytest.raises(LedgerError, match="余额不够"):
        led.post([Entry.of("user:master", -5), Entry.of("shop:x", 5)], idem_key="oops")
    assert led.balance("user:master") == 0


def test_escrow_hold_capture_is_atomic_and_leaves_no_residue():
    led = Ledger()
    led.open_account("user:master")
    led.post([Entry.of(CENTRAL, -50), Entry.of("user:master", 50)], idem_key="grant")
    led.hold("user:master", 30, idem_key="order-7", memo="买一把椅子")
    assert led.balance("user:master") == 20
    assert led.balance("escrow:order-7") == 30
    assert led.reconcile()["escrow_open"] == {"escrow:order-7": 30}  # 卡在中间的钱看得见
    led.capture(idem_key="order-7", to="shop:chair")
    assert led.balance("escrow:order-7") == 0
    assert led.balance("shop:chair") == 30
    report = led.reconcile()
    assert report["ok"] is True and report["escrow_open"] == {}


def test_escrow_release_refunds_the_buyer():
    led = Ledger()
    led.open_account("user:master")
    led.post([Entry.of(CENTRAL, -50), Entry.of("user:master", 50)], idem_key="grant")
    led.hold("user:master", 30, idem_key="order-8")
    led.release(idem_key="order-8", payer="user:master")
    assert led.balance("user:master") == 50
    assert led.balance("escrow:order-8") == 0


def test_capture_without_hold_is_refused():
    led = Ledger()
    with pytest.raises(LedgerError, match="没有冻结中的钱"):
        led.capture(idem_key="never-held", to="shop:x")


def test_file_replay_rebuilds_the_same_balances(tmp_path):
    path = tmp_path / "economy.jsonl"
    led = Ledger(path)
    led.open_account("user:master")
    led.post([Entry.of(CENTRAL, -100), Entry.of("user:master", 100)], idem_key="grant")
    led.hold("user:master", 40, idem_key="order-1")
    same = Ledger(path)
    assert same.balance("user:master") == 60 and same.balance("escrow:order-1") == 40
    assert len(same.txs) == 2 and same.reconcile()["ok"] is True
    # 重放之后再用同一个 key 记账, 还是幂等的
    again = same.post([Entry.of(CENTRAL, -100), Entry.of("user:master", 100)], idem_key="grant")
    assert again.tx_id == "tx00000001" and len(same.txs) == 2


def test_tampered_file_is_reported_not_ignored(tmp_path):
    path = tmp_path / "economy.jsonl"
    path.write_text(json.dumps({"key": "k", "entries": [["a", 5], ["b", -4]], "tx_id": "tx00000001"}) + "\n",
                    encoding="utf-8")
    led = Ledger(path)  # 加载不抛 (账是能读的), 但对账要炸出来
    report = led.reconcile()
    assert report["ok"] is False
    assert report["problems"][0]["why"] == "借贷不平"


def test_history_filters_by_account_and_shows_my_delta():
    led = Ledger()
    led.open_account("user:master")
    led.post([Entry.of(CENTRAL, -10), Entry.of("user:master", 10)], idem_key="a", memo="第一笔")
    led.post([Entry.of("user:master", -3), Entry.of("shop:tea", 3)], idem_key="b", memo="买茶")
    rows = led.history("user:master", limit=5)
    assert [r["memo"] for r in rows] == ["买茶", "第一笔"]
    assert [r["mine"] for r in rows] == [-3, 10]
    assert len(led.history(limit=1)) == 1


def _shop(with_money: int = 500) -> Shop:
    shop = Shop(Ledger())
    if with_money:
        shop.grant(with_money)
    return shop


def test_buy_moves_money_and_places_the_thing():
    shop = _shop()
    out = shop.buy("chair", idem_key="order-1")
    assert out["ok"] is True and out["price"] == 30 and out["balance"] == 470
    assert shop.placed == ["chair"]
    assert shop.ledger.balance("shop:plaza") == 30
    assert shop.ledger.reconcile()["ok"] is True
    assert shop.ledger.reconcile()["escrow_open"] == {}


def test_buy_twice_with_same_key_returns_first_receipt_and_charges_once():
    shop = _shop()
    first = shop.buy("chair", idem_key="order-1")
    again = shop.buy("chair", idem_key="order-1")
    assert again["balance"] == first["balance"] == 470  # 没有第二次扣钱
    assert shop.placed == ["chair"]  # 也没有摆第二把
    assert shop.sold == {"chair": 1}


def test_failed_placement_refunds_everything():
    shop = _shop()
    out = shop.buy("table", idem_key="order-2", place_failed=True)
    assert out["ok"] is False and out["refunded"] is True
    assert out["balance"] == 500 and shop.placed == []
    assert shop.ledger.reconcile()["ok"] is True and shop.ledger.reconcile()["escrow_open"] == {}


def test_buying_unknown_item_costs_nothing():
    shop = _shop()
    out = shop.buy("sofa", idem_key="order-3")
    assert out["ok"] is False and out["refunded"] is False
    assert shop.ledger.balance("user:master") == 500 and len(shop.ledger.txs) == 1  # 只有那笔发放


def test_buying_without_enough_points_is_refused_and_leaves_no_debt():
    shop = _shop(with_money=10)
    with pytest.raises(LedgerError, match="余额不够"):
        shop.buy("chair", idem_key="order-4")
    assert shop.ledger.balance("user:master") == 10 and shop.placed == []