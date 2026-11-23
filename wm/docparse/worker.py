"""识字子进程 (一次性, 父进程限内存、限时间: ocr.run_ocr). 只用标准库开头; pypdf / Pillow / RapidOCR 都在用到时才导入.

协议: 标准输入给一行 JSON, 标准输出回一行 JSON (只有 ASCII). 库往标准输出打的杂音会被改道到标准错误.
  {"op": "ocr_pdf", "path": ..., "pages": [2, 3], "ocr_dir": ...}   识 PDF 的这几页 (页码从 1 算)
  {"op": "ocr_image", "path": ..., "ocr_dir": ...}                  识一张图片
  回: {"ok": true, "engine": ..., "pages": [{"page": 2, "text": ..., "conf": 0.93, "lines": 12} | {"page": 3, "error": "..."}]}
      {"ok": false, "code": "component-missing" | "bad-job" | "failed", "error": "..."}

识字器是可换的: serve(factory) 里 factory(job) 返回一个有 name、version、recognize(PIL 图) -> [(文字, 框, 分数)] 的对象. 正式的是
RapidOCR (下面 rapid_factory); 测试里 tests/docparse_stub_worker.py 用同一套取图、拼字、回结果的代码, 只把识字器换成假的.

PDF 扫描页取图: pypdf 的 page.images, 取面积最大的一张 (扫描件通常一页一张整图), 不用 PyMuPDF (AGPL). 页上没有嵌入图时,
装了 pypdfium2 (可选) 就渲染, 没装就说"没有可识字的图片".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_PIXELS = 60_000_000      # 一张图最多多少像素, 再大就不识
MAX_PAGES = 30
VERSION = 1


class ComponentMissing(Exception):
    pass


def lines_to_text(lines) -> tuple[str, float]:
    """识字器给的 (文字, 框, 分数) 排成阅读顺序: 按行中心的纵坐标分行, 同一行按左边界排. 返回 (文字, 平均分数)."""
    items = []
    for text, box, score in lines:
        text = str(text or "").strip()
        if not text:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        except (TypeError, ValueError, IndexError):
            x0 = x1 = y0 = y1 = 0.0
        try:
            sc = float(score)
        except (TypeError, ValueError):
            sc = 0.0
        items.append((text, x0, x1, y0, y1, sc))
    if not items:
        return "", 0.0
    heights = sorted(max(1.0, it[4] - it[3]) for it in items)
    half = heights[len(heights) // 2] / 2
    items.sort(key=lambda it: ((it[3] + it[4]) / 2, it[1]))
    rows: list[list] = []
    for it in items:
        mid = (it[3] + it[4]) / 2
        if rows and abs(mid - rows[-1][0]) <= half:
            rows[-1][1].append(it)
        else:
            rows.append([mid, [it]])
    text = "\n".join(" ".join(x[0] for x in sorted(r[1], key=lambda it: it[1])) for r in rows)
    return text, sum(it[5] for it in items) / len(items)


def _pil_ready(img):
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("图片太大，不识")
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    return img


def _largest_image(page):
    best, area = None, 0
    for im in page.images:
        pil = im.image
        a = pil.width * pil.height
        if a > area:
            best, area = pil, a
    return best


def _render_with_pdfium(path: str, index: int):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    try:
        return doc[index].render(scale=2.0).to_pil()
    finally:
        doc.close()


def _recognize(rec, img) -> dict:
    img = _pil_ready(img)
    text, conf = lines_to_text(rec.recognize(img))
    if not text.strip():
        return {"error": "识字没认出文字"}
    return {"text": text, "conf": round(conf, 3), "lines": text.count("\n") + 1}


def handle(job: dict, factory) -> dict:
    op = job.get("op")
    if op not in ("ocr_pdf", "ocr_image") or not isinstance(job.get("path"), str):
        return {"ok": False, "code": "bad-job", "error": "任务不对"}
    try:
        rec = factory(job)
    except ComponentMissing as exc:
        return {"ok": False, "code": "component-missing", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "failed", "error": f"识字器起不来（{type(exc).__name__}）"}
    out = {"ok": True, "engine": f"{rec.name} {rec.version}", "pages": []}
    try:
        if op == "ocr_image":
            from PIL import Image
            with Image.open(job["path"]) as im:
                im.load()
                got = _recognize(rec, im)
            out["pages"].append({"page": 1, **got})
            return out
        from pypdf import PdfReader
        reader = PdfReader(job["path"], strict=False)
        if reader.is_encrypted:
            return {"ok": False, "code": "failed", "error": "PDF 已加密，不读"}
        total = len(reader.pages)
        pages = [p for p in job.get("pages", []) if isinstance(p, int)][:MAX_PAGES]
        for number in pages:
            if not 1 <= number <= total:
                out["pages"].append({"page": number, "error": "页码超出"})
                continue
            try:
                img = _largest_image(reader.pages[number - 1])
                if img is None:
                    try:
                        img = _render_with_pdfium(job["path"], number - 1)
                    except ImportError:
                        out["pages"].append({"page": number, "error": "这页没有可识字的图片（要有渲染组件才能识）"})
                        continue
                out["pages"].append({"page": number, **_recognize(rec, img)})
            except Exception as exc:  # noqa: BLE001
                out["pages"].append({"page": number, "error": f"这页的图片本机解不开或识不了（{type(exc).__name__}）"})
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "failed", "error": f"文件读不了（{type(exc).__name__}）"}


def serve(factory) -> int:
    real = sys.stdout
    sys.stdout = sys.stderr          # 库的杂音不许混进结果
    try:
        raw = sys.stdin.buffer.read()
        try:
            job = json.loads(raw.decode("utf-8"))
            if not isinstance(job, dict):
                raise ValueError("not an object")
        except ValueError:
            result = {"ok": False, "code": "bad-job", "error": "任务不是 JSON"}
        else:
            result = handle(job, factory)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "code": "failed", "error": f"识字进程出错（{type(exc).__name__}）"}
    real.write(json.dumps(result, ensure_ascii=True))
    real.write("\n")
    real.flush()
    return 0


class RapidOcrRecognizer:
    """RapidOCR (rapidocr-onnxruntime 1.4.4; 轮子里带 PP-OCRv4 三个模型, 装完离线可用). 这一轮没装、没联调; 代码照它的文档写."""
    name = "rapidocr-onnxruntime"

    def __init__(self, ocr_dir):
        site = Path(ocr_dir) / "site"
        if not site.is_dir():
            raise ComponentMissing("本机识字组件（RapidOCR）还没装")
        sys.path.insert(0, str(site))
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise ComponentMissing(f"本机识字组件装得不完整（导入失败: {exc}）") from None
        try:
            from importlib.metadata import version
            self.version = version("rapidocr_onnxruntime")
        except Exception:  # noqa: BLE001
            self.version = "?"
        self._engine = RapidOCR()

    def recognize(self, img):
        import numpy as np
        result, _ = self._engine(np.asarray(img))
        out = []
        for item in result or []:
            box, text, score = item[0], item[1], item[2]
            out.append((text, box, score))
        return out


def rapid_factory(job):
    return RapidOcrRecognizer(job.get("ocr_dir") or "")


if __name__ == "__main__":
    sys.exit(serve(rapid_factory))
