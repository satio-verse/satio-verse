"""她自己写的 MCP: 骨架 (wm/selfmcp_kit.py) 和规范检查、登记 (wm/selfmcp.py). 不联网.

- 合规范的 (只用允许的标准库和骨架、只读、域名写清楚) -> 她可以自己装 (tier auto), 登记进名单, 文件带 SHA-256;
- 偷偷 open / import os / 用下划线属性 / 访问没写的域名 -> 不合规范 (ask, 写原因);
- 碰到 cookie、token、本机端口这类字眼, 或者要 key -> 敏感 (ask);
- 骨架: 只许 GET manifest 里的 https 域名, 存东西只在自己的 data/ 里.
"""
import json
import shutil
from pathlib import Path

import pytest

from wm import selfmcp as S

GOOD = '''from selfmcp_kit import Server, http_get_json, load_json, save_json

srv = Server("moon", "0.1.0")


@srv.tool("phase", "今天的月相 (算的, 不联网)", {"day": {"type": "integer", "description": "这个月的第几天"}}, required=["day"])
def phase(day: int) -> str:
    seen = load_json("seen", 0) + 1
    save_json("seen", seen)
    return ["新月", "上弦月", "满月", "下弦月"][(day // 8) % 4]


if __name__ == "__main__":
    srv.run()
'''


def make(tmp_path: Path, src: str = GOOD, **manifest) -> Path:
    d = tmp_path / "mcp"
    d.mkdir()
    m = {"id": "moon", "name": "看月相", "purpose": "告诉她今天大概是什么月相", "version": "0.1.0",
         "network": ["api.example.com"], "tools": [{"name": "phase", "description": "今天的月相"}], **manifest}
    (d / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    (d / "server.py").write_text(src, encoding="utf-8")
    return d


def test_good_mcp_is_auto_and_installs(tmp_path):
    d = make(tmp_path)
    r = S.check(d)
    assert r == {**r, "ok": True, "tier": "auto", "problems": [], "sensitive": [], "tools": ["phase"]}
    home = tmp_path / "home"
    e = S.install(d, "auto", home)
    assert e["id"] == "self-moon" and e["by"] == "auto" and e["enabled"] and e["tools"] == ["phase"]
    dst = Path(e["dir"])
    assert (dst / "selfmcp_kit.py").read_bytes() == S.KIT.read_bytes()  # 骨架用项目里那份
    assert e["sha256"]["server.py"] == S._sha(dst / "server.py")
    assert [s["id"] for s in S.load(home)["servers"]] == ["self-moon"]
    S.install(d, "owner", home)  # 再装一次: 换掉旧的, 不重复
    assert [s["by"] for s in S.load(home)["servers"]] == ["owner"]
    assert S.set_enabled("self-moon", False, home) and not S.load(home)["servers"][0]["enabled"]


@pytest.mark.parametrize("bad, why", [
    ("import os\n", "import os"),
    ("x = open('a.txt').read()\n", "用了 open"),
    ("import random\nrandom._os.system('calc')\n", "._os"),
    ("import datetime\ndatetime.sys.modules\n", ".sys"),
    ("from selfmcp_kit import _OPENER\n", "_OPENER"),
    ("U = 'https://evil.example.org/x'\n", "evil.example.org"),
    ("eval('1+1')\n", "用了 eval"),
])
def test_rule_breaking_code_needs_asking(tmp_path, bad, why):
    r = S.check(make(tmp_path, GOOD + bad))
    assert not r["ok"] and r["tier"] == "ask" and any(why in p for p in r["problems"]), r["problems"]


def test_sensitive_or_key_needs_asking(tmp_path):
    r = S.check(make(tmp_path, GOOD + "HINT = '把 cookie 填在这里'\n"))
    assert r["ok"] and r["tier"] == "ask" and r["sensitive"]
    shutil.rmtree(tmp_path / "mcp")
    r = S.check(make(tmp_path, needs_key=True))
    assert r["ok"] and r["tier"] == "ask" and "needs_key" in r["sensitive"][0]


def test_manifest_rules(tmp_path):
    r = S.check(make(tmp_path, id="Moon Phase", network=["127.0.0.1"], name="一个名字特别特别特别长的工具"))
    assert not r["ok"] and len(r["problems"]) == 3


def test_tools_must_match_manifest(tmp_path):
    r = S.check(make(tmp_path, tools=[{"name": "phase"}, {"name": "tide"}]))
    assert not r["ok"] and any("对不上" in p for p in r["problems"])


def test_kit_only_gets_declared_https_hosts(tmp_path, monkeypatch):
    import importlib.util

    d = make(tmp_path)
    shutil.copy2(S.KIT, d / "selfmcp_kit.py")
    spec = importlib.util.spec_from_file_location("kit_under_test", d / "selfmcp_kit.py")
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    for url in ("http://api.example.com/x", "https://evil.org/x", "https://127.0.0.1/x"):
        with pytest.raises(ValueError):
            kit.http_get(url)
    with pytest.raises(ValueError):
        kit.save_json("../escape", 1)
    kit.save_json("ok", {"a": 1})
    assert kit.load_json("ok") == {"a": 1} and (d / "data" / "ok.json").is_file()
    srv = kit.Server("t")

    @srv.tool("boom", "会出错")
    def boom():
        raise RuntimeError("坏了")
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "boom"}})
    assert r["result"]["isError"] and "坏了" in r["result"]["content"][0]["text"]
    assert srv.handle({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})["error"]["code"] == -32601
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
