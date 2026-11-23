"""提取覆盖的尺子 (2026-11-18 专项): 一句自然话, 能不能变成一条记忆.

为什么单独量这个:
  714 里"忘了" 136 条、S2 新留出组 6/18、S3 日程答不对 —— 三个地方查下去都指到同一个洞:
  规则提取器只认"住在/喜欢/讨厌/养了"这几种句式, 量出来 33 句只中 8 句 (24%).
  检索层和取代层做得再好, 地基上没这条事实, 都是白搭.

两个数一起看 (缺一不可):
  覆盖  = 该记下的记下了吗        (低 = 她记性差)
  误抽  = 不该记的却记下了吗      (高 = 她会记错你说过的话, 比漏记更糟)

用法:
    python tools/bench_extract.py            # 跑一遍, 打印覆盖率/误抽率与逐条明细
    python tools/bench_extract.py --quiet    # 只打印两个数
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm.memory import extract_facts  # noqa: E402

# 33 句该记下的: 住址/偏好/宠物/日程/职业/关系/身体/习惯/口味, 每类都要有几条.
SHOULD = [
    "我是做前端的", "我转岗做后端了", "我在上海一家游戏公司上班", "我刚换了工作",
    "我住在苏州工业园区", "我上个月搬到杭州了", "我住在城南那边",
    "我每周三晚上去上钢琴课", "我12月1日要去体检", "我明天要去看牙医",
    "我今天去看了牙医", "我下周三要交房租", "我下个月要去成都出差",
    "我喜欢喝美式咖啡", "我现在爱喝手冲了", "我不吃辣", "我特别喜欢周杰伦",
    "我养了一只橘猫叫豆包", "我家狗上个月生了一场病", "我有个妹妹在读研",
    "我妈身体不太好", "我女朋友是做设计的", "我跟我爸关系一般",
    "我最近在学吉他", "我每天跑步五公里", "我睡眠一直不太好",
    "我考研考的是计算机", "我今年26岁", "我身高178",
    "我讨厌下雨天", "我怕打雷", "我对花生过敏", "我用的是火狐浏览器",
]

# 12 句不该记的: 闲聊、情绪、疑问、转述别人的话. 抽出来了就是记错.
SHOULD_NOT = [
    "今天好累啊", "外面下雨了", "你在干嘛呀", "我刚吃完饭",
    "我今天不想动", "我好困", "你说的那个我不太懂", "我朋友说他每天加班到很晚",
    "我妹妹说她不喜欢吃辣", "我该怎么办啊", "我是不是该早点睡", "我刚刚差点睡着",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    missed, noise = [], []
    for t in SHOULD:
        if not extract_facts(t):
            missed.append(t)
    for t in SHOULD_NOT:
        got = extract_facts(t)
        if got:
            noise.append((t, got))

    recall = (len(SHOULD) - len(missed)) / len(SHOULD)
    false_rate = len(noise) / len(SHOULD_NOT)

    print(f"覆盖   {len(SHOULD) - len(missed)}/{len(SHOULD)}  ({recall:.0%})")
    print(f"误抽   {len(noise)}/{len(SHOULD_NOT)}  ({false_rate:.0%})")
    if not a.quiet:
        for t in missed:
            print(f"  漏  {t}")
        for t, got in noise:
            print(f"  错  {t}  ->  {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
