"""Small explicit-command bridge, not a general language planner or physical verifier."""
import math
import re

NAMES = {"coffee": "冲咖啡", "phone": "查看手机", "computer": "使用房间电脑",
         "chair_turn": "转动椅子方向", "inspect_temperature": "检查杯子温度",
         "rest": "在床边休息", "window": "看窗外", "books": "在书架前看看",
         "visit": "过来看看你", "sleep": "上床休息", "yoga": "做瑜伽",
         "read": "在阅读椅休息", "explore": "探索房间", "drink": "喝饮料",
         "milk": "喝牛奶", "snack": "吃零食"}
PATTERNS = {
    "coffee": r"(?:去)?(?:冲|泡|做|煮|喝)(?:一杯|杯|点)?咖啡",
    "phone": r"(?:去)?(?:拿起|看看|看一下|看下|看|玩一下|玩|用一下)(?:你的|那部)?手机",
    "computer": r"(?:去)?(?:(?:坐到|坐在|到)(?:电脑|电脑桌|电脑椅)(?:前|旁|旁边|上)?(?:坐下|坐一会|玩一会)?|玩(?:一会)?电脑|(?:用|使用)(?:一下)?电脑)",
    "chair_turn": r"(?:把)?(?:电竞椅|椅子)(?:转个方向|转一下|转一转)|(?:转动|转一下)(?:电竞椅|椅子)",
    "inspect_temperature": r"(?:看看|检查|感受|摸摸|摸一下)(?:杯子|咖啡|饮料)(?:的)?(?:温度|冷热|烫不烫|热不热)",
    "rest": r"(?:去)?(?:床边|床沿)(?:坐下|坐一会|休息|歇一会)|(?:去)?休息一下",
    "window": r"(?:去)?(?:看看|看|看一下)窗外",
    "books": r"(?:去)?书架(?:前)?(?:看看|看一下|挑书)",
    "visit": r"(?:走)?过来(?:看看我|看我|陪我)?",
    "sleep": r"(?:去)?(?:上床)?(?:睡觉|睡一会|睡一下)|上床休息",
    "yoga": r"(?:去)?做(?:一下|一会)?瑜伽",
    "read": r"(?:去)?(?:坐到|坐在)?阅读椅(?:上)?(?:坐下|坐一会|休息)?",
    "explore": r"(?:去)?(?:探索|逛逛)(?:一下)?(?:房间|小屋)",
    "drink": r"(?:去)?喝(?:一杯|点)?(?:水|饮料)",
    "milk": r"(?:去)?喝(?:一杯|点)?牛奶",
    "snack": r"(?:去)?吃(?:一点|点)?零食",
    "unsupported_chair_tip": r"(?:把)?(?:电竞椅|椅子)(?:翻倒|推倒|翻转)|(?:翻倒|推倒|翻转)(?:电竞椅|椅子)",
}


def command(text):
    """Full utterance only: no quoted/discussed/negated/subordinate commands."""
    text = str(text).strip().strip("。！!？?，, ")
    text = re.sub(r"^(?:小流|绫音)[，,、 ]*", "", text)
    text = re.sub(r"^(?:(?:请|麻烦你|麻烦|你能不能|你能|你可以|帮我|给我|你)[，, ]*)+", "", text)
    text = re.sub(r"(?:吧|呀|吗|好吗|可以吗)$", "", text)
    for act, pattern in PATTERNS.items():
        if re.fullmatch(pattern, text):
            return act
    return None


def capabilities(value):
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("activities must be a bounded list")
    result = {}
    for entry in value:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] not in NAMES:
            continue  # Existing rooms have extra affordances; keep supported activities.
        if (not isinstance(entry, dict) or entry.get("id") not in NAMES
                or type(entry.get("enabled")) is not bool or entry["id"] in result):
            raise ValueError("invalid activity capability")
        result[entry["id"]] = entry["enabled"]
    return result


def timeout_seconds(value=180):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return 180.0
    return min(180.0, max(1.0, float(value)))


def terminal(msg):
    """A queue acknowledgement cannot be converted into completed success."""
    started, completed = msg.get("started") is True, msg.get("completed") is True
    ok = msg.get("ok") is True and started and completed
    result = {"ok": ok, "started": started, "completed": completed,
              "reason": str(msg.get("reason") or ("" if ok else "incomplete"))[:120]}
    # New clients distinguish a cancellation acknowledgement from confirmed cleanup.
    # Old clients remain compatible, but contradictory lifecycle evidence cannot pass.
    if any(key in msg for key in ("outcome", "cleanupConfirmed", "cancelled")):
        outcome = msg.get("outcome")
        valid = (("outcome" not in msg or outcome in ("completed", "failed", "cancelled", "cancel_unconfirmed"))
                 and all(key not in msg or type(msg[key]) is bool for key in ("cleanupConfirmed", "cancelled")))
        confirmed = msg.get("cleanupConfirmed") is True and outcome != "cancel_unconfirmed"
        cancelled = msg.get("cancelled") is True or outcome in ("cancelled", "cancel_unconfirmed")
        ok = ok and valid and confirmed and not cancelled and ("outcome" not in msg or outcome == "completed")
        result.update(ok=ok, completed=completed if ok else False, cleanupConfirmed=confirmed,
                      cancelled=cancelled,
                      outcome="completed" if ok else ("cancelled" if confirmed else "cancel_unconfirmed") if cancelled else "failed")
        if not ok and not msg.get("reason"):
            result["reason"] = ("cancelled" if confirmed else "cancel_unconfirmed") if cancelled else ("incomplete" if confirmed else "cleanup_unconfirmed")
    detail = msg.get("detail")
    if isinstance(detail, str):
        detail = {"text": detail}
    if isinstance(detail, dict):
        clean = {k: detail[k][:240] for k in ("text", "category") if isinstance(detail.get(k), str)}
        if type(detail.get("known")) is bool:
            clean["known"] = detail["known"]
        result["detail"] = clean
    return result


def reply(act, result):
    name = NAMES.get(act, "这个活动")
    if act == "unsupported_chair_tip":
        return "这个动作没有完成：翻倒椅子尚未实现，只支持转向。"
    if result["ok"]:
        detail = result.get("detail") or {}
        if act == "inspect_temperature":
            text = detail.get("text") or "温度未知，尚未获得可靠的观察"
            if detail.get("known") is not True:
                return f"小屋回报：温度仍未知。{text}"
            return f"小屋回报：{text}"
        extra = f"{detail['text']}" if detail.get("text") else ""
        return f"小屋回报：{name}的活动流程已完成。{extra}"
    why = {"unsupported": "当前小屋还不能执行", "busy": "正在进行另一个活动",
           "timeout": "没有在等待时间内收到完成回执", "scene_changed": "房间已经切换",
           "incomplete": "没有收到完整的执行完成回执", "cancelled": "活动已取消"}.get(result["reason"], str(result.get("reason") or "执行没有完成")[:120])
    if result.get("cleanupConfirmed") is False:
        why = "动作收尾尚未确认，不能确认角色已停止"
        if result.get("reason") not in (None, "", "incomplete", "cancelled", "cancel_unconfirmed", "cleanup_unconfirmed"):
            why += "；" + str(result["reason"])[:120]
    elif result.get("cancelled") is True:
        why = "活动已取消，收尾流程已结束"
    detail = (result.get("detail") or {}).get("text", "")
    return f"{name}没有完成：{why}。{detail}"
