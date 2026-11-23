"""配置加载: config/default.json <- config/local.json <- --config 指定文件 <- --set 覆盖."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def apply_override(cfg: dict, assignment: str) -> None:
    """'llm.engine=llamacpp' 形式的覆盖, 值按 JSON 解析, 失败则当字符串."""
    if "=" not in assignment:
        raise ValueError(f"覆盖项缺少 '=': {assignment}")
    path, raw = assignment.split("=", 1)
    keys = path.strip().split(".")
    node = cfg
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise ValueError(f"覆盖路径不是对象: {path}")
    node[keys[-1]] = _parse_value(raw.strip())


def load_config(path: str | None = None, overrides: Iterable[str] = ()) -> dict:
    cfg = json.loads((ROOT / "config" / "default.json").read_text(encoding="utf-8"))
    local = ROOT / "config" / "local.json"
    if local.exists():
        cfg = deep_merge(cfg, json.loads(local.read_text(encoding="utf-8")))
    if path:
        cfg = deep_merge(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
    for item in overrides:
        apply_override(cfg, item)
    mem = cfg.get("memory")
    if isinstance(mem, dict) and isinstance(mem.get("vault"), str) and mem["vault"].startswith("~"):
        # 安装包给新用户的内置记忆库是 "~/.worldmodel/memory/vault" (2026-11-02 记忆阶段 3): 读的时候就换成绝对路径,
        # 渠道日志、工作台"不许碰记忆库"这些直接拿它当路径的地方才对得上. 空串照旧是不开长期记忆
        mem["vault"] = os.path.normpath(os.path.expanduser(mem["vault"]))
    return cfg


def resolve_path(value: str) -> Path:
    """配置里的相对路径一律相对仓库根目录."""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p
