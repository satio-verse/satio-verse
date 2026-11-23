"""记忆图谱 (人、物、地点): 实体并组、按类列举、实体属性更正、问用户自己的某个人/某样东西."""
from datetime import datetime, timedelta

from wm import memory_graph as G
from wm.memory import Memory

DAY0 = datetime(2026, 6, 1, 20, 0)


def _mem(tmp_path, now):
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m


def _summary(tmp_path):
    return (tmp_path / "小流" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")


def _add(m, now, day, *facts):
    now["t"] = DAY0 + timedelta(days=day)
    m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": f, "times": ["20:00"]} for f in facts], 0)


def test_rules():
    assert G.links("用户的狗叫旺财") == [("狗", "旺财", False)]
    assert G.links("用户养了一只柯基") == [("柯基", "狗", False)]
    assert G.links("小美是用户的妹妹") == [("妹妹", "小美", False)]
    assert G.links("用户把狗的名字改成了雪碧") == [("狗", "雪碧", True)]  # 不会认出"了雪碧"
    assert G.mentions("用户的女朋友叫小雨") == {"女朋友"}  # 长词优先, 不再算"朋友"
    assert G.mentions("用户换了一辆蓝色的比亚迪") == {"车"} and G.mentions("用户去年去成都旅游了") == {"成都"}
    assert G.normalize("我爸以前是做什么的") == "我爸爸以前是做什么的" and G.normalize("我爸爸") == "我爸爸"
    assert G.attr_of("用户的猫改名叫豆包了") == ("猫", "名字", "豆包")
    assert G.attr_of("用户开的是一辆红色的宝马") == ("用户", "车", "一辆红色的宝马")
    assert G.supersedes("用户的妹妹搬到杭州了", "用户的妹妹住在南京")
    assert not G.supersedes("用户的同事叫小李", "用户的同事叫老王")  # 同事常有好几个
    assert not G.supersedes("用户的另一只猫叫豆包", "用户的猫叫团团")
    assert G.list_query("我家里都有谁") == "家人" and G.list_query("我养了哪些宠物") == "宠物"
    assert G.list_query("我都去过哪儿") == "地点" and G.list_query("我家在哪") is None
    assert G.asks_own("我爸以前是做什么的") and G.asks_own("我现在开什么车") and not G.asks_own("今天天气怎么样")


def test_graph_groups():
    texts = ["用户养了一只柯基", "用户的狗叫旺财", "旺财今天把拖鞋咬坏了", "用户的同事叫老王", "用户的同事叫小李",
             "老王养了一只布偶猫", "用户的女朋友叫小雨", "小雨的生日是3月5日"]
    g = G.Graph(texts)
    dog = g.root["旺财"]
    assert g.group[dog] == {"旺财", "狗", "柯基"} and g.members[dog] == [0, 1, 2]  # 名字认出来以后别处出现也算
    assert g.label(dog) == "旺财" and g.category(dog) == "宠物"
    assert g.root["老王"] != g.root["小李"] and g.category(g.root["老王"]) == "人物/朋友"  # 两个同事不并成一个人
    assert g.root["小雨"] == g.root["女朋友"] and g.category(g.root["小雨"]) == "人物/伴侣"
    assert [g.label(r) for r in g.roots_in("我家柯基最近闯什么祸了")] == ["旺财"]


def test_two_hops_and_alias_answers(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的女朋友叫小雨", "用户养了一只柯基", "用户的狗叫旺财")
    _add(m, now, 1, "小雨的生日是3月5日", "旺财今天把拖鞋咬坏了", "用户的女朋友喜欢吃火锅")
    now["t"] = DAY0 + timedelta(days=2)
    assert m.recall("我女朋友生日是哪天")["answer"] == "我记得你说过小雨的生日是3月5日。"  # 先说名字那条往后放
    assert "拖鞋" in m.recall("我家柯基最近闯什么祸了")["answer"]
    assert "火锅" in m.recall("小雨喜欢吃什么")["answer"]  # 问名字, 记忆里写的是"女朋友"
    assert m.recall("小雨喜欢你吗")["answer"] is None  # 问角色的不按记忆答
    assert m.recall("我该不该给女朋友买花")["answer"] is None  # 求建议交给模型
    assert "拖鞋" in m.recall("旺财还好吗")["line"]  # 联想按组走


def test_list_by_category(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的妈妈是老师", "用户有个哥哥在上海工作", "用户养了一只橘猫", "老王养了一只布偶猫")
    _add(m, now, 1, "用户还养了一只仓鼠", "用户去年去成都旅游了", "用户上个月去西安出差了")
    now["t"] = DAY0 + timedelta(days=2)
    r = m.recall("我家里都有谁")
    assert r["answer"] == "我记得你说过你的妈妈是老师，你有个哥哥在上海工作。"
    assert r["line"] == "[记忆] 用户的妈妈是老师; 用户有个哥哥在上海工作."
    assert m.recall("我养了哪些宠物")["answer"] == "我记得你说过你养了一只橘猫，你还养了一只仓鼠。"  # 老王的猫不算
    assert m.recall("我都去过哪些城市")["answer"] == "我记得你说过你去年去成都旅游了，你上个月去西安出差了。"  # 哥哥在上海工作不算去过


def test_entity_attribute_updates(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的猫叫团团", "用户的车是白色的特斯拉", "用户的妹妹住在南京")
    _add(m, now, 3, "用户的猫改名叫豆包了", "用户换了一辆蓝色的比亚迪", "用户的妹妹搬到杭州了")
    text = _summary(tmp_path)
    assert "- ~~用户的猫叫团团 (来源: [[2026-06-01]] 20:00)~~ 2026-06-04 更正为: 用户的猫改名叫豆包了 #auto-summary" in text
    assert "~~用户的车是白色的特斯拉" in text and "~~用户的妹妹住在南京" in text
    now["t"] = DAY0 + timedelta(days=4)
    assert m.recall("我的猫叫什么")["answer"] == "我记得你说过你的猫改名叫豆包了。"
    assert "比亚迪" in m.recall("我现在开什么车")["answer"]
    assert "杭州" in m.recall("我妹妹现在住哪")["answer"]
    assert "杭州" in m.recall("我妹现在住哪")["answer"]  # 口语简称


def test_confirmed_candidate_also_updates(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的狗叫可乐")
    now["t"] = DAY0 + timedelta(days=2)
    m.propose("2026-06-03", [{"text": "用户把狗的名字改成了雪碧", "times": ["09:00"]}], 1)
    assert "~~" not in _summary(tmp_path)  # 还是候选, 不动已有的
    m.review(m.candidates()[0]["id"], True)
    assert "~~用户的狗叫可乐" in _summary(tmp_path) and "雪碧" in m.recall("我的狗叫什么名字")["answer"]


def test_own_entity_question_beats_self_preference(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的男朋友叫阿杰", "阿杰每周都去打篮球")
    now["t"] = DAY0 + timedelta(days=1)
    assert "篮球" in m.recall("我男朋友平时喜欢干嘛")["answer"]  # 不答成"你还没告诉过我你喜欢什么"

def test_obsidian_notes(tmp_path):
    import time

    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    _add(m, now, 0, "用户的女朋友叫小雨", "用户的同事叫老王", "老王养了一只布偶猫", "用户养了一只柯基", "用户的狗叫旺财")
    _add(m, now, 1, "小雨的生日是3月5日")
    m.recall("你好")
    base = tmp_path / "小流" / "图谱"
    for _ in range(50):  # 笔记在后台线程里写
        if (base / "用户.md").exists():
            break
        time.sleep(0.05)
    xiaoyu = (base / "人物" / "小雨.md").read_text(encoding="utf-8")
    assert "生成: wm" in xiaoyu and "类别: 人物/伴侣" in xiaoyu and "别名: [女朋友, 小雨]" in xiaoyu
    assert "- 小雨的生日是3月5日 · [[2026-06-02]]" in xiaoyu and "相关: [[用户]]" in xiaoyu
    cat = (base / "宠物" / "猫.md").read_text(encoding="utf-8")
    assert "相关: [[老王]]" in cat  # 老王的猫只连到老王
    hub = (base / "用户.md").read_text(encoding="utf-8")
    assert "- [[旺财]]" in hub and "[[猫]]" not in hub and "- [[小雨]] (伴侣)" in hub
    (base / "我的批注.md").write_text("# 我自己写的\n", encoding="utf-8")  # 没有"生成: wm"的不删
    (base / "宠物" / "旧的.md").write_text("---\n生成: wm\n---\n", encoding="utf-8")
    m._write_notes(m._graph_notes())
    assert (base / "我的批注.md").exists() and not (base / "宠物" / "旧的.md").exists()
    assert m.recall("我自己写的")["line"] is None  # 图谱/ 不参与检索
