"""场景图: 前端渲染、寻路和 LLM 的空间目标共用 web/scenes/*.json.

服务端只关心目标物体的编号和中文名: 它们进 JSON Schema 的枚举 (约束解码只能从里面选) 和系统提示词.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCENE = ROOT / "web" / "scenes" / "room.json"


def load_scene(path: Path | str = DEFAULT_SCENE) -> dict:
    scene = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [o["id"] for o in scene.get("objects", [])]
    if len(ids) != len(set(ids)) or not all(i.isascii() and i.isidentifier() for i in ids):
        raise ValueError(f"场景物体编号必须唯一且是英文标识符: {ids}")
    return scene


def capabilities(scene: dict) -> dict[str, list[str]]:
    """能坐的 (seat), 能拿起的 (item), 能放东西的 (place) 各是哪些物体."""
    objs = scene.get("objects", [])
    return {"seat": [o["id"] for o in objs if o.get("seat")], "item": [o["id"] for o in objs if o.get("item")],
            "place": [o["id"] for o in objs if o.get("place")]}


def targets(scene: dict) -> dict[str, str]:
    """编号 -> 提示词里的中文说明 (名字加别名)."""
    out = {}
    for o in scene.get("objects", []):
        names = [o.get("name", o["id"]), *o.get("aliases", [])]
        out[o["id"]] = "/".join(dict.fromkeys(names))
    return out
