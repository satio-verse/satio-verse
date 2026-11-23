"""广场插件包 plugins/plaza-2d/ 的检查 (世界1 验收 4 和世界6、7 的"不相反").

清单过契约校验器; 和急7 的样例只差约定的几处; 包里没有语音 / 权重 / 图片素材, 整包远小于 50 MB; 旁观面板不含网络调用,
脚本语法没错; 样板文件顶上都写明了"样板".
"""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plaza_fakes import PLUGIN, ROOT, load_manifest
from wm.agent import plugin_manifest as pm

SAMPLES = ROOT / "tests" / "plugin_manifests"


def sample(name):
    return json.loads((SAMPLES / (name + ".json")).read_text(encoding="utf-8"))


def test_manifest_passes_validator_with_package_dir():
    m = load_manifest()
    issues = pm.validate_plugin_manifest(m, PLUGIN)
    assert not issues, [(i.path, i.code) for i in issues]
    assert not pm.policy_issues(m, "bundled")
    assert m["plugin"]["tier"] == "process" and m["plugin"]["world"]["kind"] == "space"


def test_manifest_has_no_conflict_with_other_samples():
    m = load_manifest()
    others = [sample(n) for n in ("qq-onebot", "weixin-ilink", "local-guest")]
    assert pm.find_conflicts(m, others) == []
    assert pm.find_conflicts(m, [sample("plaza-2d")]) == []   # 同一个 id 是更新, 不算冲突


def test_manifest_only_differs_from_sample_in_agreed_ways():
    """和急7 的样例 plaza-2d.json 的差别恰好是计划 3.2 里列的几条 (多了别的就失败)."""
    a, b = sample("plaza-2d"), load_manifest()
    wa, wb = a["plugin"]["world"], b["plugin"]["world"]
    # 不变的部分
    for key in ("kind", "prefix", "env_prompt", "leave", "spectate"):
        assert wa[key] == wb[key], key
    assert wa["actions"] == wb["actions"]
    assert wa["perception"]["hidden"] == wb["perception"]["hidden"]
    for key in ("tier", "permissions", "service", "panel", "budget", "schema", "version", "author", "license", "host"):
        assert a["plugin"][key] == b["plugin"][key], key
    assert a["id"] == b["id"] and a["name"] == b["name"]
    # 约定的 4 处差别
    ea = {e["type"]: e for e in wa["events"]}
    eb = {e["type"]: e for e in wb["events"]}
    assert set(eb) - set(ea) == {"pet_emoted"}                                                # 1. 多一个事件
    assert eb["pet_emoted"]["sense"] == "saw"
    for t in ("pet_entered", "pet_moved", "pet_said"):                                        # 2. 加 direction
        assert eb[t]["fields"] == ea[t]["fields"] + ["direction"] if t != "pet_said" else "direction" in eb[t]["fields"]
    assert ea["act_result"].get("fields") is None and eb["act_result"]["fields"] == ["ok", "why", "own_position"]   # 3.
    assert wb["perception"]["twin_fixtures"] == wa["perception"]["twin_fixtures"] + ["fixtures/twin-near-pet.json"]  # 4.
    assert set(wb["perception"]["visible"]) - set(wa["perception"]["visible"]) == {"direction", "emote", "ok", "why"}
    assert eb["pet_left"] == ea["pet_left"]


def test_manifest_breaking_a_rule_is_caught(tmp_path):
    """校验这条路不是摆设: 事件字段碰了 hidden 真值就拒装."""
    m = load_manifest()
    m["plugin"]["world"]["events"][0]["fields"].append("all_positions")
    codes = {i.code for i in pm.validate_plugin_manifest(m, PLUGIN)}
    assert "E_EVENT_FIELD_LEAKS_HIDDEN" in codes
    m = load_manifest()
    shutil.copytree(PLUGIN, tmp_path / "pkg")
    (tmp_path / "pkg" / "spectate.html").unlink()
    assert any(i.code == "E_FILE_MISSING" for i in pm.validate_plugin_manifest(m, tmp_path / "pkg"))


def test_fixtures_listed_in_manifest_exist_and_are_valid_json():
    for rel in load_manifest()["plugin"]["world"]["perception"]["twin_fixtures"]:
        fx = json.loads((PLUGIN / rel).read_text(encoding="utf-8"))
        assert fx["expect"] in ("same", "differ") and fx["timeline"] and set(fx["variants"]) == {"A", "B"}


def test_scope_no_voice_weights_or_assets_and_small_package():
    """世界7 不相反: 包里没有语音 / 权重 / 外观素材; 整包远小于 50 MB 上限. 世界6: 中继没有公开发现入口 (见 test_plaza_relay)."""
    banned = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".pth", ".pt", ".onnx", ".safetensors", ".ckpt", ".gguf", ".bin", ".moc3",
              ".vrm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".exe", ".dll"}
    files = [p for p in PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files and not [p for p in files if p.suffix.lower() in banned]
    assert sum(p.stat().st_size for p in files) < 1_000_000 < 50 * 1024 * 1024
    for p in [*PLUGIN.glob("*.py"), ROOT / "tools" / "plaza_relay.py", *(ROOT / "wm" / "world").glob("*.py")]:
        src = p.read_text(encoding="utf-8")
        for mod in ("wm.tts", "wm.asr", "wm.rvc", "wm.audio", "wm.a2f", "gpt_sovits", "pyaudio", "sounddevice"):
            assert f"import {mod}" not in src and f"from {mod}" not in src, (p.name, mod)


def test_sample_files_say_they_are_samples():
    for p in [*(ROOT / "wm" / "world").glob("*.py"), ROOT / "tools" / "plaza_relay.py", *PLUGIN.glob("*.py")]:
        head = p.read_text(encoding="utf-8")[:1200]
        assert "样板" in head, p.name


# -- 旁观面板 ------------------------------------------------------------------------------

PANEL = PLUGIN / "spectate.html"


def panel_script() -> str:
    html = PANEL.read_text(encoding="utf-8")
    a = html.index("<script>") + len("<script>")
    return html[a:html.index("</script>")]


def test_spectate_panel_has_no_network_calls():
    html = PANEL.read_text(encoding="utf-8")
    for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(", "importScripts", "http://", "https://",
                "<iframe", "<img", "<link", "src=", "href=", "innerHTML", "eval(", "document.write"):
        assert bad not in html, bad
    assert 'http-equiv="Content-Security-Policy"' in html and "default-src 'none'" in html
    assert "wmPanel" in html and "wmHost" in html
    assert "e.source !== parent" in html      # 只认宿主发来的消息


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_spectate_panel_script_syntax(tmp_path):
    r = subprocess.run(["node", "--input-type=module", "--check"], input=panel_script(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert subprocess.run(["node", "--input-type=module", "--check"], input="let x = ;", capture_output=True, text=True,
                          timeout=30).returncode != 0   # 检查本身会报错, 不是摆设


def test_relay_client_accepts_only_loopback_relay_urls():
    import sys
    sys.path.insert(0, str(PLUGIN))
    import relay_client as rc
    assert rc.parse_relay("ws://127.0.0.1:8801/plaza")
    for bad in ("ws://example.com:8801/plaza", "wss://127.0.0.1:8801/plaza", "ws://localhost:8801/plaza", "ws://10.0.0.5:8801/plaza",
                "http://127.0.0.1:8801/plaza", ""):
        with pytest.raises(ValueError):
            rc.parse_relay(bad)


def test_relay_client_requires_token_and_name(monkeypatch, capsys):
    import asyncio
    import sys
    sys.path.insert(0, str(PLUGIN))
    import relay_client as rc
    for k in ("WM_PLUGIN_TOKEN", "PLAZA_NAME", "PLAZA_CODE", "PLAZA_CREATE", "PLAZA_RELAY"):
        monkeypatch.delenv(k, raising=False)
    assert asyncio.run(rc.amain()) == 2                      # 没令牌
    monkeypatch.setenv("WM_PLUGIN_TOKEN", "t")
    monkeypatch.setenv("PLAZA_NAME", "甲")
    assert asyncio.run(rc.amain()) == 2                      # 没邀请码也没说要开新房间
    monkeypatch.setenv("PLAZA_CREATE", "1")
    monkeypatch.setenv("PLAZA_RELAY", "ws://example.com/plaza")
    assert asyncio.run(rc.amain()) == 2                      # 中继地址不是本机
