"""账号保管箱 (wm/channels/vault.py): 加密、用途许可、使用记录、工作台守门. 全程用临时目录和假密码, 不碰真的 ~/.worldmodel/vault."""
import base64
import json
import os
import pickle
import time
from pathlib import Path

import pytest

from wm.agent import policy
from wm.agent.governance import BlacklistStore
from wm.channels import mailbox as M
from wm.channels import vault as V

PW = "Pa55w0rd-示例-ZXCV"


@pytest.fixture(autouse=True)
def _dpapi_off_windows(monkeypatch):
    if os.name != "nt":  # 不是 Windows 时换一个按"格子"异或的可逆替身 (Windows 上用真的 DPAPI)
        monkeypatch.setattr(V, "_dpapi", lambda data, encrypt, slot: bytes(b ^ (sum(slot.encode()) % 255 + 1) for b in data))


@pytest.fixture
def vault(tmp_path):
    return V.Vault(tmp_path / "vault")


def _files_text(home: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in home.rglob("*") if p.is_file())


def test_secret_is_encrypted_on_disk_and_never_in_the_public_view(vault, tmp_path):
    view = vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    assert PW not in _files_text(vault.home)
    assert PW not in json.dumps(view, ensure_ascii=False) and PW not in json.dumps(vault.accounts(), ensure_ascii=False)
    assert view["secrets"] == [{"name": "password", "label": "管理页密码", "set": True}]
    lease = vault.lease("snowluma", "qq_attach", by="test")
    secrets = lease.secrets
    assert lease.ok and lease.get("password") == PW
    assert PW not in repr(secrets) and PW not in str(secrets) and "password" in repr(secrets)
    lease.done(True, "ok")
    assert not lease.secrets  # 用完就清掉
    with pytest.raises(TypeError):
        pickle.dumps(V.Secrets(password=PW))


def test_ciphertext_is_bound_to_its_account_and_field(vault):
    vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    vault.put("other", {"secret": "another-secret"}, label="别的", aid="other-aaaaaa")
    src = json.loads((vault.home / "accounts" / "snowluma.json").read_text(encoding="utf-8"))
    dst_path = vault.home / "accounts" / "other-aaaaaa.json"
    dst = json.loads(dst_path.read_text(encoding="utf-8"))
    dst["secrets"]["secret"] = src["secrets"]["password"]  # 把 A 账号的密文拷进 B 账号
    dst_path.write_text(json.dumps(dst), encoding="utf-8")
    lease = vault.lease("other-aaaaaa", "x", by="test", explicit=True)
    assert not lease.ok and lease.why == "broken" and "解不开" in lease.text
    if os.name == "nt":  # 真 DPAPI 才有的: 换一个熵值照样解不开
        blob = base64.b64decode(src["secrets"]["password"])
        with pytest.raises(V.VaultError):
            V._dpapi(blob, False, "snowluma:other")


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 只在 Windows 上")
def test_other_stores_ciphertext_does_not_open_here_and_vice_versa(vault):
    mine = V._dpapi(b"hello", True, "snowluma:password")
    with pytest.raises(M.MailError):
        M._dpapi(mine, False)
    with pytest.raises(V.VaultError):
        V._dpapi(M._dpapi(b"hello", True), False, "snowluma:password")


def test_user_name_is_masked_and_only_the_program_can_read_it(vault):
    view = vault.put("other", {"secret": "tok"}, label="某网站小号", user="abc.def@qq.com")
    assert view["user"] == "a******@qq.com" and "abc.def" not in json.dumps(view) and "abc.def" not in _files_text(vault.home)
    assert vault.user_of(view["id"]) == "abc.def@qq.com"
    assert V.mask_user("13812345678") == "138****5678" and V.mask_user("") == "" and V.mask_user("x") == "x*"


def test_purposes_ask_once_allow_deny(vault):
    asked = []
    vault.on_want = lambda acct, purpose, by: asked.append((acct["id"], purpose["id"], by))
    vault.put("bilibili", {"cookie": "SESSDATA=a; bili_jct=b"})  # 没给用途: 都是"先问"
    lease = vault.lease("bilibili", "read", by="bili")
    assert not lease.ok and lease.why == "ask" and asked == [("bilibili", "read", "bili")]
    vault.lease("bilibili", "read", by="bili")  # 还在等点头: 不再弹第二张
    assert len(asked) == 1
    assert vault.get("bilibili")["purposes"][0]["waiting"] is True
    vault.set_purpose("bilibili", "read", "allow", by="card")
    assert vault.lease("bilibili", "read", by="bili").ok
    assert not vault.lease("bilibili", "write", by="bili").ok and asked[-1] == ("bilibili", "write", "bili")
    vault.set_purpose("bilibili", "write", "deny")
    n = len(asked)
    denied = vault.lease("bilibili", "write", by="bili")
    assert not denied.ok and denied.why == "deny" and len(asked) == n  # 说了不行就不再问
    vault.set_purpose("bilibili", "write", "ask")  # 改回"先问我": 下回要用时重新问
    vault.lease("bilibili", "write", by="bili")
    assert len(asked) == n + 1
    assert vault.lease("bilibili", "nope", by="x").why == "unknown"
    assert vault.lease("nobody", "read", by="x").why == "none"
    with pytest.raises(V.VaultError):
        vault.set_purpose("bilibili", "nope", "allow")
    with pytest.raises(V.VaultError):
        vault.set_purpose("bilibili", "read", "maybe")


def test_ask_again_after_a_long_while_and_failed_callback_does_not_break_use(vault):
    now = [1000.0]
    vault.clock = lambda: now[0]
    seen = []

    def boom(*a):
        seen.append(a)
        raise RuntimeError("卡片弹不出来")
    vault.on_want = boom
    vault.put("snowluma", {"password": PW})
    assert vault.lease("snowluma", "qq_attach", by="launcher").why == "ask"
    vault.lease("snowluma", "qq_attach", by="launcher")
    assert len(seen) == 1
    now[0] += V.WANT_AGAIN_S + 1  # 卡片没人理: 隔很久再弹一次
    vault.lease("snowluma", "qq_attach", by="launcher")
    assert len(seen) == 2


def test_explicit_use_is_a_nod_but_still_logged(vault):
    vault.put("bilibili", {"cookie": "SESSDATA=a; bili_jct=b"})
    lease = vault.lease("bilibili", "check", by="page", explicit=True)
    assert lease.ok
    lease.done(True, "还有效")
    rec = vault.log_tail(1)[0]
    assert rec["purpose"] == "check" and rec["by"] == "page" and rec["ok"] is True and "SESSDATA" not in json.dumps(rec)


def test_usage_log_has_no_secrets_and_is_trimmed(vault, monkeypatch):
    monkeypatch.setattr(V, "LOG_KEEP", 20)
    vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    for i in range(80):
        with vault.lease("snowluma", "qq_attach", by="launcher") as lease:
            lease.done(i % 2 == 0, f"第 {i} 次")
    recs = vault.log_tail(500)
    assert len(recs) <= 40 and recs[-1]["note"] == "第 79 次"
    assert PW not in _files_text(vault.home)
    assert vault.get("snowluma")["last_used"]["ok"] is False  # 最后一次是第 79 次 (奇数, 没成)


def test_lease_without_done_logs_a_failure_and_clears_secrets(vault):
    vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    with vault.lease("snowluma", "qq_attach", by="launcher") as lease:
        pass
    assert vault.log_tail(1)[0]["ok"] is False and "没说结果" in vault.log_tail(1)[0]["note"]
    assert not lease.secrets
    with pytest.raises(ZeroDivisionError):
        with vault.lease("snowluma", "qq_attach", by="launcher"):
            1 / 0
    assert "ZeroDivisionError" in vault.log_tail(1)[0]["note"]


def test_repeated_identical_records_can_be_quiet(vault):
    vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    for _ in range(5):
        vault.lease("snowluma", "qq_attach", by="launcher").done(True, "没有她的号", quiet_s=600)
    assert sum(1 for r in vault.log_tail(50) if r["note"] == "没有她的号") == 1


def test_put_rejects_bad_input(vault):
    with pytest.raises(V.VaultError, match="不认识"):
        vault.put("nowhere", {"x": "y"})
    with pytest.raises(V.VaultError, match="没有「cookie」"):
        vault.put("snowluma", {"cookie": "x"})
    for empty in ("", "\n", "\r\n"):
        with pytest.raises(V.VaultError, match="没填"):
            vault.put("snowluma", {"password": empty})
    with pytest.raises(V.VaultError, match="换行"):
        vault.put("snowluma", {"password": "ab\ncd"})
    with pytest.raises(V.VaultError, match="控制字符"):
        vault.put("snowluma", {"password": "ab\x00cd"})
    with pytest.raises(V.VaultError, match="太长"):
        vault.put("snowluma", {"password": "x" * 600})
    vault.put("bilibili", {"cookie": "c=" + "x" * 8000})  # cookie 能放到 8 KB
    assert not (vault.home / "accounts" / "snowluma.json").exists()


def test_at_most_max_accounts_and_ids_are_unique(vault, monkeypatch):
    monkeypatch.setattr(V, "MAX_ACCOUNTS", 3)
    ids = {vault.put("other", {"secret": f"s{i}"}, label=f"账号{i}")["id"] for i in range(3)}
    assert len(ids) == 3 and all(i.startswith("other-") for i in ids)
    with pytest.raises(V.VaultError, match="最多"):
        vault.put("other", {"secret": "s9"})
    one = sorted(ids)[0]
    vault.put("other", {"secret": "new"}, aid=one)  # 换已有的那个不算新增
    assert vault.lease(one, "x", by="t", explicit=True).get("secret") == "new"


def test_single_platform_resave_keeps_purposes_and_clears_flags(vault):
    vault.put("snowluma", {"password": "old-one"}, purposes={"qq_attach": True})
    first = vault.get("snowluma")
    vault.set_flags("snowluma", rejected_for=first["updated"], rejected_at=1.0)
    assert vault.get("snowluma")["rejected"] is True
    time.sleep(0.01)
    vault.put("snowluma", {"password": "new-one"})
    again = vault.get("snowluma")
    assert again["rejected"] is False and again["updated"] > first["updated"]
    assert again["purposes"][0]["state"] == "allow"  # 换密码不改许可
    assert vault.lease("snowluma", "qq_attach", by="t").get("password") == "new-one"


def test_forget_removes_secret_keeps_the_log(vault):
    vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})
    vault.lease("snowluma", "qq_attach", by="launcher").done(True, "挂了")
    assert vault.forget("snowluma") is True and vault.forget("snowluma") is False
    assert vault.get("snowluma") is None and vault.accounts() == []
    assert PW not in _files_text(vault.home) and any(r["note"] == "删了" for r in vault.log_tail(20))


def test_broken_files_are_reported_not_crashing(vault):
    vault.put("snowluma", {"password": PW})
    (vault.home / "accounts" / "snowluma.json").write_text("{坏掉的", encoding="utf-8")
    (vault.home / "accounts" / "x.json").write_text(json.dumps({"id": "y", "platform": "other"}), encoding="utf-8")
    rows = vault.accounts()
    assert {r["id"]: bool(r.get("broken")) for r in rows} == {"snowluma": True, "x": True}
    lease = vault.lease("snowluma", "qq_attach", by="launcher")
    assert not lease.ok and lease.why == "broken" and "重新填" in lease.text
    vault.put("snowluma", {"password": "fresh"})  # 坏掉的直接换新的
    assert vault.get("snowluma") is not None


def test_bad_account_ids_cannot_escape_the_folder(vault):
    for bad in ("../x", "a/b", "A", "", "x" * 40, "a.json"):
        lease = vault.lease(bad, "read", by="t")  # 用的人拿到的是"不行", 不是异常
        assert not lease.ok and lease.why == "broken"
        with pytest.raises(V.VaultError):
            vault.get(bad)
    with pytest.raises(V.VaultError):
        vault.put("other", {"secret": "s"}, aid="../evil")
    assert not list(vault.home.parent.rglob("evil*"))


def test_meta_only_keeps_whitelisted_non_secret_keys(vault):
    vault.put("bilibili", {"cookie": "SESSDATA=a; bili_jct=b"}, meta={"mid": "1", "uname": "名字", "cookie": "SESSDATA=leak", "x": 1})
    meta = vault.get("bilibili")["meta"]
    assert meta == {"mid": "1", "uname": "名字"} and "leak" not in _files_text(vault.home)
    vault.update_meta("bilibili", verified=True, evil="x")
    assert vault.get("bilibili")["meta"]["verified"] is True and "evil" not in vault.get("bilibili")["meta"]


# -- 工作台守门 -----------------------------------------------------------------------
def test_workbench_blacklist_blocks_the_vault_folder_and_api(tmp_path):
    home = tmp_path / "vault"
    home.mkdir()
    store = BlacklistStore(tmp_path / "agent", defaults=False)
    ok, why = V.protect(store, home)
    assert ok and "目录加进了" in why and V.protected(store, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    for tool, args in [("read_file", {"path": str(home / "accounts" / "snowluma.json")}), ("list_dir", {"path": str(home)}),
                       ("run_command", {"command": f'type "{home / "log.jsonl"}"'}),
                       ("run_command", {"command": "curl http://127.0.0.1:8790/api/vault?log=5"})]:
        assert store.check(tool, args, ws) is not None, (tool, args)
    assert V.protect(store, home)[1].count("已在保护名单里") == 2
    rule = next(r for r in store.list() if r["kind"] == "path")
    store.remove(rule["id"], by="user")  # 你自己删掉了: 不再加回去
    ok, why = V.protect(store, home)
    assert not ok and "删掉过" in why and not V.protected(store, home)
    assert V.protected(None, home) is False


def test_policy_guards_the_vault_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_home", lambda: tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / ".worldmodel" / "vault" / "accounts" / "snowluma.json"
    for mode in ("read", "workspace", "full"):
        assert policy.check_path("read_file", secret, mode, ws).how == "deny"
        assert policy.check_path("write_file", secret, mode, ws).how == "deny"
    for cmd in (rf'type "{secret}"', r"Get-Content $env:USERPROFILE\.worldmodel\vault\log.jsonl", r"cat ~\.worldmodel\vault\state.json"):
        assert policy.check_command(cmd, "full").how == "deny", cmd
    for harmless in (r"dir C:\work\vault", "echo vault", "python send_vault.py"):
        assert policy.check_command(harmless, "full").how == "auto", harmless
