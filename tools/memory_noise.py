"""评测用的无关记忆 (干扰测试, 2026-11-01 记忆按脑区重设计阶段 0; 做法借 MemT 1.0: 先往记忆里塞无关的东西, 再看别的能力有没有变差).

每条都是用户身边的一件小事 (台灯坏了、想换个新键盘), 和各套评测问的事 (名字、住处、家人、宠物、城市、车、颜色、日期、喜好……)
都不沾边; 每条带一个只在它里面出现的关键词, 回复里提到这个词就算"噪声漏进了回答".
tests/test_memory_brain.py 查: 噪声里不含任何一套评测的期望词和禁止词, 也不含家人、宠物、城市、车、颜色这些图谱词.

  noise_facts(n, seed) -> [(正文, 关键词)]   同样的 n 和 seed 每次一样
  leaked(text, facts)  -> 提到了哪些噪声的关键词
"""
from __future__ import annotations

import random

OBJECTS = (
    "台灯 键盘 鼠标垫 显示器 耳机 充电线 插线板 雨伞 保温杯 帆布包 打印机 文件夹 订书机 便利贴 台历 闹钟 风扇 加湿器 "
    "空气净化器 扫地机器人 电饭煲 微波炉 烤箱 冰箱贴 拖把 晾衣架 鞋柜 窗帘 地毯 抱枕 床单 枕套 浴巾 牙刷 洗面奶 护手霜 "
    "指甲剪 剪刀 胶带 螺丝刀 卷尺 手电筒 遥控器 路由器 网线 机顶盒 投影仪 音箱 摄像头 手机壳 钢化膜 充电宝 头盔 "
    "跳绳 瑜伽垫 哑铃 羽毛球拍 乒乓球拍 棋盘 拼图 积木 魔方 明信片 邮票 相框 花瓶 仙人掌 门锁 钥匙扣 工牌 公交卡 收纳盒 "
    "垃圾桶 纸巾盒 衣帽架 熨斗 吹风机 梳子 镜子 眼镜盒 手表 计算器 尺子 橡皮 铅笔 墨水 毛笔 宣纸 画板 口罩 创可贴 体温计 "
    "坐垫 脚凳 挂钟 书立 笔筒 饭盒 水壶 保鲜盒 储物箱 鞋刷"
).split()
TEMPLATES = ("用户的{}前几天坏了", "用户想换个新的{}")  # 不写"一个": 第四套"兄弟姐妹"把"一个"这类说法当编出来的数


def all_facts() -> list[tuple[str, str]]:
    return [(t.format(o), o) for t in TEMPLATES for o in OBJECTS]


def noise_facts(n: int, seed: int = 0) -> list[tuple[str, str]]:
    """n 条无关记忆 (不超过总数), 按 seed 打乱后取前 n 条; 同一个关键词最多出现在一条里 (先取完一种说法再取另一种)."""
    if n <= 0:
        return []
    pool = all_facts()
    random.Random(seed).shuffle(pool)
    out, used = [], set()
    for fact, key in pool + pool:
        if len(out) >= min(n, len(pool)):
            break
        if key in used and len(used) < len(OBJECTS):
            continue
        if (fact, key) not in out:
            out.append((fact, key))
            used.add(key)
    return out


def leaked(text: str, facts: list[tuple[str, str]]) -> list[str]:
    """text 里提到了哪几条噪声的关键词."""
    return [k for _, k in facts if k in (text or "")]
