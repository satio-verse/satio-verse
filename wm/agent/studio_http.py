"""Trusted workbench HTTP controls. The app must check host/origin before dispatch.

Creator previews are returned as data for a sandboxed iframe, never as app HTML.
User-owned regression suites intentionally have no equivalent model tool.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
from pathlib import Path

from aiohttp import web

from .context import MAX_FILE, identifier
from .permissions_http import RevisionConflict, check_revision


UPLOAD_LIMIT = ((MAX_FILE + 2) // 3) * 4 + 64 * 1024
GLOBAL_MUTATIONS = {"rule_add", "rule_update", "rule_remove", "extension_enable", "extension_rollback",
                    "suite_save", "growth_evaluate", "growth_adopt"}


def _text(data, key, *, limit=200, optional=False):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"{key} 必须是有效文字（最多 {limit} 字）")
    return value


def _bool(data, key):
    if not isinstance(data.get(key), bool):
        raise ValueError(f"{key} 必须是布尔值")
    return data[key]


def _session(hub, sid):
    sid = identifier(sid)
    session = hub.get(sid)
    if session is None:
        raise ValueError("没有这个会话")
    return session


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    return value


def _response(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _run_records(hub, session):
    """Count finished task runs, never messages or assistant self-assessments."""
    folder = Path(hub.home) / "runs"
    records, truncated = [], False
    if folder.is_dir():
        paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                if not path.resolve().is_relative_to(folder.resolve()) or path.stat().st_size > 1024 * 1024:
                    continue
                row = json.loads(path.read_text("utf-8"))
                if not isinstance(row, dict) or row.get("session") != session.id:
                    continue
                if len(records) >= 500:
                    truncated = True
                    break
                # A user can inspect goals and results, but never raw model messages.
                records.append({k: v for k, v in row.items() if k in {
                    "id", "session", "title", "goal", "started", "ended", "state", "reason", "cost", "child_cost",
                    "steps", "tool_errors", "approvals", "verification", "review_note", "reviewed", "model", "mode", "summary", "plan", "resume_from_run"}})
            except (OSError, ValueError, TypeError):
                continue
    def cost(row):
        value = row.get("cost", 0)
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else 0
    accepted = sum(r.get("verification") == "accepted" for r in records)
    rejected = sum(r.get("verification") == "rejected" for r in records)
    return {"scope": "current_session", "runs": len(records), "accepted": accepted, "rejected": rejected,
            "unreviewed": len(records) - accepted - rejected, "cost": round(sum(map(cost, records)), 6),
            "records": records, "truncated": truncated}


def _growth(hub, session):
    return {**_run_records(hub, session), "suites": hub.growth.suites(), "reports": hub.growth.reports()}


def _denied(hub, session, tool, args):
    denied = hub.blacklist.check(tool, args, Path(session.workspace))
    if denied:
        raise PermissionError(denied.why)


def _growth_launches(hub, session, package_id, revision, suite):
    """Check both the exact candidate and the active baseline before any launch."""
    candidate = hub.extensions.candidate_server(package_id, revision)
    configs = [candidate]
    if candidate["enabled"] and candidate["active_revision"]:
        configs.append(hub.extensions.candidate_server(package_id, candidate["active_revision"]))
    for config in configs:
        launch = {"command": " ".join([str(config.get("command") or ""), *map(str, config.get("args") or [])]),
                  "workdir": config.get("cwd", session.workspace), "url": config.get("url", "")}
        _denied(hub, session, "mcp_connect", launch)
        for case in suite["cases"]:
            _denied(hub, session, "mcp_call", {"server_id": config["id"], "tool": case["tool"], "arguments": case["arguments"]})
            _denied(hub, session, f"mcp.{config['id']}.{case['tool']}", case["arguments"])


def _can_run(hub, session, body):
    if _bool(body, "authorized") is not True:
        raise PermissionError("请明确授权运行或采用这个扩展版本")
    if hub.teams.effective_mode(session) == "read" or session.preset == "plan":
        raise PermissionError("只读或计划模式不能运行评测或采用扩展，请先调整任务权限")


def _report(hub, report_id):
    return {"report": hub.growth._report(report_id), "adoption": hub.growth.validate(report_id)}


async def _post(hub, session, body):
    action = _text(body, "action", limit=60)
    if action in GLOBAL_MUTATIONS and session.parent_id:
        raise PermissionError("子会话不能管理全局约束、扩展或固定题集，请回到主任务操作")
    if action == "rule_add":
        rule = hub.blacklist.add(_object(body.get("rule")), by="user")
        return {"rule": rule, "blacklist": hub.blacklist.list(), "audit": hub.blacklist.audit()}
    if action == "rule_update":
        rule = hub.blacklist.update(_text(body, "rule_id"), _object(body.get("changes")), by="user")
        return {"rule": rule, "blacklist": hub.blacklist.list(), "audit": hub.blacklist.audit()}
    if action == "rule_remove":
        removed = hub.blacklist.remove(_text(body, "rule_id"), by="user")
        return {"removed": removed, "blacklist": hub.blacklist.list(), "audit": hub.blacklist.audit()}
    if action in {"extension_enable", "extension_rollback"}:
        package_id = _text(body, "package_id")
        _denied(hub, session, action, {"id": package_id})
        if action == "extension_enable":
            revision = _text(body, "revision", optional=True) or None
            item = hub.extensions.set_enabled(package_id, _bool(body, "enabled"), authorized=True, revision=revision, by="user")
        else:
            item = hub.extensions.rollback(package_id, _text(body, "revision"), authorized=True, by="user")
        return {"extension": item, "extensions": hub.extensions.list()}
    if action == "resume":
        error = await hub.resume(session.id)
        if error:
            raise ValueError(error)
        return {"run": session.run, "queue": session.queue}
    if action == "queue":
        ids = body.get("context_ids", [])
        if not isinstance(ids, list) or len(ids) > 24 or any(not isinstance(i, str) for i in ids):
            raise ValueError("context_ids 必须是最多 24 项的编号数组")
        item = await hub.enqueue(session.id, _text(body, "text", limit=100000), ids)
        return {"item": item, "queue": session.queue}
    if action == "queue_remove":
        qid = _text(body, "queue_id")
        item = next((q for q in session.queue if q.get("id") == qid), None)
        if item is None:
            raise ValueError("队列项不存在或已经开始执行")
        session.queue.remove(item)
        hub.save(session)
        await hub.emit(session, {"type": "queued", "queue": session.queue})
        return {"queue": session.queue}
    if action == "review":
        versions = _object(body['artifact_versions']) if 'artifact_versions' in body else None
        result = await asyncio.to_thread(hub.review, session.id, _text(body, "run_id"), _bool(body, "accepted"), _text(body, "note", limit=2000, optional=True), artifact_versions=versions)
        return {"run": result, "growth": await asyncio.to_thread(_growth, hub, session)}
    if action == "delivery_read":
        rid, aid = _text(body, "run_id"), _text(body, "artifact_id")
        hub.delivery_access(session, aid, rid)
        return {"artifact": await asyncio.to_thread(hub.deliveries.read, session.id, aid, run_id=rid, offset=body.get('offset', 0), limit=body.get('limit', 12000))}
    if action == "budget":
        values = {k: body[k] for k in ("budget_cny", "time_budget_s", "max_steps") if k in body}
        if not values or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values.values()):
            raise ValueError("预算必须是有效数字")
        if "max_steps" in values and (values["max_steps"] < 0 or int(values["max_steps"]) != values["max_steps"]):
            raise ValueError("max_steps 必须是非负整数，0 表示不另设限制")
        updated = hub.settings(session.id, **values)
        return {k: getattr(updated, k) for k in ("budget_cny", "time_budget_s", "max_steps")}
    if action in {"worktree_diff", "worktree_export"}:
        wid = hub.studio._worktree(session, _text(body, "worktree_id"))
        _denied(hub, session, action, {"id": wid})
        method = hub.teams.worktrees.diff if action == "worktree_diff" else hub.teams.worktrees.export_patch
        return await asyncio.to_thread(method, wid)
    if action == "context_reference":
        item = hub.contexts.add_reference(session.id, _text(body, "value", limit=4096))
        return {"item": item, "contexts": hub.contexts.list(session.id)}
    if action in {"context_inspect", "context_read"}:
        rid = identifier(_text(body, "context_id"))
        item = next((x for x in hub.contexts.list(session.id) if x['id'] == rid), None)
        if item is None:
            raise ValueError('材料不属于当前会话或已移除')
        _denied(hub, session, action, {'id': rid})
        metadata = await asyncio.to_thread(hub.contexts.inspect, session.id, rid, snapshot=[item])
        if action == 'context_inspect':
            return {'item': metadata}
        text = await asyncio.to_thread(hub.contexts.read, session.id, rid, body.get('offset', 0), body.get('limit', 12000), snapshot=[item], page=body.get('page'), sheet=body.get('sheet'))
        more = re.search(r'\n\[后续用 offset=(\d+)\]\Z', text)
        return {'item': metadata, 'text': text, 'offset': body.get('offset', 0), 'next_offset': int(more[1]) if more else None,
                'selection': {'page': body.get('page'), 'sheet': body.get('sheet')}}
    if action in {"context_pin", "context_remove"}:
        rid = identifier(_text(body, "context_id"))
        kwargs = {"pinned": _bool(body, "pinned")} if action == "context_pin" else {"remove": True}
        return {"contexts": hub.contexts.update(session.id, rid, **kwargs)}
    if action == "suite_save":
        return {"suite": hub.growth.save_suite(_object(body.get("spec")), by="user")}
    if action in {"growth_report", "report"}:
        return _report(hub, _text(body, "report_id"))
    if action == "growth_evaluate":
        _can_run(hub, session, body)
        package_id, revision, suite_id = (_text(body, k) for k in ("package_id", "revision", "suite_id"))
        _denied(hub, session, action, {"package_id": package_id, "revision": revision, "suite_id": suite_id})
        suite = hub.growth._suite(suite_id)
        if suite["package_id"] != package_id:
            raise ValueError("题集不属于这个扩展")
        _growth_launches(hub, session, package_id, revision, suite)
        if session.status == "idle" and session.stop_event.is_set():
            session.stop_event = asyncio.Event()
        report = await hub.growth.evaluate(package_id, revision, suite_id, stop=session.stop_event, authorized=True)
        return {"report": report, "adoption": hub.growth.validate(report["id"])}
    if action == "growth_adopt":
        _can_run(hub, session, body)
        report_id = _text(body, "report_id")
        report = hub.growth._report(report_id)
        _denied(hub, session, action, {"report_id": report_id, "package_id": report["package_id"]})
        _denied(hub, session, "extension_enable", {"id": report["package_id"]})
        _growth_launches(hub, session, report["package_id"], report["candidate"]["revision"], hub.growth._suite(report["suite_id"]))
        return hub.growth.adopt(report_id, authorized=True)
    raise ValueError("不支持的工作台操作")


async def studio(request, hub):
    """GET/POST /api/agent/studio; root must apply its existing ws_refusal first."""
    try:
        if request.method == "GET":
            session = _session(hub, request.query.get("id", ""))
            if "preview" in request.query:
                return _response(hub.extensions.preview(request.query["preview"], request.query.get("revision")))
            if "report" in request.query:
                return _response(_report(hub, request.query["report"]))
            growth = await asyncio.to_thread(_growth, hub, session)
            record = hub.run_record(session.id, request.query.get('run_id', ''))
            deliveries = {'artifacts': [], 'evidence': [], 'reviews': []}
            if record.get('id'):
                refresh = request.query.get('deliveries') == '1'
                if refresh:
                    for item in hub.deliveries.list(session.id, record['id'], limit=100)['items']:
                        hub.delivery_access(session, item['id'], record['id'])
                deliveries = await asyncio.to_thread(hub.deliveries.state, session.id, record['id'], refresh=refresh)
            records = [r for r in growth['records'] if r['id'] != session.run.get('id')]
            if session.run.get('id'):
                records.insert(0, session.run)
            return _response({"blacklist": hub.blacklist.list(), "audit": hub.blacklist.audit(),
                "authority": hub.blacklist.snapshot(),
                "extensions": hub.extensions.list(), "contexts": hub.contexts.list(session.id), "team": hub.teams.list(session.id),
                "run": session.run, "plan": session.plan, "queue": session.queue, "growth": growth,
                "deliveries": deliveries, "delivery_run": record, "delivery_runs": records})
        if request.method != "POST":
            raise ValueError("只支持 GET 或 POST")
        body = _object(await request.json())
        session = _session(hub, body.get("id", ""))
        if body.get('action') in {'rule_add', 'rule_update', 'rule_remove'}:
            with hub.blacklist._lock:
                check_revision(hub.blacklist, body)
                return _response(await _post(hub, session, body))
        return _response(await _post(hub, session, body))
    except RevisionConflict as exc:
        return _response({'error': str(exc), 'revision': hub.blacklist.snapshot()['revision']}, 409)
    except asyncio.CancelledError:
        return _response({"error": "评测已停止，已完成的用例保存在报告中"}, 400)
    except (ValueError, TypeError, KeyError, PermissionError) as exc:
        return _response({"error": str(exc)[:500]}, 400)
    except OSError:
        return _response({"error": "本地文件读写失败，请检查保存目录"}, 400)


async def context_upload(request, hub):
    """POST /api/agent/context?id=SID, JSON {name,data:base64}; no auto execution."""
    try:
        if request.method != "POST":
            raise ValueError("上传附件只支持 POST")
        session = _session(hub, request.query.get("id", ""))
        if request.content_length is not None and request.content_length > UPLOAD_LIMIT:
            raise ValueError(f"上传请求过大，单个附件最多 {MAX_FILE // (1024 * 1024)} MB")
        cloned = request.clone(client_max_size=UPLOAD_LIMIT)
        body = _object(await cloned.json())
        name = _text(body, "name", limit=200)
        encoded = _text(body, "data", limit=((MAX_FILE + 2) // 3) * 4, optional=True)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("附件 data 必须是有效 Base64") from exc
        if len(data) > MAX_FILE:
            raise ValueError(f"单个附件最多 {MAX_FILE // (1024 * 1024)} MB")
        item = hub.contexts.add_file(session.id, name, data)
        return _response({"item": item, "contexts": hub.contexts.list(session.id)})
    except web.HTTPRequestEntityTooLarge:
        return _response({"error": f"上传请求过大，单个附件最多 {MAX_FILE // (1024 * 1024)} MB"}, 400)
    except (ValueError, TypeError, KeyError) as exc:
        return _response({"error": str(exc)[:500]}, 400)
    except OSError:
        return _response({"error": "附件保存失败，请检查保存目录"}, 400)


async def context_image(request, hub):
    """GET /api/agent/context/image?id=SID&item=RID: 把附件里的图片原样给页面看 (对话里的缩略图).
    只读; 路径只从 ContextStore 的 _folder/_item 拿, 不接受用户拼的路径."""
    if request.method != "GET":
        return _response({"error": "看图片只支持 GET"}, 405)
    try:
        session = _session(hub, request.query.get("id", ""))
        item = hub.contexts._item(session.id, request.query.get("item", ""))
        if item.get("kind") != "file" or not item.get("file"):
            raise ValueError("这个附件不是文件")
        _, data = await asyncio.to_thread(hub.contexts._file_data, session.id, item)
        # User attachments must never become executable documents on the app
        # origin. Check actual bytes, rather than trusting a filename suffix.
        ctype = None
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            ctype = 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            ctype = 'image/jpeg'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            ctype = 'image/gif'
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ctype = 'image/webp'
        elif data.startswith(b'BM'):
            ctype = 'image/bmp'
        if ctype is None:
            raise ValueError('仅支持 PNG、JPEG、GIF、WebP 和 BMP 图片预览')
        return web.Response(body=data, content_type=ctype, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "sandbox; default-src 'none'; base-uri 'none'",
            'Cross-Origin-Resource-Policy': 'same-origin', 'Referrer-Policy': 'no-referrer'})
    except (ValueError, TypeError, KeyError) as exc:
        return _response({"error": str(exc)[:500]}, 400)
    except OSError:
        return _response({"error": "附件读取失败"}, 400)
