"""顶层 JSON 对象的增量解析器.

LLM 每吐出一段文本就 feed 一次, 解析器返回按出现顺序排列的事件:
  ("value", key, obj)   某个顶层字段的值完整闭合
  ("delta", key, text)  stream_keys 里的字符串字段新增的(已解码)文本
  ("done", obj)         整个顶层对象闭合
只关心顶层: 嵌套的数组/对象整体攒好后一次 json.loads.
"""
from __future__ import annotations

import json
import re

_WS = " \t\r\n"
_TRAILING_UNICODE = re.compile(r"\\u[0-9a-fA-F]{0,3}$")
_TRAILING_HIGH_SURROGATE = re.compile(r"\\u[dD][89abAB][0-9a-fA-F]{2}$")


def _safe_prefix(raw: str) -> str:
    """去掉末尾不完整的转义, 使 '"' + prefix + '"' 一定能被 json.loads 解码."""
    trailing = len(raw) - len(raw.rstrip("\\"))
    if trailing % 2 == 1:  # 末尾是一个还没写完的转义开头
        raw = raw[:-1]
    m = _TRAILING_UNICODE.search(raw)
    if m and not _odd_backslashes_before(raw, m.start()):
        raw = raw[:m.start()]
    m = _TRAILING_HIGH_SURROGATE.search(raw)
    if m and not _odd_backslashes_before(raw, m.start()):  # 等低位代理到齐再一起解码
        raw = raw[:m.start()]
    return raw


def _odd_backslashes_before(raw: str, index: int) -> bool:
    """index 处的反斜杠本身是否被转义 (前面有奇数个反斜杠)."""
    n = 0
    i = index - 1
    while i >= 0 and raw[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1


class JSONObjectStream:
    def __init__(self, stream_keys=("reply",)):
        self.stream_keys = set(stream_keys)
        self.result: dict = {}
        self.done = False
        self._phase = "start"
        self._key_raw = ""
        self._key = None
        self._raw = ""          # 当前值的原始文本
        self._depth = 0         # 嵌套值内的括号深度
        self._in_str = False    # 嵌套值内是否在字符串里
        self._esc = False
        self._emitted = ""      # 流式字符串已发出的解码文本

    def feed(self, text: str) -> list[tuple]:
        events: list[tuple] = []
        for ch in text:
            if self.done:
                break
            self._step(ch, events)
        if self._phase == "string" and self._key in self.stream_keys:
            self._emit_delta(events)
        return events

    # -- 内部 --------------------------------------------------------------
    def _emit_delta(self, events: list) -> None:
        decoded = json.loads('"' + _safe_prefix(self._raw[1:]) + '"')
        if len(decoded) > len(self._emitted):
            events.append(("delta", self._key, decoded[len(self._emitted):]))
            self._emitted = decoded

    def _finish_value(self, events: list) -> None:
        value = json.loads(self._raw)
        if isinstance(value, str) and self._key in self.stream_keys and len(value) > len(self._emitted):
            events.append(("delta", self._key, value[len(self._emitted):]))
        self.result[self._key] = value
        events.append(("value", self._key, value))
        self._raw = ""
        self._emitted = ""

    def _close(self, events: list) -> None:
        self.done = True
        self._phase = "closed"
        events.append(("done", self.result))

    def _step(self, ch: str, events: list) -> None:
        phase = self._phase
        if phase == "start":
            if ch == "{":
                self._phase = "key_or_end"
            return
        if phase == "key_or_end":
            if ch == '"':
                self._phase, self._key_raw, self._esc = "key", "", False
            elif ch == "}":
                self._close(events)
            return
        if phase == "key":
            if self._esc:
                self._esc = False
                self._key_raw += ch
            elif ch == "\\":
                self._esc = True
                self._key_raw += ch
            elif ch == '"':
                self._key = json.loads('"' + self._key_raw + '"')
                self._phase = "colon"
            else:
                self._key_raw += ch
            return
        if phase == "colon":
            if ch == ":":
                self._phase = "value_start"
            return
        if phase == "value_start":
            if ch in _WS:
                return
            self._raw = ch
            if ch == '"':
                self._phase, self._esc, self._emitted = "string", False, ""
            elif ch in "{[":
                self._phase, self._depth, self._in_str, self._esc = "container", 1, False, False
            else:
                self._phase = "primitive"
            return
        if phase == "string":
            self._raw += ch
            if self._esc:
                self._esc = False
            elif ch == "\\":
                self._esc = True
            elif ch == '"':
                self._finish_value(events)
                self._phase = "after_value"
            return
        if phase == "container":
            self._raw += ch
            if self._in_str:
                if self._esc:
                    self._esc = False
                elif ch == "\\":
                    self._esc = True
                elif ch == '"':
                    self._in_str = False
            elif ch == '"':
                self._in_str = True
            elif ch in "{[":
                self._depth += 1
            elif ch in "}]":
                self._depth -= 1
                if self._depth == 0:
                    self._finish_value(events)
                    self._phase = "after_value"
            return
        if phase == "primitive":
            if ch in _WS or ch in ",}":
                self._raw = self._raw.strip()
                self._finish_value(events)
                if ch == ",":
                    self._phase = "key_or_end"
                elif ch == "}":
                    self._close(events)
                else:
                    self._phase = "after_value"
            else:
                self._raw += ch
            return
        if phase == "after_value":
            if ch == ",":
                self._phase = "key_or_end"
            elif ch == "}":
                self._close(events)
