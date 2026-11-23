"""把流式回复切成可以送 TTS 的分句.

首句门槛低 (默认 4 个字遇到逗号就切), 让第一段声音尽早开始; 后续分句门槛高一些, 减少 TTS 调用次数和断句感.
"""
from __future__ import annotations

import re

STRONG = set("。！？!?；;…\n~～")
WEAK = set("，,、：:")
CLOSERS = set("”’」』）)】》\"'")
_LEADING = "".join(STRONG | WEAK | CLOSERS) + ". \t"
_SPEAKABLE = re.compile(r"[㐀-鿿豈-﫿A-Za-z0-9]")


def speakable_len(text: str) -> int:
    return len(_SPEAKABLE.findall(text))


def speakable_text(text: str) -> str:
    """只留汉字、字母、数字, 用来比较两段识别结果是否说的是同一句话."""
    return "".join(_SPEAKABLE.findall(text)).lower()


class ClauseSplitter:
    def __init__(self, first_min: int = 4, min_chars: int = 10, max_chars: int = 40):
        self.first_min = first_min
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.buf = ""
        self.count = 0

    def _threshold(self) -> int:
        return self.first_min if self.count == 0 else self.min_chars

    def _take(self, end: int, out: list[str]) -> None:
        while end < len(self.buf) and self.buf[end] in CLOSERS:
            end += 1
        clause, self.buf = self.buf[:end].strip(), self.buf[end:]
        # 流式时句末引号可能晚一个 token 才到, 落在下一句开头; 开头的标点对朗读没有意义, 去掉.
        clause = clause.lstrip(_LEADING)
        if speakable_len(clause):
            out.append(clause)
            self.count += 1

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        i = 0
        while i < len(self.buf):
            ch = self.buf[i]
            head = self.buf[:i + 1]
            # 半角句点当句末, 但 "3.5" 这种数字里的小数点不算.
            period = ch == "." and not (i > 0 and self.buf[i - 1].isdigit())
            if (ch in STRONG or period) and speakable_len(head) >= 1:
                # 省略号、连续问叹号等连写的标点要整体留在本句.
                j = i + 1
                while j < len(self.buf) and self.buf[j] in STRONG:
                    j += 1
                if j == len(self.buf) and ch in "…~～.":
                    break  # 可能还没写完, 等下一段
                self._take(j, out)
                i = 0
                continue
            if ch in WEAK and speakable_len(head) >= self._threshold():
                self._take(i + 1, out)
                i = 0
                continue
            if speakable_len(head) >= self.max_chars:
                cut = max(head.rfind(" "), head.rfind("　"))
                self._take(cut + 1 if cut > 0 else i + 1, out)
                i = 0
                continue
            i += 1
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        if self.buf:
            self._take(len(self.buf), out)
        self.buf = ""
        return out
