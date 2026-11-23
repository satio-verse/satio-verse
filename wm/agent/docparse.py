"""文档解析在工作台这边的一半 (引擎在 wm/docparse/): 她的两个工具、权限、MANIFEST. 计划: backups/tool1-docparse/计划.md.

- doc_parse: 读一份文档 (文字层优先; 扫描页 / 图片在装了本机识字组件时识字), 返回带页码的段. 读不了就明说读不了和原因,
  不改走云端, 也不引导去云端 (云端那档这一轮没开通).
- doc_backends: 看现在哪几档能用、识字组件装没装、缺什么.
权限和 read_file 一样: 先过黑名单, 再过 check_path ("read_file"); 保护的地方 (桌宠设置、token、记忆库、密钥) 照旧拒. 不问主人 ——
读文档不写任何东西, 文件也不出本机.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .. import docparse as DP
from ..docparse import DocParseError, router

TOOL_MAX_CHARS = 22_000     # studio.execute 把超过 3 万字的工具结果换成节选; JSON 转义会涨字数, 留余量
TOOL_DEFAULT_CHARS = 15_000


def _spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}}


TOOL_SPECS = [
    _spec("doc_parse",
          "读一份文档（PDF、Word、Excel、PowerPoint、OpenDocument、RTF、EPUB、文本和代码、压缩包里的文档，扫描件和图片在装了本机识字组件时也能读），"
          "按页 / 张 / 表分段返回，每段带名字和页码。读 PDF、Office 文档优先用它，回答时按段里的页码说出处。"
          "start 从第几段读起（PDF 一页一段）；一次放不下时结果里 next 会告诉你怎么接着读。"
          "结果里 unread 列的是没读到的段和原因：没读到的就是没读到，如实告诉主人，不要猜内容、不要编。"
          "识字只认字，表格、公式、双栏顺序不保证。本机识字组件没装时扫描件读不了；装东西要先走下载队列让主人点头，不要自己装。",
          {"path": {"type": "string", "description": "文件路径（相对工作区或绝对路径）"},
           "start": {"type": "integer", "minimum": 1, "description": "从第几段读起，默认 1"},
           "count": {"type": "integer", "minimum": 1, "description": "最多读几段，不填就读到字数上限"},
           "max_chars": {"type": "integer", "minimum": 1000, "maximum": TOOL_MAX_CHARS, "description": "这次最多返回多少字"},
           "char_offset": {"type": "integer", "minimum": 0, "description": "某一段太长被切开时，从这一段的第几个字读起（next 里会给）"},
           "ocr": {"type": "string", "enum": ["auto", "never"], "description": "auto：读不出字的页在可以时识字；never：不识字"}},
          ["path"]),
    _spec("doc_backends", "看文档解析现在有哪几档能用：本机文字层、本机识字（装没装、缺什么）、云端（是否开通）。"),
]
NAMES = {x["function"]["name"] for x in TOOL_SPECS}
READ_ONLY = set(NAMES)
LABELS = {"doc_parse": "解析文档", "doc_backends": "看文档解析后端"}

# 引擎的运行参数, 测试里换 (识字组件目录、识字子进程的命令、超时). 正式运行为空: 用默认的 runtime/ocr/ 和 wm/docparse/worker.py.
ENGINE_ARGS: dict = {}

# 照《底层终端（插件宿主）方案》的清单格式写的声明 (宿主现在还不读; tests/test_agent_docparse.py 核对这里列的工具和真正给她的对得上).
MANIFEST = {
    "id": "doc-parse", "name": "文档解析", "description": "读文档：文字层优先，扫描页和图片用本机识字（RapidOCR，可选组件）；读不了就明说，不自动上云。",
    "plugin": {
        "schema": 2, "version": "0.1.0", "author": "WorldModel", "license": "随 WorldModel 本体", "tier": "tool",
        "provides": "doc-parse",
        "tools": [{"name": "doc_parse", "risk": "read", "why": "读一份文档并按页返回文字"},
                  {"name": "doc_backends", "risk": "read", "why": "看解析后端的状态"}],
        "permissions": {"network": [], "local": [], "files": [{"path": "$ANY_READABLE", "mode": "r"}], "accounts": [],
                        "notify": False, "model": None, "upload": "never", "elevated": []},
        "backends": [
            {"id": "builtin-text", "kind": "builtin", "upload": False, "auto": True},
            {"id": "rapidocr-local", "kind": "local-ocr", "upload": False, "auto": True, "component": "rapidocr"},
            {"id": "mineru-cloud", "kind": "cloud", "upload": True, "auto": False, "needs_consent": True, "available": False}],
        "service": None, "panel": None, "schedule": [], "world": None,
        "components": [{"id": "rapidocr", "from": "PyPI 官方（经下载队列，主人点头才下）", "target": "runtime/ocr", "license": "Apache-2.0",
                        "wheels": ["rapidocr-onnxruntime 1.4.4", "pyclipper 1.3.0.post6", "shapely 2.1.2"]}],
        "settings": []},
}


def shown(name: str, args: dict) -> str:
    """页面上那一行工具记录写什么: 文件名."""
    if name != "doc_parse":
        return ""
    return Path(str(args.get("path") or "").strip().strip('"')).name[:80]


def _path(s, raw) -> Path:
    from . import policy as P
    from . import tools as T

    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("要给 path")
    bad = P.raw_path_ok(raw)
    if bad:
        raise ValueError(bad)
    return T.resolve(raw, Path(s.workspace))


def decision(hub, s, name: str, args: dict):
    """工作台权限 (StudioRuntime.decision 转过来): 读和 read_file 一样 —— 过黑名单和 check_path, 不问主人."""
    from . import policy as P

    if name == "doc_backends":
        return P.Decision("auto")
    try:
        path = _path(s, args.get("path"))
    except ValueError as exc:
        return P.Decision("deny", str(exc))
    ws = Path(s.workspace)
    denied = hub.blacklist.check("read_file", {"path": str(path)}, ws)
    if denied:
        return denied
    d = P.check_path("read_file", path, hub.teams.effective_mode(s), ws, hub.vault)
    return d if d.how != "auto" else P.Decision("auto")


def _int(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def call_tool(hub, s, name: str, args: dict):
    """她调文档解析工具 (StudioRuntime.call 转过来). 解析要起子进程、会等, 放到线程里."""
    ocr_dir = ENGINE_ARGS.get("ocr_dir")
    if name == "doc_backends":
        rows = [b.as_dict() for b in router.backends(ocr_dir)]
        return {"backends": rows, "cloud": dict(router.CLOUD)}
    if name != "doc_parse":
        raise ValueError("没有这个文档解析工具")
    path = _path(s, args.get("path"))
    conf = hub.cfg.get("docparse") if isinstance(hub.cfg.get("docparse"), dict) else {}
    max_chars = min(TOOL_MAX_CHARS, _int(args.get("max_chars"), TOOL_DEFAULT_CHARS) or TOOL_DEFAULT_CHARS)
    kwargs = dict(start=_int(args.get("start"), 1), count=_int(args.get("count")), char_offset=_int(args.get("char_offset"), 0),
                  max_chars=max_chars, ocr_mode=str(args.get("ocr") or "auto"),
                  ocr_pages=_int(conf.get("ocr_pages"), DP.OCR_PAGES_DEFAULT), ocr_dir=ocr_dir,
                  ocr_cmd=ENGINE_ARGS.get("ocr_cmd"), ocr_timeout_s=ENGINE_ARGS.get("ocr_timeout_s"))
    try:
        return await asyncio.to_thread(router.parse, path, **kwargs)
    except DocParseError as exc:
        raise ValueError(str(exc)) from None
