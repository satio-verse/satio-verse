"""她知道的小窝布局 (公开的地图): 能站的几处、每处有哪些台面和抽屉、东西叫什么. 不含"东西现在在哪", 那是世界的真值
(无界面实验在 roomworld.py, 以后在桌宠页面). 她住在这里, 家具、抽屉在哪是知道的; 台面上、抽屉里有什么要自己去看.

编号和名字取自真小窝: desktop/web/room-scenes/petal-oak-life-v1.json (站位、台面) 和
desktop/third_party/rooms/petal-oak-life-v1/room_interactions.json (抽屉、杯子); check_scene() 核对两边对得上.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "desktop/web/room-scenes/petal-oak-life-v1.json"
INTERACTIONS = ROOT / "desktop/third_party/rooms/petal-oak-life-v1/room_interactions.json"

# 能站的几处: 编号 -> (名字, 站位 [x, z]; 取自场景表的 approach, 咖啡角是 coffee.stand)
ZONES = {
    "coffee": ("咖啡角", (2.15, -1.24)),
    "fridge": ("冰箱前", (1.78, -1.12)),
    "desk": ("电脑桌前", (0.95, -1.15)),
    "bed": ("床边", (-0.62, -0.95)),
    "reading": ("阅读角", (-1.6, 2.12)),
    "yoga": ("瑜伽区", (1.65, 1.0)),
    "window": ("窗边", (-2.3, 0.2)),
}
# 放东西的地方: 编号 -> (名字, 在哪一处, 要不要打开才看得到里面). 同一处里先台面后抽屉 (找东西时由近到远也按这个顺序)
REGIONS = {
    "coffee_machine": ("咖啡机上", "coffee", False),
    "water_caddy": ("推车上", "coffee", False),
    "IR_Drawer_Coffee_Upper": ("咖啡工具抽屉", "coffee", True),
    "IR_Drawer_Coffee_Lower": ("咖啡备用用品抽屉", "coffee", True),
    "fridge": ("冰箱里", "fridge", True),
    "surf_desk": ("电脑桌上", "desk", False),
    "IR_Drawer_Desk": ("电脑桌文具抽屉", "desk", True),
    "surf_bed": ("床上", "bed", False),
    "nightstand": ("床头柜上", "bed", False),
    "IR_Drawer_Bedside_Upper": ("床头上层抽屉", "bed", True),
    "IR_Drawer_Bedside_Lower": ("床头下层抽屉", "bed", True),
    "surf_table": ("小边桌上", "reading", False),
    "surf_chair": ("阅读椅上", "reading", False),
    "surf_yoga": ("瑜伽区的地上", "yoga", False),
}
# 东西: 编号 -> (名字, 种类); 经验按种类记 ("杯子不在原处时常在哪找到")
ITEMS = {
    "IR_Item_Espresso_Pink": ("粉色浓缩咖啡杯", "cup"),
    "IR_Item_Espresso_White": ("白色浓缩咖啡杯", "cup"),
    "IR_Item_Mug_0": ("奶油白马克杯", "cup"),
    "IR_Item_Mug_1": ("雾粉马克杯", "cup"),
}
KINDS = {"cup": "杯子"}
# 场景表里的编号 (台面是 objects, 床头柜、推车是 decor) 和交互表里的编号 (抽屉、冰箱门、杯子); 咖啡机上不是单独的物体
_SCENE_IDS = {"surf_desk", "surf_bed", "surf_table", "surf_chair", "surf_yoga", "nightstand", "water_caddy"}
_INTERACTION_IDS = {"IR_Drawer_Coffee_Upper", "IR_Drawer_Coffee_Lower", "IR_Drawer_Desk", "IR_Drawer_Bedside_Upper",
                    "IR_Drawer_Bedside_Lower", "Fridge_Door_Hinge", *ITEMS}


def zone_of(region: str) -> str:
    return REGIONS[region][1]


def is_box(region: str) -> bool:
    """要打开才看得到里面 (抽屉、冰箱)."""
    return REGIONS[region][2]


def dist(a: str, b: str) -> float:
    """两处站位之间的直线距离 (米)."""
    (ax, az), (bx, bz) = ZONES[a][1], ZONES[b][1]
    return math.hypot(ax - bx, az - bz)


def zone_name(z: str) -> str:
    return ZONES[z][0] if z in ZONES else "房间里"


def region_name(r: str | None) -> str:
    return REGIONS[r][0] if r in REGIONS else "不知道哪里"


def item_name(i: str) -> str:
    return ITEMS[i][0] if i in ITEMS else i


def kind_of(i: str) -> str:
    return ITEMS[i][1] if i in ITEMS else "thing"


def check_scene(scene: Path = SCENE, interactions: Path = INTERACTIONS) -> list[str]:
    """和真小窝的两份文件对一遍: 编号都在、站位和场景表一样、抽屉和杯子的名字和交互表一样. 返回对不上的地方 (空 = 都对)."""
    problems = []
    s = json.loads(scene.read_text(encoding="utf-8"))
    ids = {o["id"]: o for o in s.get("objects", [])} | {d["id"]: d for d in s.get("decor", [])}
    problems += [f"场景表里没有 {i}" for i in sorted(_SCENE_IDS - set(ids))]
    stands = {"coffee": tuple(s["coffee"]["stand"]), "fridge": tuple(ids["fridge"]["approach"]),
              "desk": tuple(ids["surf_desk"]["approach"]), "bed": tuple(ids["bed_side"]["approach"]),
              "reading": tuple(ids["reading_chair"]["approach"]), "yoga": tuple(ids["yoga"]["approach"]),
              "window": tuple(ids["window"]["approach"])}
    problems += [f"{z} 的站位 {ZONES[z][1]} 和场景表 {p} 不一样" for z, p in stands.items() if ZONES[z][1] != p]
    labels = {it["id"]: it.get("label", "") for it in json.loads(interactions.read_text(encoding="utf-8")).get("items", [])}
    problems += [f"交互表里没有 {i}" for i in sorted(_INTERACTION_IDS - set(labels))]
    for r, (name, _, box) in REGIONS.items():
        if box and r in labels and labels[r] != name:
            problems.append(f"{r} 在交互表里叫 {labels[r]}, 这里叫 {name}")
    for i, (name, _) in ITEMS.items():  # 交互表是 "马克杯 · 奶油白", 这里说成 "奶油白马克杯"
        if i in labels and "".join(reversed(labels[i].split(" · "))) != name:
            problems.append(f"{i} 在交互表里叫 {labels[i]}, 这里叫 {name}")
    return problems


DEFAULT_SCENE_ID = "petal-oak-life-v1"
# Scene IDs are application-owned, not filenames supplied by a browser.
SCENE_PROFILES = {"casa-luce-v8": ROOT / "desktop/web/room-scenes/casa-luce-v8.json"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # GLB display-name keys may contain .001.


@dataclass(frozen=True)
class Layout:
    """Immutable public map; never contains current objects, quantities or history."""
    scene_id: str
    ZONES: Mapping
    REGIONS: Mapping
    ITEMS: Mapping
    KINDS: Mapping
    favorite: str

    def zone_of(self, region):
        return self.REGIONS[region][1]

    def is_box(self, region):
        return self.REGIONS[region][2]

    def dist(self, a, b):
        (ax, az), (bx, bz) = self.ZONES[a][1], self.ZONES[b][1]
        return math.hypot(ax - bx, az - bz)

    def zone_name(self, zone):
        return self.ZONES[zone][0] if zone in self.ZONES else "房间里"

    def region_name(self, region):
        return self.REGIONS[region][0] if region in self.REGIONS else "不知道哪里"

    def item_name(self, item):
        return self.ITEMS[item][0] if item in self.ITEMS else item

    def kind_of(self, item):
        return self.ITEMS[item][1] if item in self.ITEMS else "thing"


LEGACY = Layout(DEFAULT_SCENE_ID, MappingProxyType(dict(ZONES)), MappingProxyType(dict(REGIONS)),
                MappingProxyType(dict(ITEMS)), MappingProxyType(dict(KINDS)), "IR_Item_Espresso_Pink")


def scene_id(value=None) -> str:
    """Absence alone means the old room; invalid/unknown explicit IDs never fall back."""
    if value is None or value == "petal-oak":
        return DEFAULT_SCENE_ID
    if not isinstance(value, str) or value not in {DEFAULT_SCENE_ID, *SCENE_PROFILES}:
        raise ValueError("不认识的小屋场景")
    return value


def from_profile(profile: dict, expected: str) -> Layout:
    """Read the server's trusted profile sight schema, not client observation data."""
    if not isinstance(profile, dict) or profile.get("sceneId") != expected:
        raise ValueError("场景编号与受信配置不一致")
    sight = profile.get("sight")
    required = {"version", "zones", "walk", "also", "regions", "items", "names", "kinds", "kindNames", "favorite"}
    if not isinstance(sight, dict) or not required.issubset(sight) or type(sight["version"]) is not int or sight["version"] != 1:
        raise ValueError("新场景缺少有效 sight 配置")
    for key, cap in (("zones", 128), ("walk", 128), ("also", 128), ("regions", 512), ("names", 2048), ("kinds", 512), ("kindNames", 128)):
        value = sight[key]
        pattern = _NAME_ID if key == "names" else _ID
        if not isinstance(value, dict) or len(value) > cap or any(not isinstance(k, str) or not pattern.fullmatch(k) for k in value):
            raise ValueError(f"sight.{key} 编号或数量无效")
    if not sight["zones"] or not sight["regions"]:
        raise ValueError("场景没有可用的公开地图")

    def point(value, n):
        if not isinstance(value, list) or len(value) != n or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 100 for x in value):
            raise ValueError("场景几何坐标无效")
        return tuple(float(x) for x in value)

    def label(key, mapping):
        value = mapping.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 80 or any(c in value for c in "\r\n\x00"):
            raise ValueError("场景缺少有效的公开名称")
        return value

    names = sight["names"]
    zones = {key: (label(key, names), point(value, 2)) for key, value in sight["zones"].items()}
    for key, value in sight["walk"].items():
        if key not in zones:
            raise ValueError("walk 引用了未知区域")
        point(value, 2)
    for key, values in sight["also"].items():
        if key not in zones or not isinstance(values, list) or len(values) > 32:
            raise ValueError("also 引用了未知区域或过多站位")
        for value in values:
            point(value, 2)
    regions = {}
    for key, value in sight["regions"].items():
        if not isinstance(value, dict) or not isinstance(value.get("zone"), str) or value.get("zone") not in zones or type(value.get("r")) not in (int, float) or not math.isfinite(value["r"]) or not 0 < value["r"] <= 20:
            raise ValueError("场景台面定义无效")
        point(value.get("at"), 3)
        if "box" in value and type(value["box"]) is not bool:
            raise ValueError("场景容器标记无效")
        regions[key] = (label(key, names), value["zone"], value.get("box", False))
    ids = sight["items"]
    if not isinstance(ids, list) or not ids or len(ids) > 512 or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("场景物品词表无效")
    items, kinds = {}, {}
    for item in ids:
        kind = sight["kinds"].get(item)
        if not isinstance(kind, str) or not _ID.fullmatch(kind):
            raise ValueError("场景物品缺少种类")
        kinds[kind] = label(kind, sight["kindNames"])
        items[item] = (label(item, names), kind)
    if not isinstance(sight["favorite"], str) or sight["favorite"] not in items:
        raise ValueError("场景默认杯子不在词表")
    return Layout(expected, MappingProxyType(zones), MappingProxyType(regions), MappingProxyType(items),
                  MappingProxyType(kinds), sight["favorite"])


def load_scene(value=None) -> Layout:
    selected = scene_id(value)
    if selected == DEFAULT_SCENE_ID:
        return LEGACY
    path = SCENE_PROFILES[selected]
    if path.stat().st_size > 2_000_000:
        raise ValueError("场景配置过大")
    return from_profile(json.loads(path.read_text(encoding="utf-8")), selected)
