"""QQ / 微信会话的梗预查 (wm/channels/memes.py): 什么时候查、塞什么、不塞什么. 用临时小库, 不碰她工作台里的那份."""
import json
import sqlite3

import pytest

from wm.channels import memes as M

ROWS = [
    # title, slug, tags, usage, summary
    ("久留美的小曲", "久留美的小曲", "音乐梗、整活、股市梗", "调侃炒股赌博时的盈亏起伏、反复操作。", "来源 一段整活视频"),
    ("尼罗河二圣", "尼罗河二圣", "动物梗、表情包", "调侃河马和尼罗鳄两种凶猛动物。", "来源 字幕误作河恶鱼"),
    ("破防", "破防", "情绪、网络用语", "心理防线被击破, 情绪崩了。", "来源 游戏术语"),
    ("摆烂", "摆烂", "态度、网络用语", "事情没法变好了就干脆放任。", "来源 NBA 摆烂"),
    ("芭比Q", "babyq", "谐音梗", "完蛋了的意思 (烧烤 BBQ 谐音)。", "来源 直播"),
    ("一键三连", "一键三连", "B站、弹幕文化", "点赞投币收藏一起来。", "来源 B站"),
    ("电子榨菜", "电子榨菜", "生活、下饭", "吃饭时看的下饭视频。", "来源 网络"),
    ("某地人都这样", "某地人都这样", "地域黑、攻击", "地域攻击。", "来源 不该说"),
    ("擦边小剧场", "擦边小剧场", "擦边、低俗", "擦边内容。", "来源 不该说"),
    ("尊嘟假嘟", "zundujiadu", "谐音梗、可爱", "真的假的 (卖萌说法)。", "来源 网络"),
]


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "memes.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memes (title TEXT PRIMARY KEY, slug TEXT, tags TEXT, usage TEXT, origin TEXT, other TEXT, summary TEXT)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("insert into memes values (?, ?, ?, ?, '', '', ?)", ROWS)
    con.commit()
    con.close()
    return p


def line(text, who="小明(10001)", n=1):
    return f"[20:00 #{n} {who}] {text}"


class _Echo:
    """假模型: 记下每次调用的消息, 回一句固定格式的话 (推状态那一步解析不出分数, 全按 0.5 算)."""

    def __init__(self):
        self.seen = []

    async def stream(self, messages, schema=None, max_tokens=160, id_slot=None, meta=None, **kw):
        self.seen.append(messages)
        meta["usage"] = {"prompt_tokens": 10, "completion_tokens": 3}
        yield json.dumps({"emo": "happy", "reply": f"接一句{len(self.seen)}"}, ensure_ascii=False)


SHOULD = [  # 该塞的 (问梗、裸词、长词)
    "久留美的小曲是什么梗", "尼罗河二圣是啥梗啊", "破防是什么意思", "摆烂到底是什么意思？", "芭比Q 是啥梗",
    "一键三连出自哪", "电子榨菜哪来的", "尊嘟假嘟是什么梗", "你们说的久留美的小曲是什么梗", "群里刷的尼罗河二圣啥意思",
    "久留美的小曲", "尼罗河二圣！", "尊嘟假嘟", "电子榨菜", "一键三连",
    "我今天又在听久留美的小曲了", "这俩简直是尼罗河二圣吧", "这俩简直尼罗河二圣", "股票一跌就想起久留美的小曲", "破防了，这是什么梗",
]
SHOULD_NOT = [  # 不该塞的 (闲聊、库里没有、短词不在问句里、带风险标签)
    "今天天气不错", "晚上吃什么", "你这话是什么意思", "我破防了", "摆烂一天", "哈哈哈哈", "在吗", "明天几点开会",
    "这个视频挺好看", "有人一起打游戏吗", "这是什么", "什么意思啊", "某地人都这样是什么梗", "擦边小剧场是啥梗",
    "某地人都这样", "我觉得还行吧", "那个梗我知道", "收到", "周末去爬山", "芭比Q了",
]


def test_should_and_should_not(db):
    ref = M.MemeRef(db)
    hits = [s for s in SHOULD if ref.lookup(line(s))]
    misses = [s for s in SHOULD if not ref.lookup(line(s))]
    false = [s for s in SHOULD_NOT if ref.lookup(line(s))]
    assert not misses, misses
    assert not false, false
    assert len(hits) == 20


def test_ref_text_rules(db):
    out = M.MemeRef(db).lookup(line("久留美的小曲是什么梗"))
    assert out.startswith("[梗百科]") and "外部资料" in out
    assert "「久留美的小曲」" in out and "调侃炒股" in out
    assert "别科普" in out and "拿不准" in out
    assert "来源" not in out  # 不念出处


def test_risky_entries_never_injected(db):
    ref = M.MemeRef(db)
    for s in ("某地人都这样是什么梗", "某地人都这样", "擦边小剧场", "大家都说擦边小剧场好看"):
        assert ref.lookup(line(s)) is None


def test_one_item_latest_line_first(db):
    block = "\n".join([line("尼罗河二圣是什么梗", n=1), line("久留美的小曲是什么梗", n=2)])
    out = M.MemeRef(db).lookup(block)
    assert "久留美的小曲" in out and "尼罗河二圣" not in out
    out2 = M.MemeRef(db, max_items=2).lookup(block)
    assert "久留美的小曲" in out2 and "尼罗河二圣" in out2


def test_headers_quotes_and_tags_ignored(db):
    ref = M.MemeRef(db)
    # 行头里的昵称、［图片］标记、引用的旧消息都不算
    assert ref.lookup("[20:00 #1 久留美的小曲(10001)] 在吗") is None
    assert ref.lookup(line("［图片#1］")) is None
    assert ref.lookup(line("好的") + "\n［引用的消息 #5 小红：久留美的小曲是什么梗］") is None
    assert ref.lookup(line("(回复 #3) 尼罗河二圣是什么梗")) is not None


def test_long_usage_truncated(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("update memes set usage = ? where title = '电子榨菜'", ("很长的用法说明" * 60,))
    con.commit()
    con.close()
    out = M.MemeRef(db, usage_chars=50).lookup(line("电子榨菜是什么梗"))
    assert "……" in out and len(out) < 260


def test_scan_min_controls_passive_hits(db):
    # 默认 5 字: 句子里顺口带出的四字词不查 (常和普通说法撞), 调到 4 才接
    assert M.MemeRef(db).lookup(line("看完这期记得一键三连")) is None
    assert M.MemeRef(db, scan_min=4).lookup(line("看完这期记得一键三连")) is not None
    assert M.MemeRef(db, scan_min=4).lookup(line("吃饭不看点电子榨菜不行")) is not None
    assert M.MemeRef(db, scan_min=5).lookup(line("这俩简直尼罗河二圣")) is not None
    assert M.MemeRef(db, scan_min=6).lookup(line("这俩简直尼罗河二圣")) is None
    assert M.MemeRef(db, scan_min=6).lookup(line("尼罗河二圣是什么梗")) is not None  # 问句照样查


def test_bad_db_returns_none(tmp_path):
    p = tmp_path / "broken.db"
    p.write_bytes(b"not a database")
    assert M.MemeRef(p).lookup(line("久留美的小曲是什么梗")) is None


def _ext(tmp_path, enabled=True, server_text=None, db=None):
    root = tmp_path / "extensions"
    rev = "abcdef0123456789abcdef01"
    folder = root / "meme-wiki" / "revisions" / rev
    folder.mkdir(parents=True)
    core = tmp_path / "tools" / "meme_wiki" / "meme_wiki.py"
    core.parent.mkdir(parents=True)
    core.write_text("# core", encoding="utf-8")
    if db is not None:
        (core.parent / "memes.db").write_bytes(db.read_bytes())
    text = server_text if server_text is not None else (
        'CORE = Path(os.environ.get("MEME_WIKI_CORE") or r"%s")\n' % str(core))
    (folder / "server.py").write_text(text, encoding="utf-8")
    (root / "registry.json").write_text(json.dumps({"version": 1, "packages": {
        "meme-wiki": {"id": "meme-wiki", "enabled": enabled, "active_revision": rev}}}), encoding="utf-8")
    return root, core.parent / "memes.db"


def test_discover_db_from_her_extension(tmp_path, db):
    root, expect = _ext(tmp_path, db=db)
    assert M.discover_db(root) == expect
    ref = M.from_conf({}, root)
    assert ref is not None and ref.lookup(line("尼罗河二圣是什么梗"))


def test_disabled_extension_or_conf_turns_it_off(tmp_path, db):
    root, _ = _ext(tmp_path, enabled=False, db=db)
    assert M.discover_db(root) is None and M.from_conf({}, root) is None
    root2, _ = _ext(tmp_path / "b", enabled=True, db=db)
    assert M.from_conf({"enabled": False}, root2) is None
    ref = M.from_conf({}, root2)
    reg = root2 / "registry.json"
    data = json.loads(reg.read_text(encoding="utf-8"))
    data["packages"]["meme-wiki"]["enabled"] = False  # 她后来把扩展停了: 下一轮就不查
    reg.write_text(json.dumps(data), encoding="utf-8")
    assert ref.lookup(line("尼罗河二圣是什么梗")) is None


def test_explicit_db_conf(tmp_path, db):
    ref = M.from_conf({"db": str(db), "scan_min": 6, "max_items": 2}, tmp_path / "nowhere")
    assert ref is not None and ref.scan_min == 6 and ref.max_items == 2
    assert M.from_conf({"db": str(tmp_path / "missing.db")}, tmp_path) is None


def test_read_only(db):
    ref = M.MemeRef(db)
    with ref._connect() as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("delete from memes")


# -- 接进 QQ / 微信会话 (guest.py 的 ref、manager.py 的 _memes / parse_conf) ------------------------------------------

def test_guest_ref_this_turn_only_any_tier(db):
    import asyncio
    from wm.channels import Trust
    from wm.channels import guest as G

    llm = _Echo()
    s = G.GuestSession(Trust.ACQUAINTANCE, "group", "测试群", "绫音", llm)  # 熟人档: 不能带主人的记忆, 但能带参考
    block = line("久留美的小曲是什么梗")
    ref = M.MemeRef(db).lookup(block)
    asyncio.run(s.reply(block, ref=ref))
    asyncio.run(s.reply(line("哈哈", n=2)))
    first, second = llm.seen
    assert "[梗百科]" in first[-1]["content"] and first[-1]["content"].index("[梗百科]") > first[-1]["content"].index("<</外部消息")
    assert all("[梗百科]" not in u for u, _ in s.history)  # 不进历史
    assert all("[梗百科]" not in str(m["content"]) for m in second)  # 下一轮看不到
    assert first[0] == second[0]  # 系统提示没变 (前缀缓存)
    with pytest.raises(ValueError):
        asyncio.run(s.reply(block, memory="[记忆] 用户叫阿青", ref=ref))  # 记忆照旧只给主人那一档


def test_spin_session_takes_ref_too(db):
    """manager 对每种会话都按位置多传一个 ref: 只给 A/B 用的 SpinSession (wm/persona_state.py) 也得收, 不然一接上就报错;
    一样只给回话那一轮看、不进历史."""
    import asyncio
    from wm import persona_state as PS
    from wm.channels import Trust

    llm = _Echo()
    s = PS.SpinSession(Trust.ACQUAINTANCE, "group", "测试群", "绫音", llm, core=PS.clean_core({"voice": ["简短"]}), who="群友")
    block = line("久留美的小曲是什么梗")
    ref = M.MemeRef(db).lookup(block)
    r = asyncio.run(s.reply(block, None, None, ref))  # 和 manager._model 一样按位置传
    asyncio.run(s.reply(line("哈哈", n=2)))
    _, reply1, _, reply2 = llm.seen  # 每轮两次: 先推状态, 再回话
    assert "[梗百科]" in reply1[-1]["content"] and "[你此刻的状态]" in reply1[-1]["content"]
    assert r["reply"] == "接一句2" and r["state"]["closeness"] == 0.5
    assert all("[梗百科]" not in u for u, _ in s.history)  # 不进历史
    assert all("[梗百科]" not in str(m["content"]) for m in reply2)  # 下一轮看不到


def test_manager_memes_conf_and_lazy_ref(tmp_path, db):
    from types import SimpleNamespace
    from wm.channels.manager import ChannelManager, parse_conf

    conf, _ = parse_conf({})
    assert conf["memes"] == {"enabled": True, "db": "", "scan_min": 5, "max_items": 1}
    conf, _ = parse_conf({"memes": {"enabled": False, "scan_min": 4, "max_items": 9}})
    assert conf["memes"] == {"enabled": False, "db": "", "scan_min": 4, "max_items": 3}

    root, _ = _ext(tmp_path, db=db)
    reg = SimpleNamespace(root=root)
    fake = SimpleNamespace(conf={"memes": {"enabled": True, "db": "", "scan_min": 5, "max_items": 1}},
                           agent_hub=SimpleNamespace(extensions=reg))
    ref = ChannelManager._memes(fake)
    assert ref is not None and ChannelManager._memes(fake) is ref  # 没变就不重建
    assert ref.lookup(line("尼罗河二圣是什么梗"))
    fake.conf = {"memes": {"enabled": False}}
    assert ChannelManager._memes(fake) is None  # 关了
    fake.conf, fake.agent_hub = {"memes": {"enabled": True, "db": str(db)}}, None
    assert ChannelManager._memes(fake) is not None  # 指定了库就用它
