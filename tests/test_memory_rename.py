"""角色改名 (memory.renamed_from, 2026-10-27 小流 -> 绫音): 启动时把旧名字的记忆文件夹搬过来, 旧日志里带旧名的行照样认."""
from pathlib import Path

from wm.memory import Memory


def _old_vault(tmp_path: Path) -> Path:
    old = tmp_path / "小流"
    (old / "记忆").mkdir(parents=True)
    (old / "日志").mkdir()
    (old / "README.md").write_text("# 小流的记忆\n\n这里是 WorldModel 角色「小流」的长期记忆, 都是普通 Markdown.\n\n我自己写的一行: 小流很可爱\n",
                                   encoding="utf-8")
    (old / "记忆" / "关于用户.md").write_text("# 关于用户\n\n- 用户喜欢吃草莓 #auto-memory\n", encoding="utf-8")
    (old / "日志" / "2026-10-26.md").write_text("- 14:00 用户: 你好 → 小流: 你好呀\n- 14:02 小屋: 用户摸了摸小流的头 ×3\n", encoding="utf-8")
    return old


def test_old_folder_moves_to_new_name(tmp_path):
    old = _old_vault(tmp_path)
    m = Memory({"vault": str(tmp_path), "character": "绫音", "renamed_from": ["小流"], "graph_notes": False})
    m.init()
    new = tmp_path / "绫音"
    assert not old.exists() and new.is_dir() and m.dir == new
    assert "用户喜欢吃草莓" in (new / "记忆" / "关于用户.md").read_text(encoding="utf-8")
    readme = (new / "README.md").read_text(encoding="utf-8")
    # 程序写的标题和"角色「…」"跟着改, 用户自己写的字不动
    assert readme.startswith("# 绫音的记忆") and "角色「绫音」" in readme and "小流很可爱" in readme
    # 日志正文不改 (那是当时说的话), 旧名的小屋行照样算进今天的事
    log = new / "日志" / "2026-10-26.md"
    assert "→ 小流:" in log.read_text(encoding="utf-8")
    assert m._room_events(log) == [("poke", "head", 3)]
    hits = m.recall("我喜欢吃什么")
    assert hits["hits"] and hits["hits"][0]["file"] == "绫音/记忆/关于用户.md"


def test_both_folders_are_not_merged(tmp_path):
    old = _old_vault(tmp_path)
    (tmp_path / "绫音").mkdir()
    m = Memory({"vault": str(tmp_path), "character": "绫音", "renamed_from": ["小流"], "graph_notes": False})
    m.init()
    assert old.is_dir() and m.dir == tmp_path / "绫音"
    assert not (tmp_path / "绫音" / "记忆" / "关于用户.md").read_text(encoding="utf-8").count("草莓")


def test_rename_blocked_keeps_old_folder(tmp_path, monkeypatch):
    old = _old_vault(tmp_path)

    def busy(self, target):
        raise PermissionError("文件被占着")

    monkeypatch.setattr(Path, "rename", busy)
    m = Memory({"vault": str(tmp_path), "character": "绫音", "renamed_from": ["小流"], "graph_notes": False})
    m.init()
    # 搬不动: 这次接着用旧文件夹, 不新建一个空的
    assert m.dir == old and not (tmp_path / "绫音").exists()
    assert "用户喜欢吃草莓" in (old / "记忆" / "关于用户.md").read_text(encoding="utf-8")


def test_no_old_name_configured_starts_fresh(tmp_path):
    _old_vault(tmp_path)
    m = Memory({"vault": str(tmp_path), "character": "绫音", "graph_notes": False})
    m.init()
    assert (tmp_path / "小流").is_dir() and (tmp_path / "绫音" / "记忆").is_dir()
