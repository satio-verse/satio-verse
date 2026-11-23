"""世界里的经济账本最小件 (Hemifuture §16.3 第三个闭环, 2026-11-20): 复式记账 + 幂等键 + 托管 + 对账.

三件事, 一件都不许省:

- **复式**: 一笔交易写成若干条分录, 每条 `(账户, 增减)`, 加起来必须是 0. 钱不会凭空出现,
  也不会凭空消失 —— 违反这一条直接抛错, 不写进账.
- **幂等**: 每个写操作都要带 `idem_key` (网络重试 / 断线重连会拿同一个 key 再发一次).
  同一个 key 第二次来, 返回第一笔的记录, 不重复记账 —— 这就是"断线重试不双花".
- **托管**: 买东西分两步 (先冻结、再交付). `hold` 把钱从买家挪进 `escrow:<id>` 这个中间账户,
  `capture` 再挪给卖家, `release` 退回买家. 中间这一步让"扣了钱但东西没到"可回滚, 也让对账能看见
  卡在中间的钱.

金额一律是整数 (HemiPoint, 1 点 = 1 分), 不用浮点: 浮点加一千次就少一分, 对账会天天不平.
账户余额默认不许为负 (用户账户不能透支); 世界侧的"央行"账户可以, 那是发行/回收的地方.

账写在一个 JSONL 文件里 (一行一笔), 加载时重放: 每行都重新检查"分录和为 0"和"幂等键没重复",
坏行直接报出来, 不静默跳过 —— 账本最怕的就是悄悄少一行.

一句话: 这是让"她买了一双鞋"变成可对账事实的那一层; 没有它, 世界里的东西就只是演出来的.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

CENTRAL = "central"  # 世界上帝账户: 发行 / 回收积分的地方, 唯一允许余额为负的账户


class LedgerError(Exception):
    """账本拒绝这笔交易 (不平衡 / 余额不够 / 账户不存在)."""


class DuplicateKey(LedgerError):
    """同一个 idem_key 又被拿来记一笔不同的交易 (说明上游把两个动作用了同一个 key)."""


@dataclass(frozen=True)
class Entry:
    """一条分录: 哪个账户, 增减多少 (整数, 可正可负)."""

    account: str
    delta: int

    @staticmethod
    def of(account: str, delta: int) -> "Entry":
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise LedgerError(f"金额必须是整数 (HemiPoint 没有小数), 收到 {delta!r}")
        return Entry(str(account), int(delta))


@dataclass
class Tx:
    """一笔交易: 一组和为 0 的分录 + 幂等键 + 人话备注 + 时间."""

    key: str
    entries: list[Entry]
    memo: str = ""
    at: float = field(default_factory=time.time)
    tx_id: str = ""

    def as_dict(self) -> dict:
        return {"tx_id": self.tx_id, "key": self.key, "memo": self.memo, "at": self.at,
                "entries": [[e.account, e.delta] for e in self.entries]}

    @staticmethod
    def from_dict(raw: dict) -> "Tx":
        return Tx(key=str(raw["key"]), memo=str(raw.get("memo") or ""),
                  at=float(raw.get("at") or 0.0), tx_id=str(raw.get("tx_id") or ""),
                  entries=[Entry.of(str(a), int(d)) for a, d in raw.get("entries") or []])


class Ledger:
    """一本账: 账户余额 + 交易流水 + 托管. 可选落盘, 可选内存 (测试用)."""

    def __init__(self, path: str | Path | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self.balances: dict[str, int] = {}
        self.kinds: dict[str, str] = {}  # 账户 -> 种类 (user / world / escrow / central)
        self.txs: list[Tx] = []
        self.keys: dict[str, Tx] = {}  # 幂等键 -> 已经记下的那一笔
        self._seq = 0
        self.open_account(CENTRAL, "central")  # 发行 / 回收的地方, 一开始就得在
        if self.path and self.path.exists():
            self._replay()

    # -- 账户 -------------------------------------------------------------------------
    def open_account(self, name: str, kind: str = "user") -> str:
        """开一个账户 (已经有就什么都不做). kind 决定它能不能透支."""
        name = str(name).strip()
        if not name:
            raise LedgerError("账户名不能为空")
        if name in self.balances:
            if self.kinds.get(name) not in (None, kind):
                raise LedgerError(f"账户 {name} 已经是 {self.kinds[name]}, 不能改成 {kind}")
            return name
        self.balances[name] = 0
        self.kinds[name] = kind
        return name

    def balance(self, account: str) -> int:
        return self.balances.get(str(account), 0)

    @staticmethod
    def kind_for(name: str) -> str:
        """按命名猜账户种类: central / escrow:<key> / user:<谁> / 其余算世界侧 (店铺、房间)."""
        name = str(name)
        if name == CENTRAL:
            return "central"
        if name.startswith("escrow:"):
            return "escrow"
        if name.startswith("user:"):
            return "user"
        return "world"

    def accounts(self, kind: str | None = None) -> dict[str, int]:
        return {a: b for a, b in sorted(self.balances.items()) if kind is None or self.kinds.get(a) == kind}

    # -- 记账 -------------------------------------------------------------------------
    def post(self, entries: Sequence[Entry | tuple[str, int]], *, idem_key: str, memo: str = "") -> Tx:
        """记一笔. 分录加起来不为 0 就抛错; 同一个 idem_key 再发一次返回第一笔 (不重复记)."""
        key = str(idem_key or "").strip()
        if not key:
            raise LedgerError("每笔交易都要带 idem_key (没有它, 断线重试就会双花)")
        items = [e if isinstance(e, Entry) else Entry.of(*e) for e in entries]
        if not items:
            raise LedgerError("空交易")
        total = sum(e.delta for e in items)
        if total != 0:
            raise LedgerError(f"借贷不平: 分录加起来是 {total}, 必须是 0")
        # 先做一遍余额检查, 全部通过才落地 (不留半笔账)
        after = dict(self.balances)
        for e in items:
            if e.account not in after:
                if e.delta == 0:
                    continue
                self.open_account(e.account, self.kind_for(e.account))  # 没见过的账户按名字猜种类
                after[e.account] = 0
            after[e.account] = after.get(e.account, 0) + e.delta
        for account, value in after.items():
            if value < 0 and self.kinds.get(account) not in ("central", "world"):
                raise LedgerError(f"余额不够: {account} 会变成 {value}")
        old = self.keys.get(key)
        if old is not None:
            if old.entries != items:
                raise DuplicateKey(f"幂等键 {key} 已经记过一笔不同的交易 ({old.memo or '无备注'})")
            return old
        self._seq += 1
        tx = Tx(key=key, entries=items, memo=memo, at=self.clock(), tx_id=f"tx{self._seq:08d}")
        self.balances = after
        self.txs.append(tx)
        self.keys[key] = tx
        self._append(tx)
        return tx

    def hold(self, payer: str, amount: int, *, idem_key: str, memo: str = "") -> Tx:
        """冻结: 买家的钱挪进 escrow:<key> 这个中间账户 (东西还没交付)."""
        if amount <= 0:
            raise LedgerError("冻结金额必须为正")
        escrow = f"escrow:{idem_key}"
        self.open_account(escrow, "escrow")
        return self.post([Entry.of(payer, -amount), Entry.of(escrow, amount)], idem_key=idem_key,
                         memo=memo or f"冻结 {amount} 点")

    def capture(self, *, idem_key: str, to: str, memo: str = "") -> Tx:
        """交付: 把冻结的那笔钱给卖家 (卖家拿钱, 中间账户清零)."""
        held = self._escrow_amount(idem_key)
        self.open_account(to, "world")
        return self.post([Entry.of(f"escrow:{idem_key}", -held), Entry.of(to, held)],
                         idem_key=f"{idem_key}:capture", memo=memo or f"交付 {held} 点给 {to}")

    def release(self, *, idem_key: str, payer: str, memo: str = "") -> Tx:
        """退回: 东西没交付, 把冻结的钱还给买家."""
        held = self._escrow_amount(idem_key)
        return self.post([Entry.of(f"escrow:{idem_key}", -held), Entry.of(payer, held)],
                         idem_key=f"{idem_key}:release", memo=memo or f"退回 {held} 点给 {payer}")

    def _escrow_amount(self, idem_key: str) -> int:
        escrow = f"escrow:{idem_key}"
        held = self.balance(escrow)
        if held <= 0:
            raise LedgerError(f"没有冻结中的钱: {escrow} 余额 {held}")
        return held

    # -- 对账 -------------------------------------------------------------------------
    def reconcile(self) -> dict:
        """对账: 每笔和为 0、余额等于流水累加、没有负数余额. 任何一条不成立就是不平, 要人来看."""
        by_account: dict[str, int] = {}
        bad: list[dict] = []
        for tx in self.txs:
            total = sum(e.delta for e in tx.entries)
            if total != 0:
                bad.append({"tx_id": tx.tx_id, "why": "借贷不平", "sum": total})
            for e in tx.entries:
                by_account[e.account] = by_account.get(e.account, 0) + e.delta
        for account, value in sorted(by_account.items()):
            if self.balances.get(account, 0) != value:
                bad.append({"account": account, "why": "余额与流水对不上",
                            "balance": self.balances.get(account, 0), "replayed": value})
        for account, value in sorted(self.balances.items()):
            if value < 0 and self.kinds.get(account) not in ("central", "world"):
                bad.append({"account": account, "why": "负数余额", "balance": value})
        escrow = {a: b for a, b in self.balances.items() if a.startswith("escrow:") and b}
        return {"ok": not bad, "tx": len(self.txs), "accounts": len(self.balances),
                "total_points": sum(self.balances.values()),  # 复式账里这个恒等于 0 (含 central)
                "escrow_open": escrow, "problems": bad}

    # -- 落盘 -------------------------------------------------------------------------
    def _append(self, tx: Tx) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(tx.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def _replay(self) -> None:
        assert self.path is not None
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                tx = Tx.from_dict(raw)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise LedgerError(f"{self.path}:{n} 不是一笔能读的账 ({exc})") from exc
            for e in tx.entries:
                if e.account not in self.balances:
                    self.balances[e.account] = 0
                    self.kinds.setdefault(e.account, "escrow" if e.account.startswith("escrow:") else "world")
                self.balances[e.account] += e.delta
            if tx.key in self.keys and self.keys[tx.key].as_dict() != tx.as_dict():
                raise LedgerError(f"{self.path}:{n} 幂等键 {tx.key} 重复且内容不同")
            self.keys[tx.key] = tx
            self.txs.append(tx)
            self._seq = max(self._seq, int(tx.tx_id[2:]) if tx.tx_id[2:].isdigit() else self._seq)

    # -- 给人看 -----------------------------------------------------------------------
    def history(self, account: str | None = None, limit: int = 20) -> list[dict]:
        rows: Iterable[Tx] = reversed(self.txs) if account is None else (
            tx for tx in reversed(self.txs) if any(e.account == account for e in tx.entries))
        out = []
        for tx in rows:
            if account is None:
                out.append(tx.as_dict())
            else:
                mine = sum(e.delta for e in tx.entries if e.account == account)
                out.append({**tx.as_dict(), "mine": mine})
            if len(out) >= limit:
                break
        return out


@dataclass
class Shop:
    """世界侧的商店: 买东西 = 冻结 → 摆放 → 交付 (三步里任何一步没走完, 钱都退回去).

    Hemifuture §16.3 第三个闭环的验收就是这一条:
      扣款 / 发货 / 摆放**原子** —— 摆放失败必须全额退回, 不能出现"钱扣了东西没到手";
      断线重试**不双花** —— 调用方拿同一个 idem_key 再发一次, 返回第一笔的回执, 不再扣钱.

    `placed` 是这个世界现在摆着的东西 (由世界侧持有); `buy()` 返回的回执里带 `balance`,
    所以"她买了之后还剩多少点"是从账本算出来的, 不是编的.
    """

    ledger: Ledger
    catalog: dict[str, int] = field(default_factory=lambda: {"chair": 30, "table": 50, "plant": 12})
    payer: str = "user:master"
    seller: str = "shop:plaza"
    placed: list[str] = field(default_factory=list)
    sold: dict[str, int] = field(default_factory=dict)
    receipts: dict[str, dict] = field(default_factory=dict)  # idem_key -> 第一次的回执 (重试直接给同一份)

    def grant(self, amount: int = 500, *, idem_key: str = "grant:demo") -> Tx:
        """给买家发一点积分 (世界里叫首发/HemiPoint). 只为了能测通, 真实世界由运营发放."""
        return self.ledger.post([Entry.of(CENTRAL, -amount), Entry.of(self.payer, amount)],
                                idem_key=idem_key, memo=f"发给 {self.payer} {amount} 点")

    def price(self, item: str) -> int:
        return int(self.catalog[str(item)])

    def buy(self, item: str, *, idem_key: str, place_failed: bool = False, memo: str = "") -> dict:
        """买一件东西. 摆放失败 (`place_failed=True`) 会全额退回, 回执里 `ok=False`、`refunded=True`."""
        item = str(item or "").strip()
        key = str(idem_key or "").strip()
        if key and key in self.receipts:  # 断线重试: 同一份回执, 不再扣钱、不再摆一件
            return dict(self.receipts[key])
        if item not in self.catalog:
            return {"ok": False, "item": item, "why": f"这个世界没有「{item}」这件东西",
                    "balance": self.ledger.balance(self.payer), "refunded": False}
        price = self.price(item)
        hold = self.ledger.hold(self.payer, price, idem_key=key, memo=memo or f"买{item} {price} 点")
        if place_failed:
            self.ledger.release(idem_key=key, payer=self.payer, memo=f"{item}没摆成, 退回 {price} 点")
            out = {"ok": False, "item": item, "price": price, "why": "摆放失败", "refunded": True,
                   "balance": self.ledger.balance(self.payer), "tx": [hold.tx_id]}
            self.receipts[key] = out
            return out
        cap = self.ledger.capture(idem_key=key, to=self.seller, memo=f"交付{item} {price} 点")
        self.placed.append(item)
        self.sold[item] = self.sold.get(item, 0) + 1
        out = {"ok": True, "item": item, "price": price, "balance": self.ledger.balance(self.payer),
               "placed": list(self.placed), "tx": [hold.tx_id, cap.tx_id], "refunded": False}
        self.receipts[key] = out
        return dict(out)

