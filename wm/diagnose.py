"""诊断信息和环境自检 (2026-10-27 用户: "copy-diagnostics + environment self-check button"; 照 dsh-bili-scroll 2.0 的
"复制诊断信息 / 环境自检"): 出问题时在设置的"诊断"页点一下.
- 诊断信息 (report / as_text): 版本、系统、显卡、磁盘、配置摘要、本地服务、可选组件、外部渠道、额度、最近的报错,
  一份能直接贴给别人看的文字.
- 环境自检 (selfcheck): 一项一项查哪一步不通 (ok / warn / fail / skip), 每项写怎么修.
去隐私 (scrub): 不带任何 key、token、cookie, 不带余额和花了多少钱; 6~12 位的长数字 (QQ 号、手机号这类) 打码; 用户目录换成 ~.
接口 GET /api/diagnose、GET /api/selfcheck (wm/app.py, 只认本机页面).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT, resolve_path
from .ledger import BJ

LOGS = (ROOT / "logs" / "serve", ROOT / "logs" / "services")  # tools/serve.py 和本地服务看守 (wm/services.py) 写的日志
_ERR = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)")
_SECRETS = [
    (re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|token|secret|password|passwd|cookie|sessdata|bili_jct|authorization)"
                r"[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;}]+"), r"\1***"),
]
_LONG_NUM = re.compile(r"(?<![\w.:/-])\d{6,12}(?![\w.:-])")
_NEEDED = ("aiohttp", "numpy", "scipy", "psutil", "sherpa_onnx", "onnxruntime")


def scrub(text: str) -> str:
    """去隐私: key / token / cookie 换成 ***, 长数字 (QQ 号这类) 留头尾两位, 用户目录换成 ~."""
    text = str(text)
    home = str(Path.home())
    for h in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        text = text.replace(h, "~")
    for pat, rep in _SECRETS:
        text = pat.sub(rep, text)
    return _LONG_NUM.sub(lambda m: m.group()[:2] + "*" * (len(m.group()) - 4) + m.group()[-2:], text)


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def app_version() -> str:
    try:
        return json.loads((ROOT / "config" / "components.json").read_text(encoding="utf-8")).get("app_version", "?")
    except (OSError, ValueError):
        return "?"


def system() -> dict:
    """系统、Python、内存、磁盘、时区 (都是快的, 不联网)."""
    out = {"os": platform.platform(), "python": platform.python_version(),
           "python_kind": "自带的便携 Python" if (ROOT / "runtime" / "python") in Path(sys.executable).parents else "系统里的 Python",
           "cpus": os.cpu_count()}
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["ram"] = {"total": vm.total, "free": vm.available}
        out["uptime_s"] = round(time.time() - psutil.Process().create_time())
    except Exception:  # noqa: BLE001 - 没装 psutil 也照样出报告
        pass
    disks = {}
    for name, path in (("项目所在盘", ROOT), ("用户目录所在盘", Path.home())):
        try:
            u = shutil.disk_usage(path)
            disks[name] = {"drive": Path(path).anchor, "free": u.free, "total": u.total}
        except OSError:
            pass
    out["disks"] = disks
    now = datetime.now().astimezone()
    out["time"] = {"local": now.strftime("%Y-%m-%d %H:%M:%S"), "utc_offset": now.strftime("%z"),
                   "beijing": datetime.now(BJ).strftime("%Y-%m-%d %H:%M")}
    return out


def gpu() -> list[dict]:
    """N 卡 (nvidia-smi): 名字、驱动、显存、计算能力. 没有 N 卡或查不到是空表."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.used,memory.total,compute_cap",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 5:
            try:
                out.append({"name": p[0], "driver": p[1], "used_mb": int(float(p[2])), "total_mb": int(float(p[3])), "cap": p[4]})
            except ValueError:
                pass
    return out


def config_summary(cfg: dict) -> dict:
    """每样用的哪个引擎 (只写引擎名和模型文件名, 不写路径以外的东西, 不写 key)."""
    g = lambda *ks: _dig(cfg, *ks)  # noqa: E731
    llm = {"engine": g("llm", "engine"), "model": g("llm", "model_label")}
    if model := g("llm", "server", "model"):
        llm["file"] = Path(str(model)).name
    tts = {"engine": g("tts", "engine")}
    if tts["engine"] == "http":
        tts["preset"] = g("tts", "http", "preset")
    return {"llm": llm, "asr": g("asr", "engine"), "tts": tts, "face": g("face", "engine"), "motion": g("motion", "engine"),
            "avatar": g("avatar", "default"), "memory_vault": "设了" if g("memory", "vault") else "没设",
            "embed": g("memory", "embed", "engine"), "qq": bool(g("channels", "qq", "enabled")),
            "weixin": bool(g("channels", "weixin", "enabled")), "services": g("services")}


def _dig(d, *ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def recent_errors(limit: int = 15, logs: tuple[Path, ...] = LOGS) -> list[str]:
    """今天和昨天日志里最近的报错行 (ERROR / CRITICAL / Traceback), 每个文件只看最后 256 KB."""
    days = {datetime.now().strftime("%Y%m%d"), (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")}
    hits: list[tuple[float, str, str]] = []
    for d in logs:
        for f in (d.glob("*.log") if d.is_dir() else []):
            if not any(day in f.name for day in days):
                continue
            try:
                with open(f, "rb") as fp:
                    fp.seek(max(0, f.stat().st_size - (256 << 10)))
                    lines = fp.read().decode("utf-8", "replace").splitlines()
                mtime = f.stat().st_mtime
            except OSError:
                continue
            hits += [(mtime, f.name, ln.strip()[:300]) for ln in lines if _ERR.search(ln)]
    hits.sort(key=lambda h: h[0])
    return [scrub(f"[{name}] {line}") for _, name, line in hits[-limit:]]


async def report(cfg: dict, ledger, components, channels, services_view) -> dict:
    """整份诊断信息 (给人看、给人贴). services_view: 协程, 回本地服务的状态 (和 /api/services 一样)."""
    sysinfo, gpus, errors = await asyncio.gather(asyncio.to_thread(system), asyncio.to_thread(gpu),
                                                 asyncio.to_thread(recent_errors))
    try:
        svc = await services_view()
    except Exception as exc:  # noqa: BLE001
        svc = {"error": f"{type(exc).__name__}: {exc}"}
    comp = []
    if components is not None:
        comp = [{"id": c["id"], "installed": bool(c["installed"]), "present": c["present"]}
                for c in components.status().get("components", [])]
    ch = channels.public_status() if channels is not None else {}
    budget = ledger.budget_state() if ledger is not None else None
    bal = ledger.balance_view() if ledger is not None else {}
    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": app_version(), "system": sysinfo, "gpu": gpus, "config": config_summary(cfg),
        "services": [{k: s.get(k) for k in ("name", "port", "state", "restarts", "error") if k in s}
                     for s in svc.get("services", [])] if isinstance(svc, dict) else [],
        "supervisor": {"running": svc.get("running"), "error": svc.get("error")} if isinstance(svc, dict) else {},
        "components": comp,
        "channels": {"state": ch.get("state"), "error": ch.get("error"), "convs": ch.get("convs")},
        "budget": [{"id": r["id"], "level": r["level"], "ratio": r["ratio"]} for r in budget["rows"]] if budget else [],
        "balance": "查得到" if bal.get("total") is not None else (bal.get("error") or "没查"),  # 不带余额数
        "errors": errors,
    }


def as_text(r: dict, page: str = "") -> str:
    """诊断信息的文字版 (复制用). page: 页面那边补的 (WebView2 版本等)."""
    s, g = r["system"], r["config"]
    lines = [f"WorldModel 诊断信息 {r['at']}", f"版本 {r['version']} · {s['os']} · Python {s['python']} ({s['python_kind']})"]
    if page:
        lines.append(f"页面: {page}")
    if "ram" in s:
        lines.append(f"内存 {_gb(s['ram']['total'])} (可用 {_gb(s['ram']['free'])}) · CPU {s['cpus']} 线程"
                     + (f" · 服务端开了 {s['uptime_s'] // 60} 分钟" if "uptime_s" in s else ""))
    for name, d in s["disks"].items():
        lines.append(f"{name} {d['drive']} 剩 {_gb(d['free'])} / {_gb(d['total'])}")
    t = s["time"]
    lines.append(f"时间 {t['local']} (UTC{t['utc_offset'][:3]}:{t['utc_offset'][3:]}), 北京时间 {t['beijing']}")
    lines.append("显卡: " + ("; ".join(f"{x['name']} (驱动 {x['driver']}, 显存 {x['used_mb']}/{x['total_mb']} MB, 计算能力 {x['cap']})"
                                       for x in r["gpu"]) or "没有 N 卡 (或查不到)"))
    llm = g["llm"]
    lines += ["", "== 配置 ==",
              f"本机模型 {llm['engine']}" + (f" · {llm.get('file') or llm.get('model')}" if llm.get("file") or llm.get("model") else ""),
              f"语音识别 {g['asr']} · 语音合成 {g['tts']['engine']}" + (f" ({g['tts']['preset']})" if g["tts"].get("preset") else "")
              + f" · 面部 {g['face']} · 动作 {g['motion']} · 形象 {g['avatar']}",
              f"记忆库 {g['memory_vault']} · 语义检索 {g['embed']} · QQ {'开' if g['qq'] else '关'} · 微信 {'开' if g['weixin'] else '关'}"]
    lines += ["", "== 本地服务 ==" + ("" if r["supervisor"].get("running") else " (看守没在跑)")]
    lines += [f"{x.get('name')}: {x.get('state')}" + (f" (端口 {x['port']})" if x.get("port") else "")
              + (f", 重启过 {x['restarts']} 次" if x.get("restarts") else "") + (f", {x['error']}" if x.get("error") else "")
              for x in r["services"]] or ["(没有)"]
    lines += ["", "== 可选组件 ==", ", ".join(f"{c['id']} {'装了' if c['installed'] else '有' if c['present'] else '没装'}"
                                           for c in r["components"]) or "(没有清单)"]
    c = r["channels"]
    lines += ["", "== 外部渠道 ==", f"状态 {c.get('state')}" + (f", {c['error']}" if c.get("error") else "")
              + (f", {c['convs']} 个会话" if c.get("convs") else "")]
    tight = [f"{b['id']} {'用完了' if b['level'] == 'stop' else '快到了'}" for b in r["budget"] if b["level"] in ("warn", "stop")]
    limited = [b["id"] for b in r["budget"] if b["level"] != "off"]
    lines += ["", "== 额度 ==", ("设了上限: " + ", ".join(limited) if limited else "没设上限")
              + ("; " + ", ".join(tight) if tight else "") + f" · DeepSeek 余额 {r['balance']}"]
    lines += ["", f"== 最近的报错 ({len(r['errors'])} 条) =="] + (r["errors"] or ["(今天和昨天的日志里没有)"])
    return scrub("\n".join(lines))


# ---- 环境自检 ----
def _check(cid: str, name: str, level: str, detail: str = "", fix: str = "") -> dict:
    return {"id": cid, "name": name, "level": level, "detail": scrub(detail), "fix": scrub(fix)}


def _files(cfg_part: dict | None) -> list[str]:
    """一个引擎配置里写的文件 (models/、runtime/ 开头的, 逗号隔开的也拆开)."""
    out = []
    for v in (cfg_part or {}).values():
        if isinstance(v, str):
            out += [p.strip() for p in v.split(",") if p.strip().startswith(("models/", "runtime/", "third_party/"))]
    return out


def _model_files(cfg: dict) -> list[dict]:
    checks = []
    for sect, label in (("asr", "语音识别"), ("tts", "语音合成"), ("face", "面部动画")):
        engine = _dig(cfg, sect, "engine")
        part = _dig(cfg, sect, engine) if isinstance(engine, str) else None
        if engine in (None, "none", "mock") or not isinstance(part, dict):
            checks.append(_check(f"{sect}_files", f"{label}的模型文件", "skip", f"用的是 {engine or '没设'}, 不用模型文件"))
            continue
        files = _files(part) + ([part["model_dir"]] if isinstance(part.get("model_dir"), str) else [])
        missing = [f for f in dict.fromkeys(files) if not resolve_path(f).exists()]
        checks.append(_check(f"{sect}_files", f"{label}的模型文件", "fail" if missing else "ok",
                             f"{engine}: 缺 {', '.join(missing[:3])}" + (f" 等 {len(missing)} 个" if len(missing) > 3 else "")
                             if missing else f"{engine}: {len(files)} 个文件都在",
                             "设置的版本与组件里装上对应的组件, 或者重装本体" if missing else ""))
    return checks


async def selfcheck(cfg: dict, ledger, channels, services_view, root: Path = ROOT) -> list[dict]:
    """一项一项查. 每项 {id, name, level: ok / warn / fail / skip, detail, fix}; 一项出错不影响别的."""
    out: list[dict] = [_check("server", "服务端", "ok", f"WorldModel {app_version()} 在跑, 端口 {_dig(cfg, 'server', 'port')}")]

    missing = [m for m in _NEEDED if importlib.util.find_spec(m) is None]
    out.append(_check("python", "Python 和依赖", "fail" if missing else "ok",
                      f"Python {platform.python_version()}" + (f", 缺 {', '.join(missing)}" if missing else ", 需要的包都在"),
                      "重装本体 (安装包自带的 Python 里都有); 开发环境跑 python tools/setup_python.py" if missing else ""))

    try:
        free = shutil.disk_usage(root).free
        lvl = "fail" if free < 1e9 else "warn" if free < 5e9 else "ok"
        out.append(_check("disk", "磁盘空间", lvl, f"{Path(root).anchor} 剩 {_gb(free)}",
                          "清一清这个盘; 可选组件 (GPT-SoVITS 要约 30 GB) 装不下" if lvl != "ok" else ""))
    except OSError as exc:
        out.append(_check("disk", "磁盘空间", "warn", f"查不了: {exc}"))

    gpus = await asyncio.to_thread(gpu)
    if not gpus:
        out.append(_check("gpu", "显卡", "skip", "没有 N 卡 (或 nvidia-smi 查不到): 本机模型、语音用 CPU, 会慢一些"))
    else:
        x = gpus[0]
        full = x["total_mb"] and x["used_mb"] / x["total_mb"] > 0.92
        out.append(_check("gpu", "显卡", "warn" if full else "ok",
                          f"{x['name']}, 显存 {x['used_mb']}/{x['total_mb']} MB, 驱动 {x['driver']}",
                          "显存快满了: 关掉别的占显卡的程序, 或者在本地服务里少开几样" if full else ""))

    try:
        svc = await services_view()
        bad = [s for s in svc.get("services", []) if s.get("state") in ("failed", "backoff", "error")]
        off = [s for s in svc.get("services", []) if s.get("state") in ("off", "stopped")]
        if bad:
            out.append(_check("services", "本地服务", "fail",
                              "; ".join(f"{s['name']} {s['state']}" + (f": {s['error']}" if s.get("error") else "") for s in bad),
                              "看本地服务页的日志, 点重启; 起不来就把日志 (复制诊断信息) 发出来"))
        elif off:
            out.append(_check("services", "本地服务", "warn", "没开: " + ", ".join(s["name"] for s in off),
                              "本地服务页点启动 (桌宠带 --services 启动会自动拉起)"))
        else:
            out.append(_check("services", "本地服务", "ok", ", ".join(f"{s['name']} {s['state']}" for s in svc.get("services", []))
                              or "这套配置不用另开服务"))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("services", "本地服务", "warn", f"查不了: {type(exc).__name__}: {exc}"))

    out += _model_files(cfg)

    if ledger is not None:
        has_key = any(getattr(m, "has_key", False) and not getattr(m, "local", False) for m in ledger.live_models())
        out.append(_check("cloud", "云端模型", "ok" if has_key else "skip",
                          "配了云端模型 (不显示 key)" if has_key else "没配云端模型: 只用本机模型 (人设预设里能选云端模型)"))
        # 本机大模型 (cloud.py 的 selfhost: 桌宠 / QQ 选了的, 或配置 llm.local_api 写了的): 服务开着没有、上下文够不够
        from . import cloud as C
        locals_ = [m for m in ledger.live_models() if getattr(m, "local", False)]
        spec = C.local_spec()
        if spec is not None and not any(m.base_url == spec["base_url"] for m in locals_):
            locals_.append(C.CloudLLM(spec, {}))
        for m in locals_[:3]:
            info = await m.probe()
            notes = C.probe_notes(info, m.base_url)
            what = f"{C.redact(m.base_url)} · {info.get('server') or '?'} · {m.model}" + (f" · 上下文 {info['ctx']}" if info.get("ctx") else "")
            out.append(_check("local_llm", "本机大模型", "fail" if not info.get("ok") else "warn" if notes else "ok",
                              what + ("; " + "; ".join(notes) if notes else ""),
                              "先把本机模型的服务开起来 (Ollama / LM Studio, 或 python tools/start_llm.py --which local)"
                              if not info.get("ok") else ""))
        bal = ledger.balance_view()
        if bal.get("total") is not None:
            out.append(_check("balance", "DeepSeek 余额", "warn" if bal.get("low") else "ok",
                              "余额不多了" if bal.get("low") else "查得到", "到 DeepSeek 开放平台充值" if bal.get("low") else ""))
        elif bal.get("error"):
            out.append(_check("balance", "DeepSeek 余额", "warn", bal["error"],
                              "key 对不对、网通不通; 用别家的模型可以不管这一项"))
        st = ledger.budget_state()
        tight = [r for r in st["rows"] if r["level"] in ("warn", "stop")]

        def say(r: dict) -> str:
            return f"{r['name']}用完了" if r["level"] == "stop" else f"{r['name']}用了 {r['ratio']:.0%}"

        if not any(r["level"] != "off" for r in st["rows"]):
            out.append(_check("budget", "按渠道的额度", "skip", "没设上限"))
        else:
            out.append(_check("budget", "按渠道的额度", "warn" if tight else "ok",
                              "; ".join(say(r) for r in tight) if tight else f"都没到 {st['warn']:.0%}",
                              "账号与余额页调高上限, 或者等零点重新算" if tight else ""))

    if channels is not None and _dig(cfg, "channels", "qq", "enabled"):
        ch = channels.public_status()
        ok = ch.get("state") in ("connected", "online", "running")
        out.append(_check("qq", "QQ (SnowLuma)", "ok" if ok else "fail", f"状态 {ch.get('state')}" + (f": {ch['error']}" if ch.get("error") else ""),
                          "" if ok else "SnowLuma 开着吗、扫码登录了吗、外部渠道页的地址和 token 对不对"))
    else:
        out.append(_check("qq", "QQ (SnowLuma)", "skip", "没开 QQ 渠道"))

    now = datetime.now().astimezone()
    off = now.utcoffset() or timedelta(0)
    out.append(_check("time", "时间和时区", "ok", f"这台电脑是 UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}"
                      + ("" if off == timedelta(hours=8) else "; 峰谷、节假日按北京时间算, 不受影响; 额度按这台电脑的零点重新算")))

    log_dir = resolve_path(_dig(cfg, "trace", "log_dir") or "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / f".write-test-{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        out.append(_check("logs", "日志目录能写", "ok", str(log_dir)))
    except OSError as exc:
        out.append(_check("logs", "日志目录能写", "fail", f"{log_dir}: {exc}", "换个有写权限的目录 (配置 trace.log_dir), 或者别装在只读的地方"))

    seven = root / "runtime" / "7zip" / "7zr.exe"
    out.append(_check("7zip", "解压工具 7zr.exe", "ok" if seven.is_file() else "warn",
                      "在" if seven.is_file() else "没有: 装 GPT-SoVITS 这类 7z 组件时会失败", "" if seven.is_file() else "重装本体"))

    errs = await asyncio.to_thread(recent_errors, 3)
    out.append(_check("errors", "最近的报错", "warn" if errs else "ok",
                      errs[-1] if errs else "今天和昨天的日志里没有", "复制诊断信息发给别人看 (里面有最近 15 条)" if errs else ""))
    return out
