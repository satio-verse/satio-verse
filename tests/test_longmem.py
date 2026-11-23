"""长记忆: 知识更新 (划掉旧的)、用户要求遗忘、时间、联想、问自己的事由服务端回答."""
from datetime import date, datetime

from wm import memory_ops as O
from wm.memory import Memory, carry_subject, extract_facts


def _mem(tmp_path, now=datetime(2026, 10, 27, 9, 0)):
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.clock = lambda: now
    m.init()
    m.start_session()
    return m


def _facts_file(tmp_path):
    return (tmp_path / "小流" / "记忆" / "关于用户.md").read_text(encoding="utf-8")


def test_extract_subject_carry_and_home():
    assert carry_subject("我最近在减肥，不喜欢吃甜食了") == "我最近在减肥，我不喜欢吃甜食了"
    assert extract_facts("我最近在减肥，所以不喜欢吃甜食了") == [("偏好", "用户不喜欢吃甜食了")]
    assert extract_facts("我妹妹在读大三，不喜欢早起") == []  # 说的是妹妹
    assert extract_facts("我的猫很胖，喜欢睡觉") == []
    assert extract_facts("我们全家搬到苏州了") == [("事实", "用户住在苏州")]
    assert extract_facts("我上个月就搬去武汉了") == [("事实", "用户住在武汉")]
    assert extract_facts("我们下个月要搬到深圳") == []  # 计划不是住处


def test_supersede_rules():
    assert O.supersedes("用户住在苏州", "用户住在南京")
    assert O.supersedes("用户不喜欢吃甜食了", "用户喜欢吃甜食")
    assert not O.supersedes("用户喜欢吃草莓", "用户喜欢吃甜食")
    line = "- 用户住在南京 (2026-10-20) #事实 #auto-memory"
    assert O.strike(line, "2026-10-27 更正为: 用户住在苏州") == \
        "- ~~用户住在南京 (2026-10-20)~~ 2026-10-27 更正为: 用户住在苏州 #事实 #auto-memory"
    assert O.fact_of_line(O.strike(line, "x")) is None  # 划掉的不再算事实


def test_knowledge_update_strikes_old_line(tmp_path):
    m = _mem(tmp_path)
    m.observe("我家在南京", "嗯", [], None)
    m.observe("我很喜欢吃甜食", "嗯", [], None)
    path = tmp_path / "小流" / "记忆" / "关于用户.md"
    path.write_text(path.read_text(encoding="utf-8") + "- 用户住在南京的老房子还没卖 (我自己写的)\n", encoding="utf-8")
    assert m.observe("我们全家搬到苏州了", "嗯", [], None) == ["用户住在苏州"]
    assert m.observe("我最近在减肥，不喜欢吃甜食了", "嗯", [], None) == ["用户不喜欢吃甜食了"]
    text = _facts_file(tmp_path)
    assert "~~用户住在南京 (2026-10-27)~~ 2026-10-27 更正为: 用户住在苏州" in text
    assert "~~用户喜欢吃甜食" in text and "- 用户不喜欢吃甜食了" in text
    assert "- 用户住在南京的老房子还没卖 (我自己写的)" in text  # 用户手写的行不动
    assert m.recall("我家现在在哪")["answer"] == "你住在苏州呀。"
    assert m.recall("我喜欢吃甜食吗")["answer"] == "你说过不喜欢吃甜食了呀。"
    assert "南京" not in (m.recall("我住哪个城市")["line"] or "")


def test_forget_on_request(tmp_path):
    m = _mem(tmp_path)
    m.observe("我住在杭州", "嗯", [], None)
    m.observe("我喜欢喝咖啡", "嗯", [], None)
    assert O.forget_target("我忘了带钥匙") is None  # 用户自己忘了, 不是让角色忘
    m.observe("帮我把我住在哪忘掉吧", "好", [], None)
    m.observe("别记着我喜欢喝咖啡了", "好", [], None)
    text = _facts_file(tmp_path)
    assert "~~用户住在杭州 (2026-10-27)~~ 2026-10-27 用户要求忘掉" in text and "~~用户喜欢喝咖啡" in text
    assert m.recall("我住在哪")["answer"] == "你让我把这个忘掉了，我就不记着啦。"
    assert m.recall("我喜欢喝什么")["answer"] == "你让我把这个忘掉了，我就不记着啦。"
    assert "杭州" not in (m.recall("我在哪个城市住")["line"] or "")


def test_time_relative_and_upcoming_first(tmp_path):
    assert O.with_time("用户10月28日要去医院复查", date(2026, 10, 20), date(2026, 10, 27)) == "用户明天要去医院复查"
    assert O.with_time("用户11月3日要考驾照", date(2026, 10, 20), date(2026, 10, 27)) == "用户11月3日要考驾照 (7天后)"
    assert O.event_date("用户1月5日回老家", date(2026, 12, 20)) == date(2027, 1, 5)  # 跨年往后算
    assert O.absolute_dates("周五用户要考试", date(2026, 10, 24)) == "10月30日用户要考试"  # 周六说的: 下一个周五
    assert O.absolute_dates("用户每周五去游泳", date(2026, 10, 24)) == "用户每周五去游泳"
    m = _mem(tmp_path, now=datetime(2026, 10, 26, 20, 0))
    m.observe("你要记住，后天我要去医院复查", "好", [], None)
    assert "- 10月28日用户要去医院复查 (2026-10-26)" in _facts_file(tmp_path)  # 写入时换成具体日期, "我"换成"用户"
    m.clock = lambda: datetime(2026, 10, 27, 9, 0)
    m.start_session()
    first = m.recall("嗨", first=True)["line"]
    assert first.startswith("[记忆] 明天用户要去医院复查")
    assert "第 " not in first  # 有近几天的安排时只带安排, 不带见面次数这些概况
    assert m.recall("我哪天要去医院复查")["answer"] == "你明天（10月28日）要去医院复查呀。"


def test_association_and_entity_profile(tmp_path):
    m = _mem(tmp_path)
    m.add_summary("2026-10-26", [{"text": "用户养了一只柯基", "times": ["20:01"]},
                                 {"text": "用户的狗叫旺财", "times": ["20:02"]}], 0)
    assert O.entities("用户养了一只柯基") == {"狗"} and O.entities("用户的狗叫旺财") == {"狗", "旺财"}
    r = m.recall("旺财是什么品种")
    assert "柯基" in r["line"]  # 经"狗"联想到另一条
    assert r["answer"].startswith("我记得你说过") and "柯基" in r["answer"]
    assert m.recall("我该不该再养一只狗")["answer"] is None  # 求建议交给模型


def test_abstain_and_role_guard(tmp_path):
    m = _mem(tmp_path)
    m.observe("我叫小明", "嗯", [], None)
    assert m.recall("我叫什么")["answer"] == "你叫小明呀。"
    assert m.recall("我的车是什么颜色")["answer"] == "这个你还没跟我说过呢。"
    assert m.recall("我生日是哪天")["answer"] == "你还没告诉过我你的生日呢。"
    r = m.recall("你叫什么名字")  # 问角色自己: 不用用户的记忆回答
    assert r["answer"] is None and r["line"] is None


def test_association_from_name_without_hits(tmp_path):
    # 向量一条都没命中时 ("团子是什么颜色"): 从问句里的名字走到它所在的事实, 再经"仓鼠"联想到另一条
    m = _mem(tmp_path)
    m.add_summary("2026-10-21", [{"text": "用户的仓鼠叫团子", "times": ["20:00"]}], 0)
    m.add_summary("2026-10-22", [{"text": "用户的仓鼠是白色的", "times": ["20:00"]}], 0)
    m._refresh()
    assert {h["text"] for h in m._associate("团子是什么颜色", [])} == {"用户的仓鼠叫团子", "用户的仓鼠是白色的"}


def test_birth_year_zodiac_and_inference(tmp_path):
    # 用户材料里的例子: 解析出出生年、生肖、农历生日; 问的时候按记下的回答, 没说过的生肖按年份推 (说明是推的)
    assert extract_facts("我是90年生人，农历5月初九，属马") == [
        ("事实", "用户是90年出生的"), ("事实", "用户属马"), ("事实", "用户生日是农历5月初九")]
    assert extract_facts("我是属马的") == [("事实", "用户属马")] and extract_facts("我是05年的学生") == []
    m = _mem(tmp_path, now=datetime(2026, 10, 24, 9, 0))
    assert m.observe("我是90年生人，农历5月初九，属马", "嗯", [], None) == ["用户是1990年出生的", "用户属马", "用户生日是农历5月初九"]
    assert m.recall("我属什么")["answer"] == "你说过你属马呀。"
    assert m.recall("我今年多大了")["answer"] == "你说过是1990年出生的，按年份算今年36岁。"
    assert m.recall("我哪年出生的")["answer"] == "你是1990年出生的呀。"
    assert O.answer_birth("我属什么", ["用户是1990年出生的"], date(2026, 10, 24)) == "你是1990年出生的，按年份算属马（春节前出生的话属蛇）。"
    assert "春节前" in O.answer_birth("我属什么", ["用户是1990年出生的", "用户属蛇"], date(2026, 10, 24))  # 对不上就照实说
    m.observe("我记错了，我是91年的", "嗯", [], None)  # 出生年是一个槽: 更正时旧的划掉
    assert "~~用户是1990年出生的" in _facts_file(tmp_path)


def test_past_events_fade_behind_current(tmp_path):
    # 过去的安排排到眼前的事后面, 有眼前的事时最多留一条过去的; 只问到过去的事时照常带上
    m = _mem(tmp_path, now=datetime(2026, 10, 27, 9, 0))
    m.add_summary("2026-10-18", [{"text": "用户10月20日要去面试", "times": ["20:00"]},
                                 {"text": "用户10月21日要去体检", "times": ["20:01"]}], 0)
    m.add_summary("2026-10-26", [{"text": "用户10月28日要去考驾照", "times": ["20:00"]}], 0)
    line = m.recall("面试、体检、考驾照这几件事")["line"]  # 三条都取得到: 有眼前的事时过去的只留一条
    assert "明天" in line and line.count("天前") == 1
    assert "面试" in m.recall("我那次面试是哪天")["line"]
