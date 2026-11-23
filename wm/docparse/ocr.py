"""第二档: 本机识字. 父进程这一边: 起识字子进程 (worker.py)、限内存限时间、收结果. 不在 8790 里 import onnxruntime / opencv.

子进程的限制和 material_readers 解析进程是同一套: Windows 的作业对象限内存 (复用它的 _windows_job, 不复制) + 超时杀. 结果大于
4 MB 或者进程异常退出都当失败. 失败不重试: 这些页进"没读", 原因写清楚.
"""
from __future__ import annotations

import contextlib
import inspect
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import BackendInfo, component

MAX_OUTPUT = 4 * 1024 * 1024
OCR_MEMORY_BYTES = 1536 * 1024 * 1024   # 2026-11-15 真 RapidOCR 量过: 提交内存峰值 804–880 MB (工作集只有 205–297 MB), 比解析进程的 768 MB 多
BASE_TIMEOUT_S = 30.0
PER_PAGE_S = 25.0
TIMEOUT_CAP_S = 300.0
WORKER = Path(__file__).with_name("worker.py")
# 同一时刻只许跑一个识字进程 (评审 11-15): 一个进程提交内存峰值约 880 MB, 工作台会把连着的只读调用一起跑、几个会话也可能同时在用,
# 平时机器只剩 2–4 GB 可用, 还要留给语音合成和本机模型. 线程锁 (工具层用 asyncio.to_thread 调); 排队等的不算在超时里.
_ONE_AT_A_TIME = threading.Semaphore(1)


def info(ocr_dir: Path | str | None = None) -> BackendInfo:
    st = component.state(ocr_dir)
    return BackendInfo(id="rapidocr-local", name="本机识字（RapidOCR）", kind="local-ocr", upload=False, auto=True,
                       available=st["installed"], reason_unavailable=st["reason"],
                       detail="文件不出电脑；只认字，表格、公式、双栏顺序不保证。")


def default_cmd() -> list[str]:
    # -I: 隔离模式, 不把脚本所在目录 (里面有 ocr.py、cloud.py 这些名字) 放进 sys.path, 也不认 PYTHON* 环境变量
    return [sys.executable, "-I", str(WORKER)]


def timeout_for(pages: int) -> float:
    return min(TIMEOUT_CAP_S, BASE_TIMEOUT_S + PER_PAGE_S * max(1, pages))


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONNOUSERSITE="1")   # CUDA_PATH 这类不带 (会弄坏 onnxruntime)
    return env


def run_ocr(job: dict, *, cmd: list[str] | None = None, timeout_s: float | None = None) -> dict:
    """跑一次识字 (同一时刻只有一个识字进程, 别的在这里排队). 返回 worker 的结果; 起不来、超时、超限、输出不对都返回
    {"ok": False, "code": ..., "error": ...}."""
    with _ONE_AT_A_TIME:
        return _run_ocr(job, cmd, timeout_s)


def _run_ocr(job: dict, cmd: list[str] | None, timeout_s: float | None) -> dict:
    pages = len(job.get("pages") or [1])
    timeout = float(timeout_s if timeout_s is not None else timeout_for(pages))
    process, close_job = None, None
    try:
        process = subprocess.Popen(cmd or default_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            from ..agent.material_readers import _windows_job
            if "memory" in inspect.signature(_windows_job).parameters:
                close_job = _windows_job(process, memory=OCR_MEMORY_BYTES)
            else:           # material_readers 还没加 memory 参数: 只能用它固定的 768 MB (真识字器会超, 页会进"没读")
                close_job = _windows_job(process)
        stdout, _ = process.communicate(json.dumps(job, ensure_ascii=True).encode("ascii"), timeout=timeout)
        if process.returncode or len(stdout) > MAX_OUTPUT:
            return {"ok": False, "code": "failed", "error": "识字进程超出资源限制或异常退出"}
        try:
            got = json.loads(stdout.decode("utf-8"))
        except ValueError:
            got = None
        if not isinstance(got, dict) or "ok" not in got:
            return {"ok": False, "code": "failed", "error": "识字进程没有给出有效结果"}
        return got
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout", "error": f"识字超过 {timeout:g} 秒限制"}
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "code": "failed", "error": f"识字进程没能跑起来（{type(exc).__name__}）"}
    except Exception as exc:  # noqa: BLE001  (_windows_job 在建不起资源限制时抛 MaterialError)
        return {"ok": False, "code": "failed", "error": str(exc)[:200] or f"识字进程出错（{type(exc).__name__}）"}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=2)
        if close_job:
            close_job()
