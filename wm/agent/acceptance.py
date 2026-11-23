"""三层验收 (2026-11-01 用户"好，接着做": 升级方案第 3 步剩下的一块; 研究总报告第七节第 10 个验收用例"工具返回成功但
产物缺失: 任务标失败或待核查, 桌宠不汇报完成"; Codex 13、bili-cli"预演模式还是发出去了"): 一轮做完时程序分开记三件事 ——
  说: 这一轮是她自己说完的, 还是停了、到预算、出错;
  做: 工具出错几次, 命令的退出码 (最后一次命令成没成);
  真: 结果是不是真的在 —— 这一轮登记的文件还在不在、是不是登记时那一版; 她最后那段话里说"写到 / 生成了 / 保存到"的文件在不在.
只用程序核对, 不让模型自己判自己; 判不出来的 (写得好不好) 照旧留给你在成果页验收.

明确对不上 (说出来的文件找不到、说成了但最后一次命令失败了) 才算"待核查"; 她写过又删掉、也没再提的临时文件只记一笔.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

_VERB = re.compile(r"(?:写(?:到|在|进|入|好|成)|生成|建(?:了|好)|新建|创建|保存|输出到|输出为|导出|产出|放(?:到|在|进)|存(?:到|在|成|为|进|好)"
                   r"|改好|更新(?:了|好)|另存|就在|已在|都在|位于)")
_TICK = re.compile(r"`([^`\n]{1,240})`")
_BARE = re.compile(r"(?<![\w/\\.:-])((?:[A-Za-z]:[\\/])?(?:[\w一-鿿.-]+[\\/])*[\w一-鿿-][\w一-鿿.-]*"
                   r"\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/\\-])")
_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_CLAUSE = re.compile(r"[，。；！？,;!?\n]")
_NOT_YET = re.compile(r"会|将|要|打算|准备|还没|没有|没|未|待|计划|稍后|之后再|下一步")  # "会写到"、"还没生成": 不是说已经做了
_OK_WORDS = re.compile(r"通过|成功|都过了|全过|没问题|搞定|跑通|全部完成|可以用了|正常了|修好了")
_BAD_WORDS = re.compile(r"失败|没过|没通过|不通过|报错|出错|没成功|没做成|做不了|无法|还有.{0,6}问题|卡在")
# 核对一次以后她如实说了没做成 / 找不到: 不再算对不上 (说错了改口就好)
_ADMIT = re.compile(r"没(?:有)?(?:生成|做成|写成|建成|打成|成功|做出来|写出来)|不存在|找不到|没找到|失败|还没|没能|做不了|无法|说错了")
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
WALK_LIMIT = 5000


def claimed_paths(text: str) -> list[str]:
    """她最后那段话里说"写到 / 生成了 / 保存到 …"的文件 (带后缀名的; 网址、版本号、没后缀的不算). 动词要和文件在同一小句里
    (逗号、句号隔开的不算: "生成了 a.png，把旧的 b.png 删了"只算 a.png), 动词前面有"会、要、还没"的不算 (还没做)."""
    text = str(text or "")
    out: list[str] = []
    spans = [(m.start(), m.end(), m.group(1).strip()) for m in _TICK.finditer(text)]
    ticked = [(a, b) for a, b, _ in spans]
    spans += [(m.start(), m.end(), m.group(1)) for m in _BARE.finditer(text)
              if not any(a <= m.start() < b for a, b in ticked)]
    for start, _end, token in sorted(spans):
        token = token.strip().strip("'\"")
        if not _EXT.search(token) or "://" in text[max(0, start - 12):start + 3] or token.lower().startswith(("http", "www.")):
            continue
        clause = _CLAUSE.split(text[max(0, start - 40):start])[-1]
        verb = None
        for verb in _VERB.finditer(clause):
            pass
        if verb is not None and not _NOT_YET.search(clause[:verb.start()]) and token not in out:
            out.append(token)
    return out


def _walk(root: Path):
    seen = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
        for f in files:
            seen += 1
            if seen > WALK_LIMIT:
                return
            yield Path(d) / f


def find(token: str, workspace: Path, names: set[str]) -> bool:
    """说出来的那个文件找得到吗: 照原样 (绝对 / 相对工作区) 在; 或者这一轮登记过同名的; 或者工作区里有同名文件 (路径说短了)."""
    p = Path(token.replace("\\", os.sep).replace("/", os.sep))
    if (p if p.is_absolute() else workspace / p).exists():
        return True
    if p.name in names:
        return True
    return any(f.name == p.name for f in _walk(workspace)) if workspace.is_dir() else False


SAID = {"running": "done", "completed": "done", "cancelled": "stopped", "budget_exhausted": "budget", "failed": "error",
        "interrupted": "interrupted"}


def check_run(deliveries, s, final_text: str = "", state: str = "", flagged=(), cite_check=None) -> dict:
    """这一轮的三层核对 (在线程里跑: 要重算这一轮登记过的文件的哈希、可能翻工作区找同名文件).
    flagged: 上一次核对 (让她核对之前) 找不到的文件 —— 她核对完只说"就在那儿"、其实还是没有, 照样算对不上 (除非她如实说了没做成).
    cite_check: 资料本的出处核对 (notebook.NotebookStore.citation_check): 最后那段话里引的 [S3-17] 这个会话里都查到过吗.
    返回 {said, tools, real, verdict ('ok' | 'needs_check'), reasons, notes, checked_at}."""
    run, ws = s.run, Path(s.workspace)
    try:
        st = deliveries.state(s.id, run["id"], refresh=True) if run.get("id") else {"artifacts": [], "evidence": []}
    except (ValueError, OSError):
        st = {"artifacts": [], "evidence": []}
    arts, evidence = st.get("artifacts") or [], st.get("evidence") or []
    missing = [a for a in arts if a.get("status") == "missing"]
    changed = [a for a in arts if a.get("status") == "changed"]
    names = {a.get("name") for a in arts if a.get("status") in ("current", "changed")}
    claimed = claimed_paths(final_text)
    claimed_missing = [c for c in claimed if not find(c, ws, names)]
    still = [c for c in flagged if c not in claimed_missing and not find(c, ws, names) and not _ADMIT.search(final_text or "")]
    # 登记过、后来不在了、她最后那段话里又提到了 (按文件名): 也是"说出来的找不到"
    mentioned_gone = [a["path"] for a in missing if a.get("name") and a["name"] in final_text
                      and not any(Path(c).name == a["name"] for c in claimed_missing)]
    last = evidence[-1] if evidence else None
    said = SAID.get(state or run.get("state", ""), state or run.get("state", ""))
    reasons, notes = [], []
    for c in claimed_missing:
        reasons.append(f"提到写好的 {c} 在工作区里找不到")
    for c in still:
        reasons.append(f"核对以后 {c} 还是找不到")
    for p in mentioned_gone:
        reasons.append(f"这一轮写过的 {p} 现在不在了，最后那段话里还提到了它")
    if last and last.get("exit_code") not in (0, None) and _OK_WORDS.search(final_text or "") and not _BAD_WORDS.search(final_text or ""):
        reasons.append(f"最后一次命令（{str(last.get('command') or '')[:80]}）退出码 {last['exit_code']}，最后那段话却说成了")
    if cite_check is not None:
        try:
            cites = list(cite_check(final_text or ""))
        except Exception:  # noqa: BLE001 (资料本读不了: 这一层不判, 记一笔)
            cites, notes = [], notes + ["资料本出处没核对成（读不了资料本）"]
        reasons.extend(cites)
    for a in missing:
        if a["path"] not in mentioned_gone:
            notes.append(f"这一轮写过的 {a['path']} 后来不在了（没再提，可能是她删的临时文件）")
    for a in changed:
        notes.append(f"{a['path']} 登记以后又被改过")
    if run.get("tool_errors"):
        notes.append(f"工具出错 {run['tool_errors']} 次")
    failed = [e for e in evidence if e.get("status") == "failed"]
    return {"said": said,
            "tools": {"errors": int(run.get("tool_errors") or 0), "commands": len(evidence), "failed_commands": len(failed),
                      "last_command": {"command": str(last.get("command") or "")[:200], "exit_code": last.get("exit_code")} if last else None},
            "real": {"artifacts": len(arts), "missing": [a["path"] for a in missing], "changed": [a["path"] for a in changed],
                     "claimed": claimed, "claimed_missing": claimed_missing + still},
            "verdict": "needs_check" if reasons and said == "done" else "ok", "reasons": reasons, "notes": notes,
            "checked_at": time.time()}


def recheck_text(check: dict) -> str:
    """对不上时给她的那段 (状态数据, 不是新指令): 程序核对出了什么, 让她核对一次 —— 真没做成就补上或如实说, 只是说错了就更正."""
    lines = "\n".join(f"- {r}" for r in check["reasons"])
    return (f"[程序核对 · 你说做完了，但有对不上的]\n{lines}\n\n（这是工作台程序核对的结果，不是主人的新指令。"
            "请核对一次：真没做成的就补上或者如实说明，已经做成只是路径或说法错了就更正；不要不核对就说做完了。）")
