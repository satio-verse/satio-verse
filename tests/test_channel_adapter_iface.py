"""渠道适配器接口和自带清单 (聊天2 S0): 纯测试, 不碰网络、真家目录、真 key、manager.

  - Inbound 里没有信任档的位置, 带 trust / is_owner / role 一类的名字直接抛错; Host 恰好三个口, HostPorts 没有公开属性;
  - 三份自带清单过 validate_plugin_manifest 和 policy_issues(…, "bundled"), 第三方来路同一份会被拦;
  - 和 tests/plugin_manifests/ 里的样例只差已知的几处 (微信 / QQ 的真清单照代码改了);
  - 适配器文件用 ast 扫: 不碰 dunder 属性、不导入 gc / inspect / ctypes / sys, 只许导入白名单里的 wm 模块.
"""
import ast
import copy
import json
from pathlib import Path

import pytest

from wm.agent import plugin_manifest as pm
from wm.channels import adapter as A
from wm.channels.adapters import ADAPTER_IDS

@pytest.fixture
def anyio_backend():
    return "asyncio"


ROOT = Path(A.__file__).resolve().parent
SAMPLES = Path(__file__).parent / "plugin_manifests"


def manifest(name):
    return json.loads((A.MANIFEST_DIR / f"{name}.json").read_text(encoding="utf-8"))


def sample(name):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ADAPTER_IDS)
def test_manifest_valid_and_bundled(name):
    m = manifest(name)
    assert not pm.has_errors(pm.validate_plugin_manifest(m))
    assert pm.policy_issues(m, "bundled") == []
    assert m["plugin"]["tier"] == "builtin" and m["id"] == name
    A.load_manifest(name)  # 加载入口也过


@pytest.mark.parametrize("name", ADAPTER_IDS)
def test_same_manifest_from_third_party_is_refused(name):
    codes = {i.code for i in pm.policy_issues(manifest(name), "third_party")}
    assert "E_BUILTIN_NOT_BUNDLED" in codes
    with pytest.raises(A.AdapterManifestError):
        A.load_manifest(name, "third_party")


def test_separate_permissions():
    assert pm.separate_permissions(manifest("qq-onebot")) == ["act_as_owner", "answer_cards"]
    assert pm.separate_permissions(manifest("weixin-ilink")) == ["act_as_owner", "answer_cards"]
    assert pm.separate_permissions(manifest("local-guest")) == []


def test_prefixes_and_actions_do_not_clash():
    ms = [manifest(n) for n in ADAPTER_IDS]
    for i, m in enumerate(ms):
        assert pm.find_conflicts(m, ms[:i] + ms[i + 1:]) == []


def test_trust_and_proactive_declarations():
    w = {n: manifest(n)["plugin"]["world"] for n in ADAPTER_IDS}
    assert (w["qq-onebot"]["trust_from"], w["qq-onebot"]["trust_max"], w["qq-onebot"]["proactive"]["mode"]) == \
        ("platform_id", "owner_remote", "free")
    assert (w["weixin-ilink"]["trust_from"], w["weixin-ilink"]["trust_max"], w["weixin-ilink"]["proactive"]["mode"]) == \
        ("paired_owner", "owner_remote", "reply_window")
    assert (w["local-guest"]["trust_from"], w["local-guest"]["trust_max"], w["local-guest"]["proactive"]["mode"]) == \
        ("local_loopback", "acquaintance", "never")
    assert w["weixin-ilink"]["caps"]["group"] is False and "reply_window_s" not in w["weixin-ilink"]["proactive"]


def _strip(m, paths):
    m = copy.deepcopy(m)
    for path in paths:
        node = m
        parts = path.split(".")
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node[p]
        last = parts[-1]
        if isinstance(node, list):
            node.pop(int(last))
        else:
            node.pop(last)
    return m


def test_real_manifests_differ_from_samples_only_where_the_code_says():
    """样例是讲格式用的, 真清单照代码实情改了几处; 别的字段一个字不许差."""
    assert manifest("local-guest") == sample("local-guest")
    q_paths = ["description", "plugin.permissions.network", "plugin.world.events", "plugin.world.caps.attachments"]
    assert _strip(manifest("qq-onebot"), q_paths) == _strip(sample("qq-onebot"), q_paths)
    assert manifest("qq-onebot")["plugin"]["permissions"]["network"] == ["qq.com", "qq.com.cn", "qpic.cn", "qlogo.cn"]
    assert manifest("qq-onebot")["plugin"]["world"]["caps"]["attachments"] == ["image", "voice", "file", "video"]
    w_paths = ["plugin.world.events", "plugin.world.caps.attachments"]
    assert _strip(manifest("weixin-ilink"), w_paths + ["plugin.world.actions.1"]) == _strip(sample("weixin-ilink"), w_paths)
    wx = manifest("weixin-ilink")["plugin"]["world"]
    assert wx["caps"]["attachments"] == [] and [a["name"] for a in wx["actions"]] == ["wx_send", "wx_send_image"]
    assert "QQ 登录二维码" in wx["actions"][1]["why"]


# ---------------------------------------------------------------------------
# Inbound 和三个口
# ---------------------------------------------------------------------------
GOOD = {"conv": {"kind": "private", "id": 7}, "sender": {"id": "7", "nick": "某人"}, "mid": "m1",
        "segments": [{"kind": "text", "data": {"text": "你好"}}]}


def test_inbound_from_dict_ok():
    m = A.Inbound.from_dict(GOOD)
    assert m.conv.kind == "private" and m.segments[0].data["text"] == "你好" and m.reply_to is None


@pytest.mark.parametrize("name", sorted(A.FORBIDDEN_NAMES))
def test_inbound_refuses_trust_names(name):
    with pytest.raises(A.InboundError):
        A.Inbound.from_dict({**GOOD, name: "owner_remote"})
    with pytest.raises(A.InboundError):
        A.Inbound.from_dict({**GOOD, "segments": [{"kind": "text", "data": {name: True}}]})


def test_inbound_refuses_unknown_keys_and_bad_shapes():
    with pytest.raises(A.InboundError):
        A.Inbound.from_dict({**GOOD, "extra": 1})
    with pytest.raises(A.InboundError):
        A.Inbound.from_dict({**GOOD, "conv": {"kind": "weixin"}})
    with pytest.raises(A.InboundError):
        A.Inbound.from_dict({**GOOD, "segments": [{"kind": "script", "data": {}}]})
    with pytest.raises(TypeError):
        A.Inbound(conv=A.ConvRef("private", 1), trust="owner_remote")  # 数据类本身也没有这个参数


def test_host_has_exactly_three_public_methods_and_ports_no_public_attrs():
    def public(obj):
        return {n for n in dir(obj) if not n.startswith("_")}
    assert public(A.Host) == {"inbound", "state", "notice"}
    ports = A.HostPorts(None, None, None)
    assert public(ports) == {"inbound", "state", "notice"}
    assert not hasattr(ports, "__dict__")  # __slots__: 没有地方挂别的东西


def _collect():
    got = {"in": [], "st": [], "nt": []}

    async def i(m):
        got["in"].append(m)

    async def s(k, info):
        got["st"].append((k, info))

    async def n(k, t, extra):
        got["nt"].append((k, t, extra))
    return got, A.HostPorts(i, s, n)


@pytest.mark.anyio
async def test_ports_check_their_arguments():
    got, ports = _collect()
    await ports.inbound(A.Inbound.from_dict(GOOD))
    with pytest.raises(A.InboundError):
        await ports.inbound(GOOD)  # 不是 Inbound
    await ports.state("online", {"nick": "x"})
    with pytest.raises(A.AdapterError):
        await ports.state("owner", {})
    await ports.notice("muted", "被禁言了", group_id=5, token="secret", user_id=1)
    assert len(got["in"]) == 1 and got["st"] == [("online", {"nick": "x"})]
    assert got["nt"] == [("muted", "被禁言了", {"group_id": 5, "user_id": 1})]  # token 被丢掉


def test_send_result_reason_is_one_of_the_few():
    assert A.SendResult(True, mid="1").ok
    with pytest.raises(A.AdapterError):
        A.SendResult(False, reason="whatever")


# ---------------------------------------------------------------------------
# 基类的 proactive 检查 (用一个只在测试里的假适配器)
# ---------------------------------------------------------------------------
class _Probe(A.ChannelAdapter):
    id = "local-guest"
    SEND_ACTION = "guest_reply"
    IMPL = {"guest_reply": "_send"}

    def __init__(self, mode, *, reply=False, window=False):
        super().__init__()
        self.proactive = {"mode": mode}
        self._reply, self._window, self.sent = reply, window, 0

    def _reply_ok(self, conv, out):
        return self._reply

    def _window_open(self):
        return self._window

    async def _send(self, conv, out):
        self.sent += 1
        return A.SendResult(True, mid="1")


@pytest.mark.anyio
@pytest.mark.parametrize("mode,kw,ok", [("never", {}, False), ("never", {"reply": True}, True),
                                        ("reply_window", {}, False), ("reply_window", {"window": True}, True),
                                        ("free", {}, True), ("quota", {}, False)])
async def test_base_send_enforces_proactive(mode, kw, ok):
    p = _Probe(mode, **kw)
    r = await p.send(A.ConvRef("local"), A.Out(text="hi"))
    assert r.ok is ok and p.sent == int(ok)
    if not ok:
        assert r.reason in ("window_closed", "quota")


@pytest.mark.anyio
async def test_undeclared_fetch_is_refused():
    with pytest.raises(A.AdapterError):
        await _Probe("free").fetch("image", "x")


# ---------------------------------------------------------------------------
# 适配器文件的 ast 扫描: 拿到 manager 只能靠内省, 内省被扫掉
# ---------------------------------------------------------------------------
ADAPTER_FILES = [ROOT / "adapter.py", *sorted((ROOT / "adapters").glob("*.py"))]
BANNED_IMPORTS = {"gc", "inspect", "ctypes", "sys", "importlib", "builtins", "types"}
BANNED_CALLS = {"vars", "dir", "globals", "locals", "eval", "exec", "compile", "__import__", "setattr", "delattr"}
OK_DUNDER_ATTRS = {"__init__", "__post_init__"}
ALLOWED_WM = {"wm.agent.plugin_manifest", "wm.channels.adapter", "wm.channels.onebot", "wm.channels.weixin",
              "wm.channels.media", "wm.channels.outbound"}


def _module_of(path, node):
    pkg = "wm.channels" if path.parent == ROOT else "wm.channels.adapters"
    if node.level == 0:
        return node.module or ""
    parts = pkg.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def violations(path, tree):
    """一份语法树里违反规则的地方 (文字说明). 空 = 干净."""
    out = []
    for node in ast.walk(tree):
        at = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__") \
                and node.attr not in OK_DUNDER_ATTRS:
            out.append(f"{at}: 访问了 {node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in BANNED_CALLS:
                out.append(f"{at}: 调了 {name}")
            if name == "getattr" and not (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                                          and isinstance(node.args[1].value, str)
                                          and not node.args[1].value.startswith("__")):
                out.append(f"{at}: getattr 取的不是字面量名字, 或是 dunder")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_IMPORTS:
                    out.append(f"{at}: 导入了 {a.name}")
                if a.name.split(".")[0] == "wm":
                    out.append(f"{at}: wm 要用相对导入并且在白名单里")
        if isinstance(node, ast.ImportFrom):
            mod = _module_of(path, node)
            if mod.split(".")[0] in BANNED_IMPORTS:
                out.append(f"{at}: 导入了 {mod}")
            if mod.split(".")[0] == "wm":
                names = {f"{mod}.{a.name}" for a in node.names}
                if mod not in ALLOWED_WM and not names <= ALLOWED_WM:
                    out.append(f"{at}: 导入了不在白名单里的 {mod} ({', '.join(a.name for a in node.names)})")
    return out


@pytest.mark.parametrize("path", ADAPTER_FILES, ids=lambda p: p.name)
def test_adapter_source_has_no_introspection_and_only_allowed_imports(path):
    assert violations(path, ast.parse(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("src", [
    "x = host.inbound.__self__", "y = f.__closure__", "import gc", "import sys", "from inspect import stack",
    "z = getattr(h, '__dict__')", "z = getattr(h, name)", "w = vars(h)", "from ..manager import ChannelManager",
    "from .. import guest", "from ...memory import Memory", "import wm.channels.manager"])
def test_the_scan_has_teeth(src):
    assert violations(ROOT / "adapters" / "x.py", ast.parse(src)) != []


@pytest.mark.parametrize("src", ["from ..onebot import OneBotClient", "from .. import adapter", "from ..adapter import Inbound",
                                 "import asyncio", "super().__init__()", "x = getattr(h, 'online')"])
def test_the_scan_lets_the_good_ones_through(src):
    assert violations(ROOT / "adapters" / "x.py", ast.parse(src)) == []
