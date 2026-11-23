"""把「世界接口契约」接进装扩展的流程。

契约全文在知识库 `09 问题与待办/世界接口契约（2026-11-15）.md`；可执行版本就是
`wm/agent/plugin_manifest.py`（纯函数，不 import、不执行插件代码）。

这一层不新增、也不放宽任何校验规则，只做三件装的时候缺的事：

1. `inspect(spec)`：装**之前**把「这个包装进去会怎样」摊开——合约不合约、
   要哪些权限、会往世界里加什么动作/事件、属于哪种世界（chat/space/game）。
   市场和插件页拿它做预览，用户看得懂再点装。
2. `gate(spec)`：装的时候卡一道，不合约直接抛 `ContractError`（消息是给人看的）。
3. `install_plugin_package(spec)`：带 `plugin` 块的世界包，真装进插件宿主
   （`~/.worldmodel/plugins/<id>/`），不是只当草稿躺在扩展目录里。

两条来路共用这一套：老扩展（没有 `plugin` 块）照旧，只是多一份报告；
世界插件（带 `plugin` 块）走契约校验 + 宿主安装。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import plugin_manifest as pm

CONTRACT_NAME = "世界接口契约（2026-11-15）"
CONTRACT_SCHEMA = pm.SCHEMA
KINDS = ("plugin", "extension")
MANIFEST_NAME = "wmworld.json"
MAX_REPORT_ITEMS = 40


class ContractError(ValueError):
    """不合约。消息直接给人看，不要在里面塞英文栈。"""


def classify(spec) -> str:
    """看这份包带不带 `plugin` 块：带 = 世界插件，不带 = 老扩展。"""
    if isinstance(spec, dict) and isinstance(spec.get("plugin"), dict):
        return "plugin"
    return "extension"


def _issue_row(issue) -> dict:
    return {"path": issue.path, "code": issue.code, "level": issue.level, "message": issue.message}


def _declared_files(manifest) -> list:
    """清单里点名要随包带的文件（面板、环境提示、双生夹具）。"""
    plugin = manifest.get("plugin") if isinstance(manifest, dict) else None
    if not isinstance(plugin, dict):
        return []
    out = []

    def add(value):
        if isinstance(value, str) and value and value not in out:
            out.append(value)

    panel = plugin.get("panel")
    if isinstance(panel, dict):
        add(panel.get("entry"))
    world = plugin.get("world")
    if isinstance(world, dict):
        add(world.get("env_prompt"))
        spectate = world.get("spectate")
        if isinstance(spectate, dict):
            add(spectate.get("panel"))
        perception = world.get("perception")
        if isinstance(perception, dict):
            for name in perception.get("twin_fixtures") or []:
                add(name)
    return out


def summary_of(manifest) -> dict:
    """从清单里抽出给人看的一小段：这个世界要什么、给什么。"""
    if not isinstance(manifest, dict):
        return {}
    plugin = manifest.get("plugin") if isinstance(manifest.get("plugin"), dict) else {}
    world = plugin.get("world") if isinstance(plugin.get("world"), dict) else {}
    actions = [a.get("name") for a in world.get("actions") or [] if isinstance(a, dict)]
    events = [e.get("type") for e in world.get("events") or [] if isinstance(e, dict)]
    return {
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "version": plugin.get("version"),
        "tier": plugin.get("tier"),
        "world": world.get("kind") or "",
        "prefix": world.get("prefix") or "",
        "senses": sorted({e.get("sense") for e in world.get("events") or [] if isinstance(e, dict) and e.get("sense")}),
        "actions": actions,
        "events": events,
        "permissions": plugin.get("permissions") if isinstance(plugin.get("permissions"), dict) else {},
        "separate_permissions": pm.separate_permissions(manifest),
    }


def _text(errors) -> str:
    lines = []
    for issue in errors[:MAX_REPORT_ITEMS]:
        where = issue.path or "清单"
        lines.append("- %s：%s（%s）" % (where, issue.message, issue.code))
    if len(errors) > MAX_REPORT_ITEMS:
        lines.append("- 还有 %d 处，没全列" % (len(errors) - MAX_REPORT_ITEMS))
    return "\n".join(lines)


def inspect(spec, *, source: str = "third_party", files=None) -> dict:
    """装之前的报告。不发请求、不写盘、不执行包里任何代码。"""
    kind = classify(spec)
    issues, warnings = [], []
    _Issue = pm.Issue
    summary = {}
    if files is None and isinstance(spec, dict):
        files = spec.get("files") if isinstance(spec.get("files"), dict) else None

    if kind == "plugin":
        manifest = spec.get("plugin")
        check = list(pm.validate_plugin_manifest(manifest))
        check += list(pm.policy_issues(manifest, source))
        if isinstance(manifest, dict) and spec.get("id") and manifest.get("id") != spec.get("id"):
            check.append(_Issue("plugin.id", "E_ID_MISMATCH", "清单里的 id 和包里的 id 不一致", "error"))
        issues.extend(check)
        summary = summary_of(manifest)
        # 清单点名要带的文件得真在包里；文本包给不了二进制资产，单独提醒一句。
        have = set(files) if isinstance(files, dict) else set()
        for name in _declared_files(manifest):
            if have and Path(name).as_posix() not in have:
                issues.append(_Issue(name, "E_FILE_MISSING", "清单点名要这个文件，包里没有", "error"))
        if not have:
            warnings.append(_Issue("", "W_NO_FILES", "包里没带文件，装进来只能用清单声明的能力", "warn"))
    else:
        if not isinstance(spec, dict):
            issues.append(_Issue("", "E_NOT_OBJECT", "扩展包必须是 JSON 对象", "error"))
        else:
            if not isinstance(spec.get("id"), str) or not pm.ID.match(spec.get("id") or ""):
                issues.append(_Issue("id", "E_ID", "扩展编号只能是小写字母、数字和 -", "error"))
            name = str(spec.get("name") or "").strip()
            if not name or len(name) > 80:
                issues.append(_Issue("name", "E_NAME", "扩展名称需要 1～80 个字符", "error"))
            items = spec.get("files")
            if not isinstance(items, dict) or not items:
                issues.append(_Issue("files", "E_FILES", "扩展包需要 1～64 个文本文件", "error"))
            elif any(not isinstance(v, str) for v in items.values()):
                issues.append(_Issue("files", "E_FILES_TEXT", "扩展文件必须是 UTF-8 文本", "error"))
        summary = {"id": spec.get("id") if isinstance(spec, dict) else None,
                   "name": spec.get("name") if isinstance(spec, dict) else None,
                   "world": "", "actions": [], "events": []}

    errors = [i for i in issues if i.level == "error"] + [i for i in warnings if i.level == "error"]
    return {
        "ok": not errors,
        "kind": kind,
        "contract": {"name": CONTRACT_NAME, "schema": CONTRACT_SCHEMA},
        "summary": summary,
        "issues": [_issue_row(i) for i in issues],
        "warnings": [_issue_row(i) for i in warnings],
        "errors_text": _text(errors),
    }


def gate(spec, *, source: str = "third_party", files=None) -> dict:
    """装的时候用这个：不合约就抛，合约了把报告交回去（调用方可以顺手存一份）。"""
    report = inspect(spec, source=source, files=files)
    if not report["ok"]:
        count = len([i for i in report["issues"] if i["level"] == "error"])
        raise ContractError("不符合%s（%d 处）：\n%s" % (CONTRACT_NAME, count, report["errors_text"]))
    return report


def install_plugin_package(spec, *, source: str = "third_party", plugin_host=None) -> dict:
    """带 plugin 块的世界包 → 真装进插件宿主。

    宿主关着（`WM_PLUGIN_HOST` 不是 on）时不装，只回一句「已存成草稿」，不假装成功。
    """
    kind = classify(spec)
    if kind != "plugin":
        raise ContractError("这不是世界插件包（没有 plugin 块）")
    if plugin_host is None:
        from . import plugin_host as host
    else:
        host = plugin_host
    if not host.switch_on():
        return {"ok": False, "installed": False, "why": "插件宿主关着（WM_PLUGIN_HOST=on 才启用），已存成草稿"}
    manifest = spec["plugin"]
    with tempfile.TemporaryDirectory(prefix="wm-pkg-") as tmp:
        base = Path(tmp)
        (base / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for name, text in (spec.get("files") or {}).items():
            target = (base / name).resolve()
            if base.resolve() not in target.parents:
                raise ContractError("包里有文件想跑到目录外面：%s" % name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
        result = host.install(base)
    return {"ok": bool(result.get("ok")), "installed": bool(result.get("ok")),
            "plugin": result.get("id") or manifest.get("id"),
            "tier": result.get("granted_tier"),
            "why": "" if result.get("ok") else str(result.get("reason") or result.get("error") or result)[:400]}
