"""插件宿主第 0 步第三小步 (B): 路径规则也对还原后的命令各找一遍.

以前命令类规则会先把命令还原 (去掉 ^ ` 和引号的、-EncodedCommand 的 Base64 解出来的), 路径规则只在命令原文里找路径: 把"写登记表"
这样的命令整个编码一下, 路径规则就看不见了. 现在路径规则对同样几份文字各找一遍 (每份照旧先展开环境变量和 ~、统一斜杠、折叠大小写),
任意一份命中就算; 份数照旧受 MAX_COMMAND_VIEWS 限制. 只许多拦, 不许少拦: 原文那一份的判定一个字不变.
命令一条也不执行, 只是把字符串交给守门判; 全在临时目录."""
import base64
import hashlib
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agent_elevation_guard import SAFE, b64
from wm.agent import governance as G
from wm.agent import policy as P
from wm.agent.governance import BlacklistStore


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(P, "_home", lambda: home)
    monkeypatch.setenv("USERPROFILE", str(home))  # $env:USERPROFILE 和 %USERPROFILE% 的写法落在这个家目录
    ws = tmp_path / "project"
    ws.mkdir()
    store = BlacklistStore(tmp_path / "agent")
    private = tmp_path / "private"
    store.add({"kind": "path", "value": str(private), "reason": "我的私人文件"})  # 用户自己写的路径规则
    ext = store.home / "extensions"
    state = home / ".worldmodel"
    targets = {"扩展登记表": ext / "registry.json", "扩展工具快照": ext / "direct-tools.json", "扩展审计记录": ext / "audit.jsonl",
               "她的 MCP 名单": state / "self-mcp" / "registry.json", "等你点头的申请": state / "asks.json",
               "工作台派活的线": state / "workbench-threads.json", "QQ 攒着的消息": state / "qq-digest.json",
               "用户自己的规则": private / "secret.txt"}
    return SimpleNamespace(store=store, home=home, ws=ws, state=state, tmp=tmp_path, targets=targets)


def denied(world, command):
    d = world.store.check("run_command", {"command": command, "workdir": str(world.ws)}, world.ws)
    return d is not None and d.how == "deny"


def write_to(path):
    return f"Set-Content -Path '{path}' -Value x"


# ---------------------------------------------------------------- 编码、转义夹在命令里也拦
def test_encoding_the_whole_command_does_not_hide_a_protected_write(world):
    for label, path in world.targets.items():
        plain = write_to(path)
        assert denied(world, plain), label  # 原文本来就拒
        encoded = f"powershell -EncodedCommand {b64(plain)}"
        assert denied(world, encoded), label
        assert denied(world, f"powershell -enc {b64('cmd /c ' + encoded)}"), label  # 套两层
        assert denied(world, f"cmd /c \"powershell -e {b64(plain)}\""), label  # 外面再套一层 cmd /c


def test_aliases_inside_the_encoded_text_are_expanded_too(world):
    """解出来的文字也先展开环境变量和 ~ 再找路径."""
    for inner in ("Set-Content $env:USERPROFILE\\.worldmodel\\asks.json x", "Set-Content ~/.worldmodel/qq-digest.json x",
                  "echo x > %USERPROFILE%\\.worldmodel\\workbench-threads.json", "Remove-Item ~\\.worldmodel\\self-mcp\\registry.json"):
        assert denied(world, f"powershell -EncodedCommand {b64(inner)}"), inner


def test_escape_characters_and_quotes_inside_the_path_do_not_hide_it_either(world):
    for label, path in world.targets.items():
        parent, name = str(path.parent), path.name
        for hidden in (name[:4] + "^" + name[4:], name[:4] + "`" + name[4:], name[:4] + '"' + name[4:] + '"', "'" + name[:3] + "'" + name[3:]):
            command = f"Set-Content {parent}\\{hidden} x"
            assert denied(world, command), (label, command)


def test_a_protected_write_stays_denied_for_the_write_tools_too(world):
    """命令那边的看法不影响工具参数那边: 路径参数照旧按解析后的真实路径判."""
    for label, path in world.targets.items():
        assert world.store.check("write_file", {"path": str(path)}, world.ws).how == "deny", label


# ---------------------------------------------------------------- 不误拦
NEW_SAFE = [
    "certutil -hashfile a.bin SHA256 " + hashlib.sha256(b"x").hexdigest(),  # 一长串十六进制哈希
    "echo " + base64.b64encode(b"Hello world, this is a harmless payload with no paths in it at all.").decode(),  # 不相干的 Base64 数据
    f"powershell -EncodedCommand {b64('Get-Date')}",  # 编码过的无害命令
    f"powershell -EncodedCommand {b64('Get-ChildItem C:/work -Force | Select-Object Name')}",
    "type C:\\work\\" + "a" * 220 + ".txt",  # 很长的文件名
    "echo " + uuid.UUID(int=0x550E8400E29B41D4A716446655440000).hex,  # 不带横线的 UUID
    "echo " + base64.b64encode(bytes(range(256))).decode(),  # 一整段二进制的 Base64
    "python tools/run.py --input data/some/very/long/directory/structure/with/many/parts/file.json",
    "Get-Content $env:USERPROFILE\\Documents\\notes.txt",  # 家目录里别的地方
]
SAME_NAME_ELSEWHERE = "Set-Content C:\\work\\registry.json x"  # 同名文件, 不在受保护的目录里


@pytest.mark.parametrize("command", [*SAFE, *NEW_SAFE])
def test_ordinary_commands_are_not_caught(world, command):
    assert not denied(world, command), command


def test_encoded_commands_about_other_files_pass(world):
    for inner in (SAME_NAME_ELSEWHERE, "Set-Content C:\\work\\asks.json x", "Get-Content C:\\work\\qq-digest.json",
                  "Set-Content ~/notes/workbench-threads.json x"):
        assert not denied(world, f"powershell -EncodedCommand {b64(inner)}"), inner


# ---------------------------------------------------------------- 原文那一份的判定一个字不变
def old_command_paths(command):
    """版本 3 的 check() 里找路径用的那份文字 (只有原文)."""
    expanded = G._ENV.sub(lambda m: os.environ.get(m[1], m[0]), os.path.expandvars(command))
    return G._TILDE.sub(lambda _: str(P._home()), expanded).replace("/", "\\").casefold()


def old_path_hit(command, root):
    normalized = P.unvirtualize(str(Path(root).resolve())).replace("/", "\\").casefold()
    return bool(command) and bool(re.search(re.escape(normalized) + r"(?=$|[\\\s\"'`;|&<>),])", old_command_paths(command)))


def test_the_original_text_is_judged_exactly_as_before_and_nothing_gets_through_that_did_not_before(world):
    roots = list(world.targets.values()) + [world.tmp / "private", world.home / ".worldmodel", world.store.home / "extensions"]
    store = BlacklistStore(world.tmp / "plain", defaults=False)  # 只有路径规则, 才看得出路径规则的判定
    for root in roots:
        store.add({"kind": "path", "value": str(root)})
    templates = ["Set-Content '{p}' x", 'Add-Content "{p}" x', "echo x > {p}", "Remove-Item {p}", "Copy-Item a {fwd}", "type {p}",
                 "Move-Item {p} {p}.old", "Get-Content {p}.bak", "Get-Content {p}x", "Get-Content {p}\\child.txt", "dir {parent}",
                 "python -c \"open(r'{p}')\"", "Set-Content {rel} x", "Set-Content ~/.worldmodel/asks.json x",
                 "Set-Content $env:USERPROFILE\\.worldmodel\\qq-digest.json x", "echo x > %USERPROFILE%\\.worldmodel\\asks.json",
                 "Set-Content ~\\notes.txt x", "git show HEAD~1", "echo done", ""]
    seen, plain, noisy_extra = 0, 0, 0
    for root in roots:
        for template in templates:
            base = template.format(p=root, fwd=str(root).replace("\\", "/"), parent=root.parent, rel=root.name)
            noisy = [base.replace(root.name, root.name[:3] + "^" + root.name[3:]), base.replace("Set-Content", "Set-`Content")]
            plain_variants = [base, f"cmd /c \"{base}\""]
            encoded = [f"powershell -enc {b64(base)}"] if base else []
            for command in plain_variants + noisy + encoded:
                old = any(old_path_hit(command, r) for r in roots)
                new = store.check("run_command", {"command": command}, world.ws) is not None
                seen += 1
                assert new or not old, ("以前拦的现在放行了", command)  # 只许多拦, 不许少拦
                if G._command_variants(command) == [command]:  # 没有 ^ ` 引号、没有 Base64: 只有原文这一份, 判定必须和以前一样
                    plain += 1
                    assert new == old, ("原文的判定变了", command)
                noisy_extra += new and not old
    assert seen > 600 and plain > 300 and noisy_extra > 20  # 确实比对了很多条, 也确实多拦了一些 (编码、转义的写法)


def test_command_views_are_unchanged_by_the_refactor():
    """命令类规则看的几份文字 (_command_views) 和以前一样: 原文、去掉 ^ ` 引号的、Base64 解出来的, 都折叠了大小写和空白."""
    def old_views(command):
        texts = [command]
        for text in texts:
            for decoded in G._decoded_texts(text):
                if len(texts) < G.MAX_COMMAND_VIEWS and decoded not in texts:
                    texts.append(decoded)
        views = []
        for text in texts:
            for view in (G._literal(text), G._literal(text.translate(G._NOISE))):
                if view not in views:
                    views.append(view)
        return views

    for command in ("Echo  A", 'r^un"as" /user:x', f"powershell -enc {b64('Start-Process cmd -Verb RunAs')}",
                    f"powershell -enc {b64('cmd /c powershell -enc ' + b64('Set-Content a b'))}", "echo " + "A" * 17, "echo " + "////" * 8,
                    "", "git push origin main"):
        assert G._command_views(command) == old_views(command), command
    deep = "x"
    for _ in range(9):
        deep = f"powershell -enc {b64(deep)}"
    assert G._command_views(deep) == old_views(deep)
    assert len(G._command_variants(deep)) <= 2 * G.MAX_COMMAND_VIEWS  # 份数照旧受限制
