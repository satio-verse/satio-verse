"""下载队列写给人看的字: 问你的卡片 (名称、来源、大小、许可)、下完的回报、给她看的一行状态.
名称、大小、许可是她填的 (她查到的), 卡片上照实标着; 来源是程序从地址里取的, 不是她写的."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from . import STATE_TEXT, TERMINAL, Limits, human_size, human_time
from .rules import EXECUTABLE, origin


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _mid(text: str, n: int) -> str:
    """太长的地址留头尾 (域名和文件名都看得到)."""
    return text if len(text) <= n else text[:n * 2 // 3] + "…" + text[-(n // 3 - 1):]


def _size_line(job: dict, limits: Limits) -> str:
    declared = job.get("size_declared")
    if declared:
        return f"{human_size(declared)}（她查到的；实际更大会先停下再问你）"
    return f"未知（实际超过 {human_size(limits.unknown_ok_bytes)} 会先停下再问你）"


def route_text(job: dict, proxy: str | None) -> str:
    if job.get("route") == "direct" or not proxy:
        return "直连"
    u = urlsplit(proxy)
    return f"经本机代理 {u.hostname}:{u.port}" if u.hostname else "经代理"


def card(job: dict, limits: Limits, root: Path | None = None, proxy: str | None = None) -> str:
    """页面上那张卡片的正文 (完整的): 名称、来源、大小、许可, 再加存到哪 (完整路径)、有没有哈希、怎么连."""
    ask = job.get("ask") or {}
    why = ask.get("why") or "new"
    hosts = [origin(u) for u in job["urls"]]
    lines = []
    if why == "redirect":
        lines.append(f"下载 {job['id']} 要转去卡片上没列的来源：{ask.get('host')}（从 {ask.get('from')} 转过去的）。连不连？")
    elif why == "size":
        was = human_size(job["size_declared"]) if job.get("size_declared") else "未知"
        lines.append(f"下载 {job['id']} 实际大小是 {human_size(ask.get('total'))}，卡片上写的是{was}。还下不下？")
    lines.append(f"名称：{_clip(job['name'], 80)}")
    lines.append("来源：" + "、".join(dict.fromkeys(hosts)))
    lines += [f"  {_mid(u, 160)}" for u in job["urls"]]
    extra = [h for h in job.get("hosts") or [] if h not in hosts]
    if extra:
        lines.append("会转到：" + "、".join(extra))
    lines.append("大小：" + (human_size(ask.get("total")) + "（服务器报的）" if why == "size" else _size_line(job, limits)))
    lines.append("许可：" + (f"{_clip(job['license'], 60)}（她查到的，没有核实）" if job.get("license") else "未知"))
    lines.append("哈希：" + ("有 SHA-256，下完核对，对不上会删掉" if job.get("sha256") else "没有（下完没法核对内容）"))
    lines.append(f"存到：{job['dest']}")
    lines.append("走法：" + route_text(job, proxy))
    if Path(job["dest"]).suffix.lower() in EXECUTABLE:
        lines.append("注意：这是能直接运行的文件；下完不会自动运行。")
    if job.get("note"):
        lines.append(f"用途：{_clip(job['note'], 120)}")
    return "\n".join(lines)


def card_short(job: dict, limits: Limits) -> str:
    """桌宠对话栏和 QQ 上问你的那一句 (300 字以内才会上 QQ): 四样都在, 地址只留域名."""
    ask = job.get("ask") or {}
    why = ask.get("why") or "new"
    hosts = "、".join(dict.fromkeys(origin(u) for u in job["urls"]))
    if why == "redirect":
        return _clip(f"{job['id']}「{_clip(job['name'], 30)}」要从 {ask.get('from')} 转去没列过的来源 {ask.get('host')}，连不连", 280)
    if why == "size":
        was = human_size(job["size_declared"]) if job.get("size_declared") else "未知"
        return _clip(f"{job['id']}「{_clip(job['name'], 30)}」实际大小 {human_size(ask.get('total'))}（之前写的是{was}），还下不下", 280)
    size = human_size(job["size_declared"]) if job.get("size_declared") else "未知"
    return _clip(f"{job['id']}「{_clip(job['name'], 40)}」｜来源 {_clip(hosts, 70)}｜大小 {size}｜许可 {_clip(job.get('license') or '未知', 30)}"
                 f"｜存到 {_clip(Path(job['dest']).name, 50)}" + ("｜有哈希" if job.get("sha256") else "｜没有哈希"), 280)


def progress(job: dict) -> str:
    done, total = int(job.get("done") or 0), int(job.get("total") if job.get("total") is not None else -1)
    if total > 0:
        return f"{human_size(done)} / {human_size(total)}（{done * 100 // total}%）"
    return f"{human_size(done)}（总大小未知）" if done else ""


def state_line(job: dict) -> str:
    """一句话说这个下载现在怎样 (给她和页面看)."""
    state = job["state"]
    head = f"{job['id']}「{_clip(job['name'], 40)}」{STATE_TEXT.get(state, state)}"
    if state == "awaiting":
        why = (job.get("ask") or {}).get("why")
        return head + {"redirect": "：要转去没列的来源，等主人定", "size": "：实际比卡片上写的大，等主人定"}.get(why, "：卡片弹出去了，点了才开始传")
    if state in ("running", "queued", "paused"):
        tail = progress(job)
        if state == "running" and job.get("speed"):
            tail += f"，{human_size(job['speed'])}/秒"
            if job.get("eta_s") is not None:
                tail += f"，还要约 {human_time(job['eta_s'])}"
        if state == "paused":
            tail += "，主人暂停的" if job.get("pause_by") == "user" else ""
        if job.get("error"):
            tail += f"；上一次没连上：{_clip(job['error'], 80)}（在重试）"
        return head + (f"：{tail}" if tail else "")
    if state == "done":
        return head + f"：{human_size((job.get('final') or {}).get('bytes'))}，存在 {job['dest']}"
    if state == "failed":
        return head + f"：{_clip(job.get('error') or '原因不明', 160)}"
    return head


def report(job: dict) -> dict:
    """结束了的下载交回发起的会话时说的话: {how, text (一句), body (交给她的那段), hint (怎么看详情)}."""
    state, jid, name = job["state"], job["id"], _clip(job["name"], 60)
    took = human_time((job.get("ended") or 0) - (job.get("approved_at") or job.get("created") or 0))
    final = job.get("final") or {}
    if state == "done":
        checked = "SHA-256 对上了" if final.get("verified") else "没有哈希可核对"
        text = f"下载 {jid}「{name}」下完了（{human_size(final.get('bytes'))}，{checked}，从点头到下完 {took}）"
        more = f"\n存在：{job['dest']}\nSHA-256：{final.get('sha256') or '没算出来'}\n文件只是下好了，没有运行、没有解压。"
    elif state == "failed":
        text = f"下载 {jid}「{name}」没成：{_clip(job.get('error') or '原因不明', 200)}"
        more = "\n要重试用 download_resume（会从断的地方接着下）；换来源要重新排一个（主人要再点一次头）。" \
            if job.get("error_kind") not in ("hash", "private", "dest") else ""
    elif state == "denied":
        text = f"主人没同意下载 {jid}「{name}」，没有下"
        more = "\n别换个办法自己去下；需要的话问主人为什么、有没有别的选择。"
    else:
        text = f"下载 {jid}「{name}」被取消了"
        more = ""
    return {"id": jid, "name": name, "how": state, "code": None, "took": took, "text": text,
            "body": f"[下载有结果了 · {jid}]\n{text}。{more}", "hint": f"详情用 download_status(id='{jid}')", "what": "下载"}


def view(job: dict, viewer: str = "") -> dict:
    """给她和页面看的一个任务 (不带令牌、审计这些内部的)."""
    out = {"id": job["id"], "name": job["name"], "state": job["state"], "text": state_line(job),
           "mine": bool(viewer) and viewer in (job.get("owner"), job.get("origin")),
           "owner": job.get("owner", ""), "owner_title": job.get("owner_title", ""),
           "urls": list(job["urls"]), "dest": job["dest"], "total": job.get("total", -1), "done": job.get("done", 0),
           "created": job.get("created"), "updated": job.get("updated")}
    for key in ("speed", "eta_s", "error", "sha256", "license", "size_declared", "pause_by", "ended", "note"):
        if job.get(key) not in (None, ""):
            out[key] = job[key]
    if job["state"] == "awaiting":
        out["ask"] = {k: v for k, v in (job.get("ask") or {}).items() if k in ("seq", "why", "card", "host", "from", "total")}
    if job["state"] in TERMINAL and job.get("final"):
        out["final"] = job["final"]
    return out
