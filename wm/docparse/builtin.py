"""第一档: 现有的本机解析器 (wm/agent/material_readers.py, 一行没改). 在带内存和时间限制的子进程里取文字层; 这里只把它的结果
整理成"段": 一段 = PDF 的一页 / 一张幻灯片 / 一张工作表 / 一节 …, 带它自己的名字 (label) 和页码 (page).

段的状态: text (有字) | blank (PDF 里没读出字的页, 可能是扫描图片, 等第二档) | image (整份是一张图片, 等第二档).
整份读不了 (加密、含宏、认不出格式、超限……) 返回 error, 原因用 material_readers 已有的中文原话.
"""
from __future__ import annotations

from pathlib import Path


def _readers():
    from ..agent import material_readers as readers   # 懒导入: 引擎包本身不碰工作台
    return readers


def kind_of(path: Path) -> str:
    readers = _readers()
    with path.open("rb") as f:
        head = f.read(4096)
    return readers.sniff(head, path.suffix) or path.suffix.lower()


def split_sections(result: dict) -> list[dict]:
    """material_readers 的 sections -> 段. 正文是一整块字符串, 每节 [start:end] 切出来, 去掉开头的 [label] 那一行."""
    text = result.get("text") or ""
    readers = _readers()
    parts = []
    for index, sec in enumerate(result.get("sections") or [], 1):
        label = str(sec.get("label") or f"第 {index} 段")
        chunk = text[int(sec.get("start") or 0):int(sec.get("end") or 0)]
        head = f"[{label}]\n"
        if chunk.startswith(head):
            chunk = chunk[len(head):]
        chunk = chunk.strip("\n")
        part = {"n": index, "label": label, "page": sec.get("page"), "state": "text", "how": "text", "text": chunk}
        if sec.get("kind") == "page" and chunk.strip() == readers.EMPTY_PAGE:
            part.update(state="blank", how="", text="")
        parts.append(part)
    return parts


def parse(path: Path) -> dict:
    """返回 {"error": 原因} 或 {"format", "parts", "warnings", "partial", "pages_total"}."""
    readers = _readers()
    kind = kind_of(path)
    result = readers.extract_path(path, kind)
    status = result.get("parse_status")
    fmt = str(result.get("format") or kind.lstrip("."))
    if status == "image":
        return {"format": fmt, "parts": [{"n": 1, "label": "图片", "page": None, "state": "image", "how": "", "text": ""}],
                "warnings": [], "partial": False, "pages_total": 1}
    if status in ("ready", "partial", "needs_ocr"):
        parts = split_sections(result)
        warnings = [w for w in result.get("warnings", []) if "没有可提取文字" not in w and "图片中的文字尚未读取" not in w]
        if not parts and not (result.get("text") or "").strip():
            return {"error": "这个文件里没有可提取的文字", "format": fmt}
        if parts and all(p["state"] == "text" and not p["text"].strip() for p in parts):
            return {"error": "这个文件里没有可提取的文字", "format": fmt}
        if not parts:    # 没有分节的 (理论上不会): 当成一整段
            parts = [{"n": 1, "label": "全文", "page": None, "state": "text", "how": "text", "text": (result.get("text") or "").strip()}]
        return {"format": fmt, "parts": parts, "warnings": warnings, "partial": status == "partial",
                "pages_total": int(result.get("pages") or len(parts))}
    reason = result.get("error") or result.get("reader") or "没有可提取的文字"
    return {"error": str(reason), "format": fmt}
