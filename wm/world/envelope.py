"""事件包封与清单规格. 样板: 主程序不引用, 宿主第 1 步可以替换. 契约第 6 节第 1、3 条.

宿主按清单给插件交来的事件打包封:
  - 来源世界 (= prefix)、感知通道 (sense)、紧急程度 (urgency) 由宿主填, 插件填不了;
  - type 不在清单里 -> 整条丢掉并记审计; fields 里没声明的字段 -> 删掉并记审计 (真值不会因为插件多带一个键就进来);
  - 带了 trusted / as_instruction / is_owner 一类键 -> 整条丢掉 (正文一律当数据, 没有"当指令"的开关);
  - 正文和字段值一律过 outbound.clean_in (去控制字符、零宽字符, 方括号和尖括号换全角), 限长.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from wm.channels.outbound import clean_in

FORBIDDEN_KEYS = {"trusted", "as_instruction", "instruction", "is_owner", "owner", "trust", "trust_tier", "trust_level",
                  "is_master", "verified_owner", "privilege", "role", "permission", "permissions", "auth"}
FIELD_LIMIT = 60
TEXT_LIMIT = 200
MAX_AUDIT = 500
SENSE_LABEL = {"saw": "看见", "heard": "听见", "told": "别人告诉她", "receipt": "回执", "notice": "提示主人"}


class Audit:
    """被丢掉 / 被挡住的东西的记录 (内存里的列表, path 不为空时同时追加到 jsonl)."""

    def __init__(self, path=None):
        self.items: list[dict] = []
        self.path = Path(path) if path else None

    def add(self, kind: str, **detail) -> None:
        item = {"ts": round(time.time(), 3), "kind": kind, **detail}
        self.items.append(item)
        del self.items[:-MAX_AUDIT]
        if self.path:
            try:
                with self.path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def kinds(self) -> list[str]:
        return [i["kind"] for i in self.items]


@dataclass(frozen=True)
class EventDecl:
    type: str
    sense: str
    urgency: str
    fields: tuple


@dataclass(frozen=True)
class ParamDecl:
    name: str
    type: str
    required: bool = False
    max_len: int | None = None
    values: tuple = ()


@dataclass(frozen=True)
class ActionDecl:
    name: str
    kind: str
    outward: bool
    receipt: str | None
    params: tuple
    why: str


@dataclass(frozen=True)
class WorldSpec:
    """从(已经通过校验的)清单里读出来的世界规格. 宿主之后只信它, 不信插件在运行时说的话."""
    id: str
    prefix: str
    kind: str
    events: dict
    actions: dict
    visible: tuple
    hidden: tuple
    leave_s: int

    @staticmethod
    def from_manifest(manifest: dict) -> "WorldSpec":
        w = manifest["plugin"]["world"]
        events = {e["type"]: EventDecl(e["type"], e["sense"], e.get("urgency", "batch"), tuple(e.get("fields", [])))
                  for e in w.get("events", [])}
        actions = {}
        for a in w.get("actions", []):
            params = tuple(ParamDecl(n, p["type"], bool(p.get("required")), p.get("max_len"), tuple(p.get("values", [])))
                           for n, p in (a.get("params") or {}).items())
            actions[a["name"]] = ActionDecl(a["name"], a["kind"], bool(a.get("outward")), a.get("receipt"), params, a["why"])
        per = w.get("perception") or {}
        return WorldSpec(manifest["id"], w["prefix"], w["kind"], events, actions, tuple(per.get("visible", [])),
                         tuple(per.get("hidden", [])), int((w.get("leave") or {}).get("ack_timeout_s", 5)))


@dataclass(frozen=True)
class Envelope:
    seq: int
    source: str
    ts: float
    type: str
    sense: str
    urgency: str
    fields: dict
    text: str = ""


def _value(v) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return clean_in(str(v), FIELD_LIMIT)
    return None


def make_envelope(spec: WorldSpec, frame, seq: int, audit: Audit, now: float | None = None) -> Envelope | None:
    """插件交来的一帧 {type, fields, text} -> 包封; 不合格返回 None 并记审计."""
    if not isinstance(frame, dict):
        audit.add("event_not_object")
        return None
    typ = frame.get("type")
    decl = spec.events.get(typ) if isinstance(typ, str) else None
    if decl is None:
        audit.add("event_undeclared_type", type=str(typ)[:40])
        return None
    raw = frame.get("fields") if isinstance(frame.get("fields"), dict) else {}
    bad = (FORBIDDEN_KEYS & {k for k in frame if isinstance(k, str)}) | (FORBIDDEN_KEYS & {k for k in raw if isinstance(k, str)})
    if bad:
        audit.add("event_forbidden_key", type=typ, keys=sorted(bad))
        return None
    fields: dict = {}
    for k in decl.fields:
        if k not in raw:
            continue
        val = _value(raw[k])
        if val is None:
            audit.add("event_bad_field_value", type=typ, field=k)
        else:
            fields[k] = val
    for k in raw:
        if k not in decl.fields:
            audit.add("event_undeclared_field", type=typ, field=str(k)[:40])
    text = clean_in(frame.get("text") or "", TEXT_LIMIT)
    return Envelope(seq, spec.prefix, round(time.time() if now is None else now, 3), typ, decl.sense, decl.urgency,
                    fields, text)
