"""守门名单的路径 (2026-11-07 底层对标 P0-1): 打包应用 (Claude 桌面版这类 MSIX) 里的进程看 %APPDATA% 被重定向到
…\\Packages\\<包>\\LocalCache\\Roaming, 名单初始化时把桌宠设置和 token 的规则记成了这种路径, 真正的文件反而没挡住.
现在比较前一律换回真正的位置, 第 2 版迁移把记错的规则改写成真路径 (记审计), 新库多两条默认保护 (钩子配置、全局说明).
全在临时目录, 路径是编的."""
import json

from wm.agent import policy as P
from wm.agent.governance import DEFAULT_RULES_VERSION, BlacklistStore

HOME = "C:\\Users\\tester"
VIRT = HOME + "\\AppData\\Local\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\WorldModel\\pet-settings.json"
REAL = HOME + "\\AppData\\Roaming\\WorldModel\\pet-settings.json"


def test_unvirtualize_maps_redirected_paths_only():
    assert P.unvirtualize(VIRT) == REAL
    assert P.unvirtualize(VIRT.lower()).lower() == REAL.lower()  # 大小写不论
    local = HOME + "\\AppData\\Local\\Packages\\X_1\\LocalCache\\Local\\Google\\Chrome\\User Data"
    assert P.unvirtualize(local) == HOME + "\\AppData\\Local\\Google\\Chrome\\User Data"
    assert P.unvirtualize(HOME + "\\AppData\\Local\\Packages\\X_1\\LocalCache\\Roaming") == HOME + "\\AppData\\Roaming"
    for same in (REAL, "D:\\proj\\a.txt", HOME + "\\AppData\\Local\\Packages\\X_1\\Settings\\a.dat", ""):
        assert P.unvirtualize(same) == same


def test_inside_compares_real_locations_both_ways():
    assert P.inside(REAL, VIRT) and P.inside(VIRT, REAL)
    folder_virt = VIRT.rsplit("\\", 1)[0]
    assert P.inside(REAL, folder_virt) and not P.inside(HOME + "\\AppData\\Roaming\\Other\\x", folder_virt)


def _store_with_rule(tmp_path, value, version):
    store = BlacklistStore(tmp_path / "agent")
    state = json.loads(store.path.read_text("utf-8"))
    state["rules"].append({**store._new({"kind": "path", "value": REAL, "reason": "保护本机凭据"}, "user", "测试", origin="default"),
                           "value": value})
    state["default_rules_version"] = version
    store.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return store


def test_redirected_rule_blocks_the_real_file_even_without_migration(tmp_path):
    _store_with_rule(tmp_path, VIRT, DEFAULT_RULES_VERSION)  # 版本已是最新: 不迁移, 读的时候就换回真位置 (文件不动)
    store = BlacklistStore(tmp_path / "agent")
    saved = json.loads(store.path.read_text("utf-8"))["rules"]
    assert any(r["value"] == VIRT for r in saved) and any(r["value"] == REAL for r in store.list())
    ws = tmp_path / "ws"
    assert store.check("read_file", {"path": REAL}, ws).how == "deny"
    assert store.check("write_file", {"path": REAL}, ws).how == "deny"
    assert store.check("run_command", {"command": f"Get-Content '{REAL}'", "workdir": str(ws)}, ws).how == "deny"
    assert store.check("read_file", {"path": HOME + "\\AppData\\Roaming\\WorldModel\\skills\\a\\SKILL.md"}, ws) is None


def test_v2_migration_rewrites_redirected_paths_and_audits(tmp_path):
    _store_with_rule(tmp_path, VIRT, 1)
    store = BlacklistStore(tmp_path / "agent")
    values = [r["value"] for r in store.list()]
    assert VIRT not in values and REAL in values
    audit = next(e for e in store.audit() if e["action"] == "migrate_defaults")
    assert audit["fixed"] == [{"id": audit["fixed"][0]["id"], "from": VIRT, "to": REAL}]
    assert json.loads(store.path.read_text("utf-8"))["default_rules_version"] == DEFAULT_RULES_VERSION
    before = store.path.read_bytes()
    BlacklistStore(tmp_path / "agent")  # 只迁移一次
    assert store.path.read_bytes() == before


def test_fresh_store_protects_hooks_and_global_instructions(tmp_path):
    store = BlacklistStore(tmp_path / "agent")
    ws = tmp_path / "ws"
    hooks, agents = str(store.home / "hooks.json"), str(store.home / "AGENTS.md")
    assert store.check("write_file", {"path": hooks}, ws).how == "deny"
    assert store.check("read_file", {"path": hooks}, ws).how == "deny"
    assert store.check("write_file", {"path": agents}, ws).how == "deny"
    assert store.check("read_file", {"path": agents}, ws) is None  # 全局说明她可以读, 不能改
    assert store.check("run_command", {"command": f"Set-Content '{hooks}' x", "workdir": str(ws)}, ws).how == "deny"


def test_new_path_rules_are_recorded_at_the_real_location(tmp_path):
    store = BlacklistStore(tmp_path / "agent", defaults=False)
    rule = store.add({"kind": "path", "value": VIRT})
    assert rule["value"].lower() == REAL.lower()


def test_reads_reuse_checked_rules_until_the_file_changes(tmp_path, monkeypatch):
    """2026-11-08 全面检查: 每一步拼系统提示、判只读都要读名单, 以前每次都把每条规则重新核对 (路径要 resolve),
    多智能体子任务那组测试因此超时. 内容没变就用上次核对过的结果; 内容一变 (哪怕长度一样) 马上重核; 到点也重核."""
    from wm.agent import governance as G

    store = BlacklistStore(tmp_path / "agent")
    calls = []
    real = G._validate
    monkeypatch.setattr(G, "_validate", lambda rule, **kw: calls.append(1) or real(rule, **kw))
    first = store.list()
    checked = len(calls)
    assert checked == len(first) > 0                 # 第一次: 每条都核对
    for _ in range(5):
        assert store.list() == first                 # 内容没变: 不再核对
    assert len(calls) == checked
    mine = store.list()
    mine[0]["value"] = "调用方改了自己那份"
    assert store.list() == first                     # 给出去的是新的一份, 改了不影响缓存

    state = json.loads(store.path.read_text(encoding="utf-8"))
    old = state["rules"][0]["reason"]
    new = ("甲" if old[0] != "甲" else "乙") + old[1:]
    assert len(new.encode("utf-8")) == len(old.encode("utf-8"))
    state["rules"][0]["reason"] = new
    store.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    assert store.list()[0]["reason"] == new          # 外面改了文件, 长度一样也认得出
    assert len(calls) == 2 * checked

    later = G.time.monotonic() + G.READ_CACHE_S + 1
    monkeypatch.setattr(G.time, "monotonic", lambda: later)
    store.list()
    assert len(calls) == 3 * checked                 # 到点整份重核一次

    store.path.write_text("{", encoding="utf-8")     # 坏了照样停下, 不拿缓存顶
    try:
        store.list()
    except ValueError:
        pass
    else:
        raise AssertionError("名单坏了却没报错")
