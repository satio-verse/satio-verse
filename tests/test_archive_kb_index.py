"""实验数据的自动索引 (tools/archive_kb.py write_indexes): .md 写成链接, 另列正文里提到这些数据的笔记. 用临时知识库, 不碰真的."""
import importlib.util

from wm.config import ROOT

_spec = importlib.util.spec_from_file_location("archive_kb", ROOT / "tools" / "archive_kb.py")
ak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ak)


def put(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_kb(tmp_path):
    kb = tmp_path / "WorldModel知识库"
    base = kb / ak.SUB
    diag = base / "会话诊断"
    put(diag / "记忆页-20261101" / "说明.md", "# 记忆页")
    put(diag / "记忆页-20261101" / "结果" / "shot.png")
    put(diag / "r1a-定位轮" / "summary.md")
    put(diag / "r1a-定位轮复核" / "README.md")
    put(diag / "r1a-定位轮复核" / "notes.md")
    put(diag / "只有数据" / "a.json", "{}")
    put(base / "记忆与对话评测" / "longmem-bench-1.md")
    put(base / "记忆与对话评测" / "digest-1.jsonl", "{}")
    put(base / "归档规则.md", "`11 实验数据/会话诊断/只有数据/` 这篇在实验数据里面, 不算正文")
    put(kb / "03 记忆与关系" / "记忆页.md", "截图在 `11 实验数据/会话诊断/记忆页-20261101/`。")
    put(kb / "07 实验与评测" / "定位.md", "见 11 实验数据/会话诊断/r1a-定位轮复核/README.md")
    put(kb / "10 开发日志" / "开发日志.md",
        "数据：`11 实验数据/记忆与对话评测/longmem-bench-*`；另见 `11 实验数据/会话诊断/记忆页-20261101/`")
    put(kb / "00 首页.md", "[[11 实验数据/索引]]")
    return kb, base


def test_md_rows_become_links_and_others_stay_code(tmp_path):
    kb, base = make_kb(tmp_path)
    ak.commit(base, {})
    idx = (base / "会话诊断" / "索引.md").read_text(encoding="utf-8")
    assert "| [[11 实验数据/会话诊断/记忆页-20261101/说明\\|记忆页-20261101/说明.md]] |" in idx
    assert "| `记忆页-20261101/结果/shot.png` |" in idx  # 图片照旧
    assert "| `只有数据/a.json` |" in idx
    ev = (base / "记忆与对话评测" / "索引.md").read_text(encoding="utf-8")
    assert "| [[11 实验数据/记忆与对话评测/longmem-bench-1\\|longmem-bench-1.md]] |" in ev
    assert "| `digest-1.jsonl` |" in ev


def test_referrers_by_folder_and_category(tmp_path):
    kb, base = make_kb(tmp_path)
    ak.commit(base, {})
    idx = (base / "会话诊断" / "索引.md").read_text(encoding="utf-8")
    assert "## 正文里提到的笔记" in idx
    # 文件夹链到它的说明, 后面跟提到它的笔记 (相对知识库的路径, 显示文件名)
    assert ("- [[11 实验数据/会话诊断/记忆页-20261101/说明|记忆页-20261101]]："
            "[[03 记忆与关系/记忆页|记忆页]] · [[10 开发日志/开发日志|开发日志]]") in idx
    assert "- [[11 实验数据/会话诊断/r1a-定位轮复核/README|r1a-定位轮复核]]：[[07 实验与评测/定位|定位]]" in idx
    # "r1a-定位轮复核" 不算提到 "r1a-定位轮"; 实验数据里面的笔记 (归档规则) 不算正文
    assert "`r1a-定位轮/`" in idx.split("没被正文提到的文件夹：")[1]
    assert "`只有数据/`" in idx.split("没被正文提到的文件夹：")[1]
    assert "归档规则" not in idx
    # 只写到这一类 (带 *) 的另起一行
    ev = (base / "记忆与对话评测" / "索引.md").read_text(encoding="utf-8")
    assert "- 这一类（没指到具体文件夹的）：[[10 开发日志/开发日志|开发日志]]" in ev


def test_nothing_mentioned_no_section(tmp_path):
    base = tmp_path / "kb" / ak.SUB
    put(base / "截图" / "a.png")
    ak.commit(base, {})
    assert "正文里提到的笔记" not in (base / "截图" / "索引.md").read_text(encoding="utf-8")


def test_mentions_stops_at_longer_names():
    assert ak.mentions("见 会话诊断/r1a-定位轮/", "会话诊断/r1a-定位轮")
    assert ak.mentions("见 会话诊断/r1a-定位轮", "会话诊断/r1a-定位轮")
    assert not ak.mentions("见 会话诊断/r1a-定位轮复核/", "会话诊断/r1a-定位轮")
    assert not ak.mentions("会话诊断/动作成长-E15/", "会话诊断/动作成长")
    assert ak.mentions("会话诊断/动作成长-E15/ 和 会话诊断/动作成长/", "会话诊断/动作成长")
