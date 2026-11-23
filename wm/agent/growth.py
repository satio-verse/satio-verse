"""User-owned deterministic regression suites for Creator MCP revisions.

The model may run a fixed suite and adopt a passing candidate after policy approval,
but cannot write its own exam. Results describe only these MCP inputs/assertions.
Code executes in temporary package copies, not a security sandbox; the caller must
authorize the commands/tools and their external effects before evaluation.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .extensions import ID, TOOL_NAME, _atomic, _read_json, _relative, _within
from .mcp_runtime import MCPClient, MCPError

MAX_SUITE_BYTES = 64 * 1024
MAX_CASE_OUTPUT = 64 * 1024
MAX_REPORT_AGE_S = 7 * 24 * 3600
SCOPE = "仅评估此固定题集中的 MCP 工具行为；不代表通用智能或其他任务能力提升。"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _suite_body(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not ID.fullmatch(spec["id"]):
        raise ValueError("题集 id 必须为 1～48 个小写字母、数字或 -")
    if not isinstance(spec.get("package_id"), str) or not ID.fullmatch(spec["package_id"]):
        raise ValueError("题集需要有效的 package_id")
    name = str(spec.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("题集名称需要 1～80 个字符")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("每个题集需要 1～20 个固定测试用例")
    clean, total_timeout = [], 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("tool"), str) or not TOOL_NAME.fullmatch(case["tool"]):
            raise ValueError(f"第 {index + 1} 个用例工具名无效")
        arguments = case.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("用例 arguments 必须为 JSON 对象")
        expect = case.get("expect")
        if not isinstance(expect, dict) or not expect or set(expect) - {"contains", "not_contains", "equals"}:
            raise ValueError("每个用例必须包含 contains、not_contains 或 equals 断言，不执行自定义断言代码")
        assertions = {}
        for key, value in expect.items():
            if key == "equals":
                if len(_json(value)) > 8000:
                    raise ValueError("equals 预期结果过大")
                assertions[key] = value
                continue
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list) or not 1 <= len(values) <= 10 or any(not isinstance(v, str) or not v or len(v) > 1000 for v in values):
                raise ValueError("包含/不包含断言需要 1～10 个非空短字符串")
            assertions[key] = values
        timeout = case.get("timeout_s", 15)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError("单个用例 timeout_s 必须为 1～60 秒")
        total_timeout += timeout
        clean.append({"name": str(case.get("name") or f"用例 {index + 1}")[:100], "tool": case["tool"],
            "arguments": arguments, "expect": assertions, "timeout_s": float(timeout)})
    if total_timeout > 120:
        raise ValueError("每组用例的超时总额最多 120 秒")
    body = {"id": spec["id"], "name": name, "package_id": spec["package_id"], "cases": clean}
    if len(_json(body).encode("utf-8")) > MAX_SUITE_BYTES:
        raise ValueError("题集 JSON 过大，最多 64 KB")
    return body


def _assertions(result, expect):
    parts = result.get("content", [])
    if not isinstance(parts, list):
        return {"passed": False, "assertions": [], "error": "工具结果格式无效"}
    text = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    if not parts and "structuredContent" in result:
        text = _json(result["structuredContent"])
    if len(text.encode("utf-8")) > MAX_CASE_OUTPUT:
        return {"passed": False, "assertions": [], "error": "工具输出超过 64 KB，未对截断内容评分"}
    checks = []
    for key, wanted in expect.items():
        if key in {"contains", "not_contains"}:
            for word in wanted:
                checks.append({"kind": key, "expected": word, "passed": (word in text) if key == "contains" else (word not in text)})
        else:
            if isinstance(wanted, str):
                equal = text == wanted
            else:
                try:
                    actual = result["structuredContent"] if "structuredContent" in result else json.loads(text)
                    equal = _json(actual) == _json(wanted)  # True is not equivalent to numeric 1.
                except (ValueError, TypeError):
                    equal = False
            checks.append({"kind": "equals", "expected": wanted, "passed": equal})
    error = "MCP 工具返回 isError" if result.get("isError") else ""
    return {"passed": not error and bool(checks) and all(c["passed"] for c in checks), "assertions": checks,
        "error": error, "output_preview": text[:1000], "output_truncated": len(text) > 1000,
        "output_sha256": hashlib.sha256(text.encode()).hexdigest()}


class GrowthStore:
    def __init__(self, home: Path, registry):
        self.root = Path(home) / "growth"
        self.registry = registry
        self.lock = threading.RLock()
        self.running = asyncio.Lock()

    def save_suite(self, spec, by="user"):
        if by != "user":
            raise PermissionError("固定回归题集只能由用户创建或修改，模型不能修改验收标准")
        body = _suite_body(spec)
        digest = _hash(body)
        suite = {**body, "version": digest[:24], "hash": digest, "created": time.time(), "by": "user"}
        with self.lock:
            folder = self.root / "suites" / body["id"]
            revision = folder / "revisions" / (suite["version"] + ".json")
            if not _within(revision, self.root):
                raise ValueError("题集目录超出范围")
            if revision.exists():
                old = _read_json(revision)
                if not isinstance(old, dict) or old.get("hash") != digest or _hash(_suite_body(old)) != digest:
                    raise ValueError("题集历史版本已损坏，未覆盖")
                suite = old
            else:
                _atomic(revision, suite)
            _atomic(folder / "current.json", {"version": suite["version"], "hash": digest})
            return suite

    def _suite(self, suite_id):
        if not isinstance(suite_id, str) or not ID.fullmatch(suite_id):
            raise ValueError("题集编号无效")
        folder = self.root / "suites" / suite_id
        pointer = _read_json(folder / "current.json", {})
        version = pointer.get("version", "") if isinstance(pointer, dict) else ""
        if not isinstance(version, str) or len(version) != 24 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("题集不存在或版本指针损坏")
        path = folder / "revisions" / (version + ".json")
        if not _within(path, self.root):
            raise ValueError("题集目录超出范围")
        suite = _read_json(path)
        if not isinstance(suite, dict) or suite.get("by") != "user":
            raise ValueError("题集记录无效")
        digest = _hash(_suite_body(suite))
        if suite.get("hash") != digest or pointer.get("hash") != digest or digest[:24] != version:
            raise ValueError("固定题集内容与版本记录不符")
        return suite

    def suites(self):
        folder = self.root / "suites"
        result = []
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not path.is_dir() or not ID.fullmatch(path.name):
                continue
            try:
                result.append(self._suite(path.name))
            except (OSError, ValueError):
                result.append({"id": path.name, "error": "固定题集版本校验失败"})
        return result

    def _write_report(self, report):
        report["finished"] = time.time()
        report["report_hash"] = _hash({k: v for k, v in report.items() if k != "report_hash"})
        _atomic(self.root / "reports" / (report["id"] + ".json"), report)

    def _report(self, report_id):
        if not isinstance(report_id, str) or len(report_id) != 32 or any(c not in "0123456789abcdef" for c in report_id):
            raise ValueError("报告编号无效")
        path = self.root / "reports" / (report_id + ".json")
        if not _within(path, self.root):
            raise ValueError("报告目录超出范围")
        report = _read_json(path)
        if not isinstance(report, dict) or report.get("id") != report_id or report.get("report_hash") != _hash({k: v for k, v in report.items() if k != "report_hash"}):
            raise ValueError("报告不存在或内容校验失败")
        return report

    def reports(self):
        result = []
        folder = self.root / "reports"
        paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100] if folder.is_dir() else []
        for path in paths:
            try:
                report = self._report(path.stem)
                report["adoption"] = self.validate(path.stem)
                result.append(report)
            except (OSError, ValueError, KeyError):
                result.append({"id": path.stem, "error": "评测报告校验失败"})
        return result

    def _copy(self, config, parent):
        folder = Path(tempfile.mkdtemp(prefix="case-", dir=parent))
        if not _within(folder, parent):
            raise ValueError("测试副本目录超出范围")
        origin = Path(config["cwd"])
        for name, wanted in config["files"].items():
            relative = _relative(name)
            source = origin / relative
            if not _within(source, origin):
                raise ValueError("候选文件路径超出范围")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != wanted:
                raise ValueError("评测开始前扩展文件发生改变")
            destination = folder / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        (folder / "manifest.json").write_text(_json(config["manifest"]), encoding="utf-8")
        return folder

    async def _case(self, config, case, parent):
        folder = self._copy(config, parent)
        running = {**config, "cwd": str(folder)}
        async with MCPClient(running, timeout=case["timeout_s"]) as client:
            tools = self.registry._validate_tools(await client.list_tools(), running)
            if case["tool"] not in {t["name"] for t in tools}:
                return {"passed": False, "assertions": [], "error": "版本未提供指定工具"}
            response = await client.request("tools/call", {"name": case["tool"], "arguments": case["arguments"]})
            return _assertions(response, case["expect"])

    async def _phase(self, config, cases, phase, parent):
        for index, case in enumerate(cases):
            started = time.monotonic()
            try:
                outcome = await asyncio.wait_for(self._case(config, case, parent), case["timeout_s"])
            except asyncio.TimeoutError:
                outcome = {"passed": False, "assertions": [], "error": "用例超过固定时间限制"}
            except (MCPError, OSError, ValueError, TypeError, KeyError):
                outcome = {"passed": False, "assertions": [], "error": "工具连接、执行或版本校验失败"}
            phase["cases"].append({"index": index, "name": case["name"], "tool": case["tool"],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2), **outcome})
            phase["passed"] = sum(c["passed"] for c in phase["cases"])
            phase["failed"] = len(phase["cases"]) - phase["passed"]
        phase["elapsed_ms"] = round(sum(c["elapsed_ms"] for c in phase["cases"]), 2)

    async def evaluate(self, package_id, revision, suite_id, stop=None, authorized=False):
        if authorized is not True:
            raise PermissionError("运行候选与基线代码需要当前任务明确授权")
        suite = self._suite(suite_id)
        if suite["package_id"] != package_id:
            raise ValueError("题集不属于这个扩展")
        candidate = self.registry.candidate_server(package_id, revision)
        baseline_id = candidate["active_revision"] if candidate["enabled"] else None
        baseline = self.registry.candidate_server(package_id, baseline_id) if baseline_id else None
        report = {"id": uuid.uuid4().hex, "package_id": package_id, "suite_id": suite_id, "suite_hash": suite["hash"],
            "suite_version": suite["version"], "created": time.time(), "scope": SCOPE, "status": "running", "case_count": len(suite["cases"]),
            "baseline_activation": {"enabled": candidate["enabled"], "active_revision": candidate["active_revision"]},
            "baseline": {"revision": baseline_id, "cases": [], "passed": 0, "failed": 0, "elapsed_ms": 0} if baseline else None,
            "candidate": {"revision": candidate["revision"], "cases": [], "passed": 0, "failed": 0, "elapsed_ms": 0}}
        self.root.mkdir(parents=True, exist_ok=True)
        runs = self.root / "runs"
        runs.mkdir(exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix="evaluation-", dir=runs))
        async def run():
            if baseline:
                await self._phase(baseline, suite["cases"], report["baseline"], parent)
            await self._phase(candidate, suite["cases"], report["candidate"], parent)
        try:
            async with self.running:
                await self.registry._cancellable(run(), stop)
            passed = report["candidate"]["passed"] == len(suite["cases"])
            report["status"] = "passed" if passed else "failed"
            report["comparison"] = ("candidate_passed_no_baseline" if passed else "candidate_failed_no_baseline") if baseline is None else (
                "improved_on_suite" if passed and report["candidate"]["passed"] > report["baseline"]["passed"] else "regression_passed" if passed else "regression_failed")
            self._write_report(report)
            return report
        except asyncio.CancelledError:
            report["status"] = "cancelled"
            report["comparison"] = "incomplete"
            self._write_report(report)
            raise
        finally:
            # Only owned temporary copies, with their resolved absolute boundary checked.
            if parent.exists() and parent.resolve() != runs.resolve() and _within(parent, runs):
                shutil.rmtree(parent, ignore_errors=True)

    def validate(self, report_id):
        report = self._report(report_id)
        reasons = []
        if report.get("status") != "passed":
            reasons.append("候选没有完成全部用例并通过")
        finished = report.get("finished", 0)
        if not isinstance(finished, (int, float)) or finished > time.time() + 60 or time.time() - finished > MAX_REPORT_AGE_S:
            reasons.append("报告已过期，需要重新评测")
        try:
            suite = self._suite(report["suite_id"])
            if suite["hash"] != report["suite_hash"] or suite["package_id"] != report["package_id"]:
                reasons.append("固定题集已经改变，需要重新评测")
            cases = report["candidate"]["cases"]
            if len(cases) != len(suite["cases"]) or not all(c.get("passed") is True for c in cases):
                reasons.append("候选用例结果不完整或有失败")
            current = self.registry.candidate_server(report["package_id"], report["candidate"]["revision"])
            activation = {"enabled": current["enabled"], "active_revision": current["active_revision"]}
            if activation != report["baseline_activation"]:
                reasons.append("当前启用版本已经改变，需要重新比较")
            if report.get("baseline"):
                self.registry.candidate_server(report["package_id"], report["baseline"]["revision"])
                baseline_cases = report["baseline"]["cases"]
                if len(baseline_cases) != len(cases) or any(old.get("passed") and not new.get("passed") for old, new in zip(baseline_cases, cases)):
                    reasons.append("候选存在退化或基线结果不完整")
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append("题集或版本完整性校验失败")
        return {"eligible": not reasons, "reasons": reasons, "comparison": report.get("comparison"), "scope": SCOPE}

    def adopt(self, report_id, authorized=False):
        if authorized is not True:
            raise PermissionError("采用评测版本需要当前任务明确授权")
        with self.lock, self.registry.lock:
            result = self.validate(report_id)
            if not result["eligible"]:
                raise ValueError("不能采用：" + "；".join(result["reasons"]))
            report = self._report(report_id)
            enabled = self.registry.set_enabled(report["package_id"], True, authorized=True,
                revision=report["candidate"]["revision"], by="growth:" + report_id)
            _atomic(self.root / "adoptions" / (uuid.uuid4().hex + ".json"), {"at": time.time(), "report_id": report_id,
                "package_id": report["package_id"], "revision": report["candidate"]["revision"], "suite_hash": report["suite_hash"]})
            return {"extension": enabled, "report_id": report_id, "comparison": report["comparison"], "scope": SCOPE}
