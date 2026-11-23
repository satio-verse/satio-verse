# S3「推」的文字层设计：说 / 等 / 沉默、不算一轮、语气标记

2026-11-18。**本文是设计 + 红规格，不含代码改动。**

## 为什么只有设计

`wm/channels/manager.py` 里还有**别的会话未提交的改动**（`git diff --stat`：183 行新增 / 20 行删除，
主要是知乎渠道那批 `zhihu.py` 接入）。硬约束要求不碰、不提交别人的工作，所以本轮只写：

- 本设计说明（落点、改法、取舍、回退）；
- `tests/test_proactive_turn_boundary.py`：5 条验收规格，全部 `xfail`（红规格，不是失败）。
  实现时把模块顶部的 `pytestmark = pytest.mark.xfail(...)` 删掉，就是正式验收。

## 落点（行号按 2026-11-18 的工作区；别人提交后会漂，以 `grep` 为准）

| 位置 | 现在做什么 |
| --- | --- |
| `wm/channels/manager.py:3913 _maybe_proactive` | 到 idle 时间就调 `_reach`；不是 sent/draft 就把重试时间往后推 |
| `wm/channels/manager.py:3961 _reach` | 拼提醒 → `_model(conv.session.nudge, ...)` → `_deliver` → 记 `pro_count / unanswered / her_last_owner` |
| `wm/channels/manager.py:3787 _model` / `:3805 _deliver` | 调一次模型 / 落额度 → 清洗 → 草稿或直发 → `_trace` |
| `wm/channels/manager.py:121 _PRO` / `:566 parse_conf` | proactive 配置项在这里加缺省值 |
| `wm/channels/guest.py:214 nudge` / `:220 _ask` / `:265 _remember` | **禁止改**（别人的文件）。`_ask` 每次都把（提醒, `{"emo","reply"}`）append 进 `session.history` |
| `wm/memory.py:1534 observe` / `:1572` | 桌宠每轮对话 `meta["turns"] += 1`（`wm/pipeline.py:2014` 调） |
| `wm/ledger.py:179 _add` / `:192` / `:194` | 每次模型请求 +1 轮（`local["turns"]` 或 `agg["turns"]`） |
| `wm/channels/manager.py:5619 _trace` | 每笔渠道决策写一行 tracelog（主动的也会写） |

## (a) speak / wait / 沉默（新开关 `proactive.speak_gate`，默认 off）

- `parse_conf` 加一项：`"speak_gate": pro.get("speak_gate") is True`。
  **不写 `config/default.json`**（那是别人的文件，也是线上配置）：缺省就是 `False`。
- gate 关着（默认）= 行为与现在**逐字节一致**，只是代码路径允许 wait。
- gate 开着时，`_reach` 的提醒多一句：这次允许回 `【说】…` / `【等】` / 空（沉默）；
  `_deliver` 之前解析：
  - 说 → 照现在的发送档发；
  - 等 → 不发，`_reach` 返回 `"wait"`，`_maybe_proactive` 把 `pro_retry_at` 推一个时间块（比如 10 分钟），
    下一个 tick 再问她一次（不占 `max_per_day`、不算 `unanswered`）；
  - 沉默 → 不发，`pro_retry_at` 照现在推 `min(3600, idle_h*3600)`，今天不再追。
- 判断仍由她做（模型输出），管理器只执行；gate 只是"允许三选一"的开关。

## (b) 主动搭话不算一轮（ADR-0026）

标记：`_reach` 里 `rec["proactive"] = True`；同时用 ledger 的 contextvar 包住这次 `_model`：

```python
with L.proactive(True):          # wm/ledger.py 新增
    r = await self._model(conv.session.nudge, (note, memory), rec, t0)
```

三处跳过计数（**只跳过"轮数"，花费和 token 照记**，额度不能被主动说话偷掉）：

1. **统计** `wm/ledger.py`
   - 新增 `_proactive` contextvar + `proactive(on)` 上下文管理器（和现有 `charging()` 同一套路，`charging` 在 `ledger.py:62`）；
   - `Ledger.record`（`:359`）读 contextvar，把 `"proactive": True` 写进 rec；
   - `_add`（`:179`）见 `r.get("proactive")`：`cost / hit / miss / out / think` 照加，
     **不**加 `agg["turns"]`、`agg["local"]["turns"]`、`by_who/by_ch/by_model` 的 `turns`、`peak_turns`。
2. **记忆** `wm/memory.py`
   - `observe(..., proactive: bool = False)`；`proactive=True` 时跳过 `meta["turns"] += 1`（`:1572`），
     也不更新 `last_seen/familiarity`（那几项都是"聊过一轮"的派生值）；
   - 说明：现在主动搭话只走 QQ 私聊（`_reach`），根本不调 `observe`，这一处是为桌宠侧主动开口预留的；
     加了 `proactive` 形参但默认值不变，现有调用零影响。
3. **会话历史** `wm/channels/guest.py`（**禁止改** → 在 manager 侧夹一层）
   - `_reach` 调模型前记 `before = len(conv.session.history)`；`_deliver` / 失败分支之后
     `del conv.session.history[before:]`（`_ask` 每轮只 append 一条，重问也是同一条，删一条即可）。
   - 取舍：她主动说的那句不进下一轮 prompt 历史（ADR-0026 的"不算一轮"就是这个意思；
     下一轮从主人回的那句开始）。以后 `guest.py` 腾出来，改成 `nudge(..., proactive=True)`
     内部不 `_remember` 更干净，Manager 侧那两行就能删。
   - 这条删完以后，`session.history` 条数不变 —— 就是验收里"历史条数不变"。

另外 `_trace` 的 rec 已经带 `"kind": "proactive"`，统计页可以再按 `rec["proactive"]` 过滤，不用新字段。

## (c) 语气标记：历史里存、展示文本里不出现（ADR-0007）

现状：`wm/channels/guest.py` 的 `SCHEMA` 只有 `emo + reply` 且 `additionalProperties: false`；
history 存的是 `I.dumps_compact({"emo": ..., "reply": ...})`。**改 schema 要动 `guest.py`（禁止）**，
所以这一刀走带内标记，和现有 `【表情包:心情】`、`【点赞】` 同一个路子：

- 提醒里让模型可以在 reply 末尾写 `【语气:轻】`（可选，不写就是没标记）；
- `guest._ask` 在 manager 拿到 `r` **之前**已经把这个 reply 原样存进 history
  （`clean_out` 不删 `【…】`）→ **历史里有**；
- `_deliver` 里在 `clean_out`/`segment` **之前**先剥：
  `wm/channels/outbound.py` 新增 `strip_tone(text) -> (text, tone | None)`（纯函数，和 `clean_out`/`is_silent` 放一起），
  发出去和界面展示的都只有剥过的文本 → **展示没有**。
- 升级路线（等 `guest.py` 空出来）：模型输出 `{"emo","tone","reply"}`，history 存 `tone` 字段、
  展示不读它 —— 那才是和 `emo` 真正同一个机制；带内标记只是当前约束下的过渡。

## 验收（`tests/test_proactive_turn_boundary.py`，现在全 xfail）

1. `speak_gate` 缺省 `False`，显式开才 `True`；
2. ledger：主动的模型调用 `local["turns"] / agg["turns"]` 不变，`hit/miss/out/cost` 照记；
3. `Memory.observe(..., proactive=True)` 不增加 `关系.md` 的 `turns`；
4. `GuestSession.nudge(..., proactive=True)` 不往 `session.history` 追加；
5. 语气标记：`clean_out` 后仍在（历史有），`strip_tone` 剥掉后展示文本没有。

## 回退

实现提交后任一处出问题：`git checkout -- wm/channels/manager.py wm/ledger.py wm/memory.py wm/channels/outbound.py`
并恢复 `tests/test_proactive_turn_boundary.py` 顶部那行 `pytestmark`（红规格能整除地留在仓库里）。
本设计说明本身不依赖任何代码，可以保留。
