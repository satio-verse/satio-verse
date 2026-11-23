"""桌宠小屋页面的 desktop/web/room-sight.js 和服务端 wm/cognition/layout.py 是同一张表: 站位一样, 放东西的地方都在布局里、
在同一处, 东西的编号一样 (页面报上来的编号服务端都认)."""
import re
from pathlib import Path

from wm.cognition import layout as Y

SRC = (Path(__file__).resolve().parent.parent / "desktop/web/room-sight.js").read_text(encoding="utf-8")


def _block(name: str) -> str:
    return re.search(rf"export const {name} = \{{(.*?)\n\}};", SRC, re.S).group(1)


def test_zones_match_layout():
    zones = {k: (float(x), float(z)) for k, x, z in re.findall(r"(\w+): \[(-?[\d.]+), (-?[\d.]+)\]", _block("ZONES"))}
    assert zones == {k: v[1] for k, v in Y.ZONES.items()}


def test_regions_and_items_match_layout():
    regions = dict(re.findall(r"(\w+): \{ zone: '(\w+)'", _block("REGIONS")))
    assert regions and all(Y.zone_of(r) == z for r, z in regions.items())
    boxes = set(re.findall(r"(\w+): \{[^}]*box: true", _block("REGIONS")))
    assert boxes == {r for r in regions if Y.is_box(r)} == {"fridge"}  # 桌宠小屋还没有抽屉
    items = re.search(r"export const ITEMS = \[(.*?)\];", SRC).group(1)
    assert re.findall(r"'(\w+)'", items) == list(Y.ITEMS)
    names = dict(re.findall(r"(\w+): '([^']+)'", re.search(r"export const NAMES = \{(.*?)\};", SRC, re.S).group(1)))
    assert all(names[i] == Y.item_name(i) for i in Y.ITEMS) and all(names[r] == Y.region_name(r) for r in regions)
