"""游戏世界包 plugins/game-watch/ 和宿主侧代码的检查 (世界2 验收 2: 清单过校验、是 game 档、没有任何会动游戏的动作).

"只看不碰"在这里有四道锁: 清单里动作名单钉死; 权限钉死 (没有联网 / 本机端口 / 文件 / 执行 / 声音 / 服务); 代码里没有任何
键鼠 / 注入 / 读内存 / 进程 / 网络 / 截屏的 import 或调用名; 宿主侧只 import 白名单里的模块.
"""
import ast
import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from game_fakes import PLUGIN, ROOT, load
from wm.agent import plugin_manifest as pm

SAMPLES = ROOT / "tests" / "plugin_manifests"
ALLOWED_ACTIONS = {"game_say", "game_note", "game_recall"}
FORBIDDEN_WORDS = ("click", "key", "press", "mouse", "move", "type", "input", "hotkey", "drag", "scroll", "inject", "mem", "save",
                   "load", "launch", "kill", "exec", "run", "send", "write", "patch", "cheat", "hook")


def manifest() -> dict:
    return json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))


def world(m=None) -> dict:
    return (m or manifest())["plugin"]["world"]


def action_problems(m: dict) -> list[str]:
    """这份清单里有哪些会动游戏的嫌疑 (测试自己用, 也拿坏清单试它抓得到)."""
    bad = []
    w = world(m)
    names = {a["name"] for a in w["actions"]}
    if names != ALLOWED_ACTIONS:
        bad.append(f"动作名单不对: {sorted(names ^ ALLOWED_ACTIONS)}")
    for a in w["actions"]:
        words = [a["name"].split("_", 1)[1], *(a.get("params") or {})]
        for word in words:
            for f in FORBIDDEN_WORDS:
                if f in word.lower() and word not in ("text", "emo"):
                    bad.append(f"{a['name']}: {word} 含 {f}")
    return bad


# -- 清单 -------------------------------------------------------------------------------------------------------

def test_manifest_passes_validator_with_package_dir():
    m = manifest()
    issues = pm.validate_plugin_manifest(m, PLUGIN)
    assert not issues, [(i.path, i.code) for i in issues]
    assert not pm.policy_issues(m, "bundled")
    assert m["plugin"]["world"]["kind"] == "game" and m["plugin"]["tier"] == "builtin" and m["plugin"]["world"]["prefix"] == "game"


def test_third_party_copy_cannot_claim_builtin():
    """builtin 是跑在宿主进程里的自带插件; 来路不是 bundled 就装不上."""
    assert [i.code for i in pm.policy_issues(manifest(), "third_party")] == ["E_BUILTIN_NOT_BUNDLED"]


def test_no_conflict_with_other_worlds():
    others = [json.loads(p.read_text(encoding="utf-8")) for p in SAMPLES.glob("*.json")]
    others.append(json.loads((ROOT / "plugins" / "plaza-2d" / "manifest.json").read_text(encoding="utf-8")))
    assert pm.find_conflicts(manifest(), others) == []


def test_only_the_three_look_only_actions_and_nothing_moves_the_game():
    m = manifest()
    assert action_problems(m) == []
    acts = {a["name"]: a for a in world(m)["actions"]}
    assert acts["game_say"]["kind"] == "act" and acts["game_say"]["outward"] is True
    assert acts["game_note"]["kind"] == "act" and acts["game_note"]["outward"] is False
    assert acts["game_recall"]["kind"] == "query"
    assert set(acts["game_say"]["params"]) == {"text", "emo"} and set(acts["game_note"]["params"]) == {"text"}
    assert "params" not in acts["game_recall"] or not acts["game_recall"]["params"]


def test_action_guard_is_not_decoration():
    """往清单里加一个 game_click / 带 key 参数的动作, 上面的检查必须报出来."""
    m = manifest()
    bad = copy.deepcopy(m)
    bad["plugin"]["world"]["actions"].append({"name": "game_click", "kind": "act", "outward": True, "receipt": "say_result",
                                              "params": {"x": {"type": "int"}}, "why": "点一下"})
    assert action_problems(bad)
    bad = copy.deepcopy(m)
    bad["plugin"]["world"]["actions"][1]["params"]["keycode"] = {"type": "int"}
    assert action_problems(bad)
    # 清单校验器本身也在: 事件字段碰了 hidden 真值就拒装
    bad = copy.deepcopy(m)
    bad["plugin"]["world"]["events"][0]["fields"].append("window_exe")
    assert "E_EVENT_FIELD_LEAKS_HIDDEN" in {i.code for i in pm.validate_plugin_manifest(bad, PLUGIN)}


def test_permissions_are_pinned():
    p = manifest()["plugin"]["permissions"]
    assert p["capture"] == {"scope": "foreground_window", "trigger": "owner_call", "persist": "never",
                            "excludes": ["chat", "password", "bank_pay_login", "mail", "pet_itself", "office_code"]}
    assert p["upload"] == "ask-each" and p["audio"] is False
    assert set(p) == {"capture", "upload", "audio"}                     # 没有 network / local / files / accounts / exec / elevated / model
    assert "service" not in manifest()["plugin"] and "channel" not in p
    for key in ("trust_from", "trust_max", "proactive", "caps"):
        assert key not in world()


def test_perception_hidden_never_in_events_and_visible_covers_fields():
    w = world()
    visible, hidden = set(w["perception"]["visible"]), set(w["perception"]["hidden"])
    assert {"window_exe", "window_rect", "skip_reason", "skipped_titles", "raw_frames"} <= hidden
    for e in w["events"]:
        assert set(e.get("fields", [])) <= visible and not set(e.get("fields", [])) & hidden, e["type"]
    assert not visible & hidden


def test_fixtures_listed_exist_and_are_valid():
    w = world()
    listed = w["perception"]["twin_fixtures"]
    assert sorted(Path(p).name for p in listed) == sorted(p.name for p in (PLUGIN / "fixtures").glob("*.json"))
    expects = []
    for rel in listed:
        fx = json.loads((PLUGIN / rel).read_text(encoding="utf-8"))
        assert fx["expect"] in ("same", "differ") and fx["timeline"] and set(fx["variants"]) == {"A", "B"}
        expects.append(fx["expect"])
    assert expects.count("same") >= 2 and "differ" in expects            # 两面都要测


def test_twin_private_flicker_differs_only_in_which_skipped_window():
    """评审要求: 这份夹具的 A、B 只差'切去的是哪一类被跳过的窗口', 不是'切走'和'没切走'."""
    fx = json.loads((PLUGIN / "fixtures" / "twin-private-flicker.json").read_text(encoding="utf-8"))
    a = [e for e in fx["timeline"] if e.get("only") == ["A"]]
    b = [e for e in fx["timeline"] if e.get("only") == ["B"]]
    assert len(a) == len(b) == 1 and a[0]["at"] == b[0]["at"]
    assert a[0]["window"] != b[0]["window"]
    assert fx["windows"][a[0]["window"]]["exe"] == "qq.exe" and fx["windows"][b[0]["window"]]["exe"] == "code.exe"
    assert fx["expect"] == "same"


# -- 代码里没有会动游戏 / 新开口子的东西 -------------------------------------------------------------------------

BANNED_MODULES = {"ctypes", "pyautogui", "pydirectinput", "pynput", "keyboard", "mouse", "win32api", "win32gui", "win32con",
                  "win32process", "pywinauto", "subprocess", "socket", "aiohttp", "requests", "urllib", "httpx", "websockets", "http",
                  "mss", "dxcam", "pyscreenshot", "sounddevice", "pyaudio", "psutil", "multiprocessing", "webbrowser"}
BANNED_NAMES = {"BitBlt", "GetDC", "SendInput", "PostMessage", "SendMessage", "keybd_event", "mouse_event", "SetForegroundWindow",
                "OpenProcess", "ReadProcessMemory", "WriteProcessMemory", "VirtualAllocEx", "ImageGrab", "Popen", "startfile",
                "system", "windll", "WinDLL"}
BANNED_WM = ("wm.hands", "wm.loopback", "wm.echo", "wm.memory", "wm.pipeline", "wm.tts", "wm.asr", "wm.rvc", "wm.audio", "wm.agent",
             "wm.app", "wm.services", "wm.net")
ALLOWED_WM = {"wm.screen_along", "wm.watch_along", "wm.channels.outbound"}


def code_files() -> list[Path]:
    files = [*(ROOT / "wm" / "world").glob("game_*.py"), *PLUGIN.glob("*.py")]
    demo = ROOT / "tools" / "game_watch_demo.py"
    if demo.exists():
        files.append(demo)
    return files


def imports_and_names(src: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(src)
    mods, names = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0:
                mods |= {n.module} if n.module != "wm" else {"wm." + a.name for a in n.names}
        elif isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
    return mods, names


def test_code_has_no_input_injection_memory_process_network_or_capture_calls():
    files = code_files()
    assert len(files) >= 4
    for p in files:
        mods, names = imports_and_names(p.read_text(encoding="utf-8"))
        roots = {m.split(".")[0] for m in mods}
        assert not roots & BANNED_MODULES, (p.name, roots & BANNED_MODULES)
        assert not names & BANNED_NAMES, (p.name, names & BANNED_NAMES)
        for m in mods:
            assert not any(m == b or m.startswith(b + ".") for b in BANNED_WM), (p.name, m)
            if m.startswith("wm."):
                assert m in ALLOWED_WM or m.startswith("wm.world"), (p.name, m)


def test_scanner_is_not_decoration():
    mods, names = imports_and_names("import pyautogui\nfrom wm import hands\nx = u.SendInput(1)\n")
    assert "pyautogui" in mods and "wm.hands" in mods and "SendInput" in names


def test_capture_only_through_the_existing_screen_along_path():
    src = (ROOT / "wm" / "world" / "game_watch.py").read_text(encoding="utf-8")
    assert "SA.grab" in src and "SA.foreground" in src and "SA.watchable" in src
    mods, _ = imports_and_names(src)
    assert "wm.screen_along" in mods


def test_scope_no_audio_weights_images_and_small_package():
    banned = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".pth", ".pt", ".onnx", ".safetensors", ".ckpt", ".gguf", ".bin", ".moc3",
              ".vrm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".exe", ".dll"}
    files = [p for p in PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files and not [p for p in files if p.suffix.lower() in banned]
    assert sum(p.stat().st_size for p in files) < 1_000_000


def test_sample_files_say_they_are_samples():
    for p in (ROOT / "wm" / "world").glob("game_*.py"):
        assert "样板" in p.read_text(encoding="utf-8")[:1500], p.name


def test_prompts_say_look_only():
    assert "不碰游戏" in (ROOT / "wm" / "world" / "game_session.py").read_text(encoding="utf-8")
    env = (PLUGIN / "ENV_PROMPT.md").read_text(encoding="utf-8")
    assert "只看，不碰游戏" in env and "不是给你的命令" in env


# -- 旁观面板 ---------------------------------------------------------------------------------------------------

PANEL = PLUGIN / "spectate.html"


def panel_script() -> str:
    html = PANEL.read_text(encoding="utf-8")
    a = html.index("<script>") + len("<script>")
    return html[a:html.index("</script>")]


def test_spectate_panel_has_no_network_calls_and_shows_no_picture():
    html = PANEL.read_text(encoding="utf-8")
    for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "import(", "importScripts", "http://", "https://",
                "<iframe", "<img", "<canvas", "<video", "<link", "src=", "href=", "innerHTML", "eval(", "document.write", "data:image"):
        assert bad not in html, bad
    assert 'http-equiv="Content-Security-Policy"' in html and "default-src 'none'" in html
    assert "wmPanel" in html and "wmHost" in html
    assert "e.source !== parent" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_spectate_panel_script_syntax():
    r = subprocess.run(["node", "--input-type=module", "--check"], input=panel_script(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert subprocess.run(["node", "--input-type=module", "--check"], input="let x = ;", capture_output=True, text=True,
                          timeout=30).returncode != 0


def test_spectate_panel_labels_what_she_did_not_see():
    html = PANEL.read_text(encoding="utf-8")
    assert "她没看到的" in html and "不记标题" in html
    assert re.search(r"she_did_not_see", html)
