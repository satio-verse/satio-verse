"""契约测试 (不可变): 她自己写的 MCP 按规范查, 不靠她自觉 (wm/selfmcp.py; external-channels.md 第 17.1 节).
规范以外的写法一律算硬问题 (不能自己装), 碰到 key、token、本机端口这类字眼要问主人.
"""
from wm import selfmcp as SM

NET = ["api.example.com"]
GOOD = '''from selfmcp_kit import Server, http_get_json

srv = Server("moon", "0.1.0")


@srv.tool("phase", "查某天的月相", {"date": {"type": "string"}}, required=["date"])
def phase(date: str) -> str:
    data = http_get_json("https://api.example.com/moon", {"date": date})
    return str(data.get("phase", ""))
'''
BAD = {
    "open": 'f = open("x")',
    "eval": 'eval("1")',
    "exec": 'exec("1")',
    "getattr": 'getattr(1, "x")',
    "__import__": '__import__("os")',
    "import os": "import os",
    "import subprocess": "import subprocess",
    "import socket": "import socket",
    "下划线属性": "x = (1).__class__",
    "绕到 sys": "import datetime\nd = datetime.sys",
    "没写的域名": 'u = "https://evil.example.org/a"',
    "骨架以外的东西": "from selfmcp_kit import _request",
}


def test_good_code_passes():
    assert SM.check_code(GOOD, NET) == ([], [])


def test_code_outside_the_convention_is_rejected():
    for name, src in BAD.items():
        hard, _ = SM.check_code(src, NET)
        assert hard, name


def test_sensitive_words_need_the_owner():
    for src in ('u = "https://api.example.com/x?token=1"', 'u = "http://127.0.0.1:8790/api"', 'p = "pet-settings.json"'):
        _, soft = SM.check_code(src, NET + ["127.0.0.1"])
        assert soft, src


def test_manifest_rules():
    ok = {"id": "moon", "name": "看月相", "purpose": "查某天的月相", "network": NET, "tools": [{"name": "phase"}]}
    assert SM.check_manifest(ok) == []
    for net in (["localhost"], ["127.0.0.1"], ["a.com", "b.com", "c.com", "d.com", "e.com", "f.com"]):
        assert SM.check_manifest({**ok, "network": net}), net
    assert SM.check_manifest({**ok, "tools": []})
