"""例子的测试: 不起进程, 直接调 srv.handle (要联网的工具在这里把 http_get_json 换成假的)."""
import server


def call(name, **args):
    return server.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})


def test_list_is_read_only():
    tools = server.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["phase"] and tools[0]["annotations"]["readOnlyHint"]


def test_phase():
    text = call("phase", date="2000-01-21")["result"]["content"][0]["text"]
    assert "满月" in text
