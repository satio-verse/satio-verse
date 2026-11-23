"""她的经历存在哪: 小窝认知的事件流 (杯子在哪、找到过哪里) 和端东西的经验. 放在用户数据区, 不在安装目录
(卸载会删整个 {app}\\logs, installer/worldmodel.iss).

- 默认 ~/.worldmodel/experience/ (配置 room.cognition.data_dir 可以改; 相对路径相对仓库根目录).
- 没设 data_dir、又改了 trace.log_dir 的 (测试、自检都把日志指到自己的目录): 跟着 log_dir 走 (<log_dir>/cognition/),
  不写进用户真的经历.
- 旧位置 (<log_dir>/cognition/, 2026-10-27 以前都写在这里) 有、新位置没有: 服务端启动时搬过去, 旧文件改名留一份 .migrated;
  两边都有就不动, 记一条警告.
- 命令行:
    python -m wm.cognition.store where           # 现在用的是哪个文件夹
    python -m wm.cognition.store status          # 有哪些文件、多少条
    python -m wm.cognition.store export 文件.zip  # 打包导出
    python -m wm.cognition.store delete --yes    # 删掉 (服务端开着的时候先停, 不然它下次写又会建起来)
    python -m wm.cognition.store migrate         # 手动搬旧位置的
  都可以带 --config / --set (和 python -m wm 一样).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

FILES = ("room-events.jsonl", "carry-events.jsonl", "afford-events.jsonl")
DEFAULT_DIR = "~/.worldmodel/experience"
GEN_FILE = "generation.json"  # 删除代次: 删过、重置过几次 (页面按它清本机存的; 代次 > 0 以后不再从旧位置搬)


HISTORY = 20  # 记最近几次删除、重置的范围 (页面隔了好几代才连上时, 照每一次的范围清)


def _gen_file(folder: Path) -> dict:
    try:
        d = json.loads((folder / GEN_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("gen"), int) else {"gen": 0}
    except (OSError, ValueError):
        return {"gen": 0}


def last_reset(folder: Path) -> dict:
    """最近一次删除、重置: {gen, at, why, scope}; 没有过是 {gen: 0}."""
    return {k: v for k, v in _gen_file(folder).items() if k != "history"}


def reset_history(folder: Path) -> list[dict]:
    """最近几次删除、重置 [{gen, at, why, scope}], 旧的在前."""
    h = _gen_file(folder).get("history")
    return h if isinstance(h, list) else []


def generation(folder: Path) -> int:
    return int(last_reset(folder).get("gen") or 0)


def bump(folder: Path, why: str, scope: list[str]) -> int:
    """代次加一 (删除、重置以后): 页面连上时见到更大的代次, 按 scope 清掉本机存的 (experience: 她认识的东西;
    page: 再加上练习参数); 旧位置的不再搬进来."""
    gen = generation(folder) + 1
    folder.mkdir(parents=True, exist_ok=True)
    now = {"gen": gen, "at": datetime.now().isoformat(timespec="seconds"), "why": why, "scope": list(scope)}
    history = (reset_history(folder) + [now])[-HISTORY:]
    (folder / GEN_FILE).write_text(json.dumps({**now, "history": history}, ensure_ascii=False), encoding="utf-8")
    return gen


def delete_legacy(cfg: dict) -> list[str]:
    """删掉旧位置 (<log_dir>/cognition/) 的那几个和搬走时留的 .migrated 副本: 重置以后旧的不能再被搬回来、也不该还留着."""
    new, old = data_dir(cfg), legacy_dir(cfg)
    if new.resolve() == old.resolve():
        return []
    gone = []
    for name in FILES:
        for f in (old / name, old / (name + ".migrated")):
            if f.exists():
                f.unlink()
                gone.append(f.name)
    return gone


def _paths(cfg: dict) -> tuple[str, str]:
    rc = (cfg.get("room") or {}).get("cognition") or {}
    return str(rc.get("data_dir") or ""), str((cfg.get("trace") or {}).get("log_dir") or "logs")


def _resolve(value: str) -> Path:
    from ..config import resolve_path
    return resolve_path(str(Path(value).expanduser()))


def legacy_dir(cfg: dict) -> Path:
    """2026-10-27 以前写的地方."""
    return _resolve(_paths(cfg)[1]) / "cognition"


def user_dir(cfg: dict, sub: str, configured: str = "") -> Path:
    """她攒下来的东西放哪 (同一套规矩, 动作成长 motion-growth 也用): 设了就用设的; 测试、自检改了 trace.log_dir 的跟着
    <log_dir>/<sub>; 不然 ~/.worldmodel/<sub> (用户数据区, 卸载不删)."""
    log_dir = str((cfg.get("trace") or {}).get("log_dir") or "logs")
    if configured:
        return _resolve(configured)
    if log_dir != "logs":
        return _resolve(log_dir) / sub
    return Path("~/.worldmodel").expanduser() / sub


def data_dir(cfg: dict) -> Path:
    """小窝认知的: 测试、自检时是 <log_dir>/cognition (以前就写在那里)."""
    data, log_dir = _paths(cfg)
    if not data and log_dir != "logs":
        return legacy_dir(cfg)
    return user_dir(cfg, Path(DEFAULT_DIR).name, data)


def scene_dir(cfg: dict, scene=None) -> Path:
    """Legacy files stay put; a known new scene gets its own event namespace."""
    from .layout import DEFAULT_SCENE_ID, scene_id
    selected = scene_id(scene)
    base = data_dir(cfg)
    return base if selected == DEFAULT_SCENE_ID else base / "scenes" / selected


def _event_files(folder: Path):
    """Only owned filenames and registered namespaces; never follow links outside."""
    from .layout import SCENE_PROFILES
    base = folder.resolve()
    for prefix in [Path(), *(Path("scenes") / scene for scene in sorted(SCENE_PROFILES))]:
        for name in FILES:
            rel = prefix / name
            path = folder / rel
            if path.exists():
                if not path.is_file() or not path.resolve().is_relative_to(base):
                    raise OSError("经验路径超出所选目录或不是文件")
                yield rel.as_posix(), path


def migrate(cfg: dict) -> list[str]:
    """旧位置有、新位置没有的搬过去 (复制完再把旧的改名 .migrated). 返回搬了哪几个."""
    new, old = data_dir(cfg), legacy_dir(cfg)
    if new.resolve() == old.resolve() or generation(new) > 0:  # 删过、重置过: 旧位置的不再搬 (不然会把删掉的带回来)
        return []
    moved = []
    for name in FILES:
        src, dst = old / name, new / name
        if not src.exists():
            continue
        if dst.exists():
            log.warning("她的经历在旧位置 %s 和新位置 %s 都有, 没搬; 要合并的话先导出看看", src, dst)
            continue
        new.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        src.rename(src.with_name(src.name + ".migrated"))
        moved.append(name)
        log.info("她的经历搬到用户数据区: %s -> %s", src, dst)
    return moved


def status(folder: Path) -> dict:
    files = {}
    for name, f in _event_files(folder):
        with f.open(encoding="utf-8") as fh:
            n = sum(1 for line in fh if line.strip())
        files[name] = {"lines": n, "bytes": f.stat().st_size,
                       "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")}
    return {"dir": str(folder), "files": files, "generation": generation(folder), "last_reset": last_reset(folder),
            "history": reset_history(folder)}


def export(folder: Path, out):
    """打包成 zip (里面是那几个 JSONL 和一份 status.json). out: 文件路径, 或者能写的二进制流 (服务端接口直接回给页面)."""
    if not hasattr(out, "write"):
        out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in _event_files(folder):
            z.write(path, name)
        z.writestr("status.json", json.dumps(status(folder), ensure_ascii=False, indent=1))
    return out


def delete(folder: Path) -> list[str]:
    gone = []
    for name, path in _event_files(folder):
        path.unlink()
        gone.append(name)
    return gone


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wm.cognition.store", description="她的经历: 在哪、导出、删除")
    ap.add_argument("cmd", choices=["where", "status", "export", "delete", "migrate"])
    ap.add_argument("out", nargs="?", help="export: 导出到哪个 zip")
    ap.add_argument("--config")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--yes", action="store_true", help="delete: 确认删除")
    a = ap.parse_args(argv)
    from ..config import load_config
    cfg = load_config(a.config, a.set)
    folder = data_dir(cfg)
    if a.cmd == "where":
        print(folder)
    elif a.cmd == "status":
        print(json.dumps(status(folder), ensure_ascii=False, indent=1))
    elif a.cmd == "export":
        out = Path(a.out or f"她的经历-{datetime.now():%Y%m%d-%H%M%S}.zip").expanduser()
        print(export(folder, out))
    elif a.cmd == "delete":
        if not a.yes:
            print(f"会删掉 {folder} 里的 {', '.join(status(folder)['files']) or '(没有文件)'}; 确认的话加 --yes")
            return 1
        print("删掉了: " + (", ".join(delete(folder)) or "(本来就没有)"))
    else:
        print("搬了: " + (", ".join(migrate(cfg)) or "(没有要搬的)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
