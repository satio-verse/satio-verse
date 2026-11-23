"""阅读面板 (工具1 之二, 边读边问) 的服务端: 把资料本里一份资料的块按页 / 按节排好给界面画, 并把用户选中发给她的块登记成
"这个会话里查到过的出处" (不然她引用时会被 citation_check 打回). 只读资料本已经抽好的块, 不联网、不起子进程、不碰原文件.

借用了 NotebookStore 的私有成员 (_load / _chunks / _chunk): 资料本改版 (块的存法、位置字段) 时这里要跟着改.
"""
from __future__ import annotations

from . import notebook as NB

MAX_QUOTE_KEYS = 40
UNIT_CHARS = 2500            # 没有页 / 节的资料 (文字、网页、Word 段落) 攒到多少字算一个单元
MAX_FILL = 400               # PDF 中间 / 开头没读出字的页最多补多少个空单元
UNREADABLE = {
    "needs_ocr": "没读出文字（可能是扫描件，要 OCR；还没读）",
    "garbled": "读出来的字是乱码（PDF 的字体没带文字对照表），要 OCR 才能读；还没读",
    "image": "是图片，资料本暂时只收文字",
    "unsupported": "这种格式还没有本地解析器，没读",
    "unavailable": "本机缺解析组件，没读",
    "error": "没能读出来",
    "empty": "没有可提取的文字",
}
_KEYED = (("page", "第 {} 页"), ("slide", "幻灯片 {}"), ("chapter", "第 {} 节"), ("table", "表格 {}"))
_NO_TEXT = "这一页没读出文字（可能是扫描图片）"


def _unit_key(loc: dict):
    for kind, _ in _KEYED:
        if kind in loc:
            return kind, loc[kind]
    if "sheet" in loc:
        return "sheet", loc["sheet"]
    return None


def _label(key) -> str:
    kind, value = key
    if kind == "sheet":
        return f"工作表「{value}」"
    return dict(_KEYED)[kind].format(value)


def _block_label(blocks: list[dict]) -> str:
    """没有页 / 节的单元起个名字: 一块就用它的位置; 几块并起来写区间 (段落 12–40 / 第 1–80 行)."""
    first, last = blocks[0]["loc"], blocks[-1]["loc"]
    if len(blocks) == 1:
        return NB.loc_label(first)
    for field, unit in (("para", "段落"), ("cells", "单元格")):
        if field in first and field in last:
            a, b = first[field][0], last[field][-1]
            return f"{unit} {a}–{b}" if a != b else f"{unit} {a}"
    if "lines" in first and "lines" in last:
        a, b = first["lines"][0], last["lines"][-1]
        head = f"「{first['heading']}」" if first.get("heading") else ""
        return f"{head}第 {a}–{b} 行"
    return f"{NB.loc_label(first)} 起（{len(blocks)} 块）"


def _block(no: int, chunk: dict) -> dict:
    loc = chunk.get("loc") or {}
    return {"key": f"S{no}-{chunk.get('k')}", "label": NB.loc_label(loc), "text": str(chunk.get("text") or "")}


def view(store, nid: str, no: int) -> dict:
    """一份资料的阅读视图 (见计划 §3.1). 资料号不存在抛 ValueError."""
    nb = store._load(nid)
    src = next((s for s in nb.get("sources") or [] if s.get("no") == int(no)), None)
    if src is None:
        raise ValueError("没有这份资料")
    status, reader = str(src.get("status") or ""), str(src.get("reader") or "")
    out = {"notebook": {"id": nid, "title": nb.get("title") or "资料本"},
           "source": {"no": src["no"], "name": src.get("name") or "", "kind": src.get("kind") or "", "format": src.get("format") or "",
                      "status": status, "reader": reader, "url": src.get("url") or "",
                      "pdf": src.get("kind") == "file" and str(src.get("format") or "").lower() == "pdf"},
           "readable": False, "reason": "", "note": "", "unit_kind": "block", "units": [], "total_chars": 0}
    chunks = [c for c in store._chunks(nid, src["no"]) if isinstance(c, dict) and c.get("k")]
    if status not in ("ready", "partial") or not chunks:
        out["reason"] = reader or UNREADABLE.get(status) or "资料里没有可读的文字"
        return out
    units: list[dict] = []
    cur_key, cur_blocks = None, []

    def flush():
        if not cur_blocks:
            return
        if cur_key:
            units.append({"label": _label(cur_key), "kind": cur_key[0], "value": cur_key[1], "blocks": list(cur_blocks)})
        else:
            units.append({"label": _block_label([b["_c"] for b in cur_blocks]), "kind": "block", "value": None, "blocks": list(cur_blocks)})

    size = 0
    for chunk in chunks:
        loc = chunk.get("loc") or {}
        key = _unit_key(loc)
        block = {**_block(src["no"], chunk), "_c": chunk}
        same = (key is not None and key == cur_key) or (key is None and cur_key is None and cur_blocks and size < UNIT_CHARS)
        if not same:
            flush()
            cur_key, cur_blocks, size = key, [], 0
        cur_blocks.append(block)
        size += len(block["text"])
    flush()
    result, filled = [], 0
    prev_page = 0
    for unit in units:
        if unit["kind"] == "page" and isinstance(unit["value"], int):
            for page in range(prev_page + 1, unit["value"]):  # 夹在中间(或开头)没读出字的页补空单元
                if filled >= MAX_FILL:
                    break
                result.append({"label": f"第 {page} 页", "page": page, "empty": True, "note": _NO_TEXT, "blocks": []})
                filled += 1
            prev_page = max(prev_page, unit["value"])
        row = {"label": unit["label"], "empty": False, "note": "",
               "blocks": [{k: v for k, v in b.items() if k != "_c"} for b in unit["blocks"]]}
        if unit["kind"] == "page":
            row["page"] = unit["value"]
        result.append(row)
    for n, row in enumerate(result, 1):
        row["n"] = n
    kinds = {u["kind"] for u in units}
    out.update({"readable": True, "note": reader, "units": result, "unit_kind": kinds.pop() if len(kinds) == 1 else "block",
                "total_chars": sum(len(b["text"]) for u in result for b in u["blocks"])})
    return out


def quote(store, sid: str, nid: str, keys) -> dict:
    """用户在面板里点了"问": 这几块算这个会话里查到过的 (她引用不会被打回). 编号不在这本里整个拒绝."""
    if not isinstance(keys, (list, tuple)) or not keys:
        raise ValueError("要给出处编号")
    clean: list[str] = []
    for raw in keys[:MAX_QUOTE_KEYS + 1]:
        m = NB.KEY.fullmatch(str(raw or "").strip())
        if not m:
            raise ValueError("出处编号要像 S3-17 这样")
        key = f"S{int(m.group(1))}-{int(m.group(2))}"
        if key not in clean:
            clean.append(key)
    if len(clean) > MAX_QUOTE_KEYS:
        raise ValueError(f"一次最多 {MAX_QUOTE_KEYS} 块")
    store._load(nid)
    labels = {}
    for key in clean:
        chunk = store._chunk(nid, key)
        if chunk is None:
            raise ValueError(f"资料本里没有 {key} 这一块")
        labels[key] = NB.loc_label(chunk.get("loc") or {})
    store.remember(sid, nid, clean)
    return {"keys": clean, "labels": labels}

