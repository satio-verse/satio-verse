"""网络插件第 0 步 (4/4): wm/net/ 要能整目录拿掉.

国内公开版不带这个功能, 所以 (用户 2026-11-11 定的):
- wm/ 下除了 wm/net/ 以外的 .py 文件, 没有一个引用 wm.net (import、相对 import、importlib.import_module / __import__ 的字符串都算);
- wm/net/ 里所有 .py (包括别的对话写的配置生成器) 只许用标准库、wm.privilege、和这个包自己.
扫描用 ast, 不是搜文字 (注释和说明文字里提到 wm.net 不算引用). 扫描函数自己也用临时造的目录树验证过: 抓得到该抓的, 放得过该放的."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WM = ROOT / "wm"
NET = WM / "net"
STDLIB = set(sys.stdlib_module_names)
NET_NAME = "wm.net"
ALLOWED_PROJECT = ("wm.privilege", NET_NAME)  # wm.net 是这个包自己


def py_files(base: Path, skip: Path | None = None):
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts or (skip is not None and skip in path.parents):
            continue
        yield path


def package_of(path: Path, root: Path) -> list[str]:
    """文件所在的包: wm/x/y.py 和 wm/x/__init__.py 都是 ['wm', 'x']."""
    return list(path.relative_to(root).parent.parts)


def imports_of(path: Path, root: Path) -> list[tuple[int, str]]:
    """(行号, 绝对模块名). 相对 import 按文件位置还原; from wm import x 记成 wm.x (x 多半是子模块); 其它 from a.b import c 记成 a.b.
    importlib.import_module("...") / __import__("...") 里写死的字符串也算."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pkg = package_of(path, root)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = pkg[:max(len(pkg) - (node.level - 1), 0)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            if base == "wm":
                found += [(node.lineno, f"wm.{alias.name}") for alias in node.names]
            else:
                found.append((node.lineno, base))
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                found.append((node.lineno, node.args[0].value))
    return found


def refers_to_net(name: str) -> bool:
    return name == NET_NAME or name.startswith(NET_NAME + ".")


def references_outside(wm_root: Path, project_root: Path) -> list[str]:
    """wm/ 下 wm/net/ 以外的文件里对 wm.net 的引用."""
    out = []
    for path in py_files(wm_root, skip=wm_root / "net"):
        for line, name in imports_of(path, project_root):
            if refers_to_net(name):
                out.append(f"{path.relative_to(project_root).as_posix()}:{line} 引用了 {name}")
    return out


def disallowed_inside(net_root: Path, project_root: Path) -> list[str]:
    """wm/net/ 里不是标准库、wm.privilege、wm.net 自己的 import."""
    out = []
    for path in py_files(net_root):
        for line, name in imports_of(path, project_root):
            top = name.split(".")[0]
            ok = top in STDLIB or any(name == p or name.startswith(p + ".") for p in ALLOWED_PROJECT)
            if not ok:
                out.append(f"{path.relative_to(project_root).as_posix()}:{line} 用了 {name}")
    return out


# ---- 真的目录树 -------------------------------------------------------------------------------------------------

def test_nothing_in_wm_outside_wm_net_refers_to_wm_net():
    assert NET.is_dir() and any(WM.glob("*.py"))
    assert references_outside(WM, ROOT) == []


def test_everything_in_wm_net_uses_only_the_standard_library_and_wm_privilege():
    assert (NET / "checkup.py").is_file()
    assert disallowed_inside(NET, ROOT) == []


def test_wm_net_reaches_into_the_rest_of_wm_only_through_wm_privilege():
    """wm/net/ 往 wm/ 别处伸手的地方 (不算它自己) 只有 wm.privilege 一处: 整目录拿掉时不会带走别人要用的东西, 也不依赖别人的内部."""
    reached = {name for path in py_files(NET) for _, name in imports_of(path, ROOT) if name.split(".")[0] == "wm" and not refers_to_net(name)}
    assert reached <= {"wm.privilege"}, reached


# ---- 扫描函数自己 -----------------------------------------------------------------------------------------------

def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_the_scanner_catches_every_way_of_referring_to_wm_net(tmp_path):
    write_tree(tmp_path, {
        "wm/__init__.py": "",
        "wm/ok.py": "import json\nfrom wm import config\nfrom .config import x\n# import wm.net 在注释里不算\nS = 'wm.net 在字符串里不算'\n",
        "wm/a.py": "from wm.net import x\n",
        "wm/b.py": "import wm.net.collect\n",
        "wm/c.py": "from . import net\n",
        "wm/d.py": "from .net.snapshot import Snapshot\n",
        "wm/e.py": "from wm import net\n",
        "wm/f.py": "import importlib\nimportlib.import_module('wm.net.checkup')\n",
        "wm/g.py": "__import__('wm.net')\n",
        "wm/pkg/__init__.py": "",
        "wm/pkg/h.py": "from ..net import x\n",
        "wm/pkg/i.py": "from .. import net\n",
        "wm/pkg/j.py": "from ... import net\n",  # 越过 wm 之上: 不是 wm.net
        "wm/pkg/deeper/__init__.py": "",
        "wm/pkg/deeper/k.py": "from ...net import x\n",
        "wm/net/__init__.py": "",
        "wm/net/inside.py": "from wm.net import x\nimport wm.net.collect\n",  # wm/net 自己里面不算 "外面"
    })
    got = references_outside(tmp_path / "wm", tmp_path)
    flagged = sorted(line.split(":")[0] for line in got)
    assert flagged == ["wm/a.py", "wm/b.py", "wm/c.py", "wm/d.py", "wm/e.py", "wm/f.py", "wm/g.py", "wm/pkg/deeper/k.py", "wm/pkg/h.py",
                       "wm/pkg/i.py"]
    assert not any(line.startswith(("wm/ok.py", "wm/pkg/j.py", "wm/net/")) for line in got)


def test_the_scanner_catches_imports_that_are_not_standard_library_in_wm_net(tmp_path):
    write_tree(tmp_path, {
        "wm/__init__.py": "",
        "wm/privilege.py": "",
        "wm/net/__init__.py": "",
        "wm/net/good.py": ("from __future__ import annotations\nimport json, asyncio, ipaddress, winreg\nfrom dataclasses import dataclass\n"
                           "from wm.privilege import is_elevated\nfrom wm import privilege\nfrom .sibling import x\nfrom . import other\n"
                           "from .. import privilege as p2\nfrom ..privilege import is_elevated as e2\nimport wm.net.sibling\n"),
        "wm/net/bad.py": ("import psutil\nimport yaml\nfrom wm.agent import x\nfrom ..agent import y\nfrom .. import config\nimport aiohttp.web\n"
                          "import importlib\nimportlib.import_module('requests')\nfrom wm import net_other\nimport wm\n"),
    })
    got = disallowed_inside(tmp_path / "wm" / "net", tmp_path)
    assert not any("good.py" in line for line in got), got
    assert sorted(line.split(" 用了 ")[1] for line in got) == sorted(
        ["psutil", "yaml", "wm.agent", "wm.agent", "wm.config", "aiohttp.web", "requests", "wm.net_other", "wm"])


def test_a_clean_tree_passes_both_scans(tmp_path):
    write_tree(tmp_path, {"wm/__init__.py": "", "wm/x.py": "import os\n", "wm/net/__init__.py": "", "wm/net/y.py": "import os\n"})
    assert references_outside(tmp_path / "wm", tmp_path) == [] and disallowed_inside(tmp_path / "wm" / "net", tmp_path) == []
