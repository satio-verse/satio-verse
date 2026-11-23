"""parse(): 三档路由. 先第一档 (文字层); 读不出字的页 / 图片才进第二档 (本机识字, 要装好组件); 第三档 (云端) 路由里没有.

规矩:
- 有文字层的照旧只走第一档, 不识字.
- 读不了就明说"读不了"和为什么, 不改走云端, 也不引导去云端 (结果里除了固定的 cloud 那一句, 没有"上传""云端帮你"这类话).
- 识字只识这一次要给她看的那一段范围里的页, 最多 ocr_pages 页; 超出的进"没读", 让她 start= 接着读.
- 识字失败 (没装、超时、超内存、图片解不开) 的页进"没读", 不重试、不抛.
同步函数 (起子进程会等): 工作台这边用 asyncio.to_thread 调.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import (CLOUD_NOTE, DEFAULT_MAX_CHARS, MAX_CHARS_CAP, OCR_DIR, OCR_PAGES_CAP, OCR_PAGES_DEFAULT, UNREAD_SHOWN, BackendInfo,
               DocParseError, builtin, cloud, extra_backends, ocr)

CLOUD = {"used": False, "note": CLOUD_NOTE}


def builtin_info() -> BackendInfo:
    return BackendInfo(id="builtin-text", name="本机文字层（内置）", kind="builtin", upload=False, auto=True,
                       detail="Word、Excel、PowerPoint、PDF 文字层、OpenDocument、RTF、EPUB、文本和代码、压缩包里的文档等；"
                              "文件最大 32 MB（压缩包内 128 MB）、PDF 200 页、30 万字符。")


def backends(ocr_dir=None) -> list[BackendInfo]:
    """doc_backends 列的: 三档 + 额外登记的. 只是列出来, 路由只会调前两档."""
    out = [builtin_info(), ocr.info(ocr_dir), cloud.INFO]
    out.extend(b.info for b in extra_backends() if b.info.id not in {x.id for x in out})
    return out


def _clamp(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _unread(part: dict, reason: str) -> dict:
    part.update(state="unread", reason=reason)
    return part


def _run_ocr(path: Path, parts: list[dict], start: int, count, ocr_pages: int, ocr_dir, cmd, timeout_s) -> bool:
    """给范围里读不出字的页 / 图片识字. 就地改 parts. 返回有没有真的起过识字进程."""
    window_end = start + count if count else None
    cands = [p for p in parts if p["state"] in ("blank", "image") and p["n"] >= start and (window_end is None or p["n"] < window_end)]
    if not cands:
        return False
    st = ocr.info(ocr_dir)
    if not st.available:
        for p in cands:
            what = "这是一张图片" if p["state"] == "image" else "这页没有文字层，可能是扫描图片"
            _unread(p, f"{what}；{st.reason_unavailable}")
        return False
    chosen, rest = cands[:ocr_pages], cands[ocr_pages:]
    for p in rest:
        _unread(p, f"这次最多识 {ocr_pages} 页，这页没识；用 start={p['n']} 接着读")
    if chosen[0]["state"] == "image":
        job = {"op": "ocr_image", "path": str(path.resolve()), "ocr_dir": str(ocr_dir)}
    else:
        job = {"op": "ocr_pdf", "path": str(path.resolve()), "pages": [p["page"] or p["n"] for p in chosen], "ocr_dir": str(ocr_dir)}
    got = ocr.run_ocr(job, cmd=cmd, timeout_s=timeout_s)
    if not got.get("ok"):
        why = str(got.get("error") or "识字没成")
        for p in chosen:
            _unread(p, f"识字没成：{why}")
        return True
    by_page = {r.get("page"): r for r in got.get("pages", []) if isinstance(r, dict)}
    for p in chosen:
        r = by_page.get(p["page"] or p["n"])
        if r and isinstance(r.get("text"), str) and r["text"].strip():
            p.update(state="text", how="ocr", text=r["text"], conf=r.get("conf"))
        else:
            _unread(p, str((r or {}).get("error") or "识字没有给出这页的结果"))
    return True


def parse(path, *, start=1, count=None, char_offset=0, max_chars=DEFAULT_MAX_CHARS, ocr_mode="auto", ocr_pages=OCR_PAGES_DEFAULT,
          ocr_dir=None, ocr_cmd=None, ocr_timeout_s=None) -> dict:
    path = Path(path)
    if not path.is_file():
        raise DocParseError(f"没有这个文件: {path}")
    start = _clamp(start, 1, 10**9, 1)
    count = _clamp(count, 1, 10**9, 0) or None
    char_offset = _clamp(char_offset, 0, 10**9, 0)
    max_chars = _clamp(max_chars, 1000, MAX_CHARS_CAP, DEFAULT_MAX_CHARS)
    ocr_pages = _clamp(ocr_pages, 1, OCR_PAGES_CAP, OCR_PAGES_DEFAULT)
    mode = "never" if ocr_mode == "never" else "auto"
    ocr_dir = Path(ocr_dir) if ocr_dir else OCR_DIR

    base = {"file": str(path), "backend": "builtin", "cloud": dict(CLOUD)}
    got = builtin.parse(path)
    if "error" in got:
        return {**base, "status": "unreadable", "format": got.get("format", ""), "parts_total": 0, "start": start, "returned": 0,
                "parts": [], "unread": [{"n": 0, "label": "整份文件", "reason": got["error"]}], "next": "", "warnings": []}
    parts, fmt = got["parts"], got["format"]
    total = len(parts)
    if start > total:
        raise DocParseError(f"start 超出：这份文件一共 {total} 段")

    ran = False
    if mode == "never":
        for p in parts:
            if p["state"] in ("blank", "image") and p["n"] >= start and (count is None or p["n"] < start + count):
                _unread(p, "按要求没有识字")
    else:
        ran = _run_ocr(path, parts, start, count, ocr_pages, ocr_dir, ocr_cmd, ocr_timeout_s)

    out_parts, unread, used = [], [], 0
    end = total if count is None else min(total, start - 1 + count)
    i, cut_hint = start - 1, ""
    while i < end:
        p = parts[i]
        if p["state"] != "text":
            unread.append({"n": p["n"], "label": p["label"], "reason": p.get("reason") or "没有读出文字"})
            i += 1
            continue
        offset = char_offset if i == start - 1 else 0
        text = p["text"][offset:]
        item = _out(p, text)
        size = _size(item)
        if used + size <= max_chars:
            out_parts.append(item)
            used += size
            i += 1
            continue
        if not out_parts:    # 一段就比整个预算还长: 只能切开, 记下切到哪
            keep = _fit(text, max_chars - _size({**_out(p, ""), "cut": True}))
            out_parts.append({**_out(p, keep), "cut": True})
            cut_hint = f"这一段还没读完：start={p['n']}, char_offset={offset + len(keep)} 接着读"
        break
    nxt = cut_hint or (f"还有内容：start={i + 1} 接着读" if i < total else "")
    if len(unread) > UNREAD_SHOWN:
        extra = len(unread) - UNREAD_SHOWN
        unread = unread[:UNREAD_SHOWN] + [{"n": 0, "label": "其余", "reason": f"另有 {extra} 处同样没读"}]
    status = "unreadable" if not out_parts else ("partial" if unread or got.get("partial") else "ready")
    return {**base, "status": status, "format": fmt, "parts_total": total, "start": start, "returned": len(out_parts),
            "backend": "builtin+local-ocr" if ran else "builtin", "parts": out_parts, "unread": unread, "next": nxt,
            "warnings": list(got.get("warnings", [])) + (["识字只认字，表格、公式、双栏顺序不保证。"] if any(x.get("how") == "ocr" for x in out_parts) else [])}


def _size(item: dict) -> int:
    """这一段放进结果 JSON 以后占多少字 (含引号、换行的转义和自己的名字、页码这些). 预算按它算, 不按原文字数算:
    工作台把超过 3 万字的工具结果换成节选, 转义多的文档 (表格、代码) 按原文字数算会超."""
    return len(json.dumps(item, ensure_ascii=False)) + 2


def _fit(text: str, room: int) -> str:
    """text 的最长开头, 使它放进 JSON 以后不超过 room."""
    def cost(t: str) -> int:
        return len(json.dumps(t, ensure_ascii=False)) - 2
    if cost(text) <= room:
        return text
    lo, hi = 0, min(len(text), max(0, room))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if cost(text[:mid]) <= room else (lo, mid - 1)
    return text[:lo]


def _out(p: dict, text: str) -> dict:
    item = {"n": p["n"], "label": p["label"], "how": p["how"], "text": text}
    if p.get("page"):
        item["page"] = p["page"]
    if p.get("conf") is not None:
        item["conf"] = p["conf"]
    return item
