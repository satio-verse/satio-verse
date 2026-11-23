"""换声音：把 models/tts/<名字>/voice.json 里的权重与参考音写进 config/local.json，再让看守重启 TTS。

为什么要这个工具: 一套声音 = GPT 权重 + SoVITS 权重 + 参考音 + 参考音文本, 四样必须一起换;
分开改 config/local.json 容易只换一半 (实测踩过: 换了 SoVITS 忘了参考音文本, 出的是别人的音色).
参考音对可懂度影响最大 (同一套权重 7 条参考音 CER 差 4 倍), 所以每个 voice.json 都带一份 ref_scan
记录, 换权重后应当用 tools/ref_scan.py 重扫一遍再定参考音.

用法:
  python tools/switch_voice.py list
  python tools/switch_voice.py show ayane
  python tools/switch_voice.py check ayane
  python tools/switch_voice.py apply ayane              # 写配置 + 重启 9880
  python tools/switch_voice.py apply ayane --no-restart  # 只写配置, 下次重启生效
  python tools/switch_voice.py apply ayane --dry-run     # 只打印会改什么
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES = ROOT / "models" / "tts"
LOCAL_CFG = ROOT / "config" / "local.json"
# 输出端补偿写在单独文件里, 服务端按 mtime 热加载 —— 换声音不用重启 8790.
TONE_CFG = ROOT / "config" / "tts-tone.json"
PORT = 9880

FIELDS = ("version", "gpt", "sovits", "ref_audio", "prompt_text", "prompt_lang", "text_lang",
          "sample_rate", "sample_steps", "seed")
# 只有 v5 系才认的字段: 别的版本写不写都行, 不写就不发 (wm.tts 只在配置里有值时才带上它)
OPTIONAL_FIELDS = ("sample_steps", "seed")
# 输出端补偿 (音量/明暗), 由 wm.tts.ToneShaper 生效; 不属于「权重」, 但必须跟声音一起换.
OUT_KEYS = ("gain_db", "shelf_db", "shelf_fc", "limit")


def norm_output(v: dict) -> dict:
    out = v.get("output") or {}
    return {k: float(out[k]) for k in OUT_KEYS if out.get(k) is not None}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def dump_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def profiles() -> dict[str, dict]:
    out = {}
    for p in sorted(VOICES.glob("*/voice.json")):
        try:
            v = load_json(p)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {p}: {e}")
            continue
        v["_path"] = str(p)
        out[v.get("name") or p.parent.name] = v
    return out


def resolve(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def current() -> dict:
    """现在真正生效的那套: config/local.json 写了就用, 没写的用 config/default.json 兜底."""
    local = load_json(LOCAL_CFG)
    default = load_json(ROOT / "config" / "default.json") if (ROOT / "config" / "default.json").is_file() else {}
    lhttp = ((local.get("tts") or {}).get("http") or {})
    dhttp = ((default.get("tts") or {}).get("http") or {})
    srv = {**((dhttp.get("server") or {})), **((lhttp.get("server") or {}))}
    return {
        "version": srv.get("version", ""),
        "gpt": srv.get("gpt", ""),
        "sovits": srv.get("sovits", ""),
        "ref_audio": lhttp.get("ref_audio_path") or dhttp.get("ref_audio_path") or "",
        "prompt_text": lhttp.get("prompt_text") or dhttp.get("prompt_text") or "",
        "prompt_lang": lhttp.get("prompt_lang") or dhttp.get("prompt_lang") or "zh",
        "sample_rate": lhttp.get("sample_rate") or dhttp.get("sample_rate") or 32000,
        "sample_steps": lhttp.get("sample_steps") or dhttp.get("sample_steps") or "",
        "seed": lhttp.get("seed") or dhttp.get("seed") or "",
        "text_lang": lhttp.get("text_lang") or dhttp.get("text_lang") or "zh",
        "output": tone_now(),
    }


def tone_now() -> dict:
    if not TONE_CFG.is_file():
        return {}
    try:
        return dict(load_json(TONE_CFG))
    except Exception:  # noqa: BLE001
        return {}


def cmd_list(_args) -> int:
    cur = current()
    print(f"{'name':10} {'version':12} GPT / SoVITS / 参考音")
    for name, v in profiles().items():
        mark = " *" if v["gpt"] == cur["gpt"] and v["sovits"] == cur["sovits"] else ""
        print(f"{name:10} {v.get('version', ''):12} {v['gpt'].split('/')[-1]} + {v['sovits'].split('/')[-1]}")
        print(f"{'':10} {'':12} ref {Path(v['ref_audio']).name}  «{v['prompt_text'][:18]}…»{mark}")
    if cur["gpt"]:
        print(f"\n现在生效: {Path(cur['gpt']).name} + {Path(cur['sovits']).name}, ref {Path(cur['ref_audio']).name}")
    else:
        print("\n现在生效: config/local.json 没写, 用 config/default.json 的默认 (服务端自带的合成器)")
    return 0


def fmt_out(d: dict) -> str:
    if not d:
        return "(无补偿)"
    parts = []
    if abs(d.get("gain_db", 0)) > 0.01:
        parts.append(f"{d['gain_db']:+.1f} dB")
    if abs(d.get("shelf_db", 0)) >= 0.1:
        parts.append(f"{d['shelf_db']:+.1f} dB @ {d.get('shelf_fc', 6500):.0f} Hz")
    if d.get("limit"):
        parts.append(f"限幅 {d['limit']:.2f}")
    return ", ".join(parts) or "(无补偿)"


def cmd_show(args) -> int:
    v = profiles().get(args.name)
    if v is None:
        print(f"没有这个声音: {args.name}; 有: {', '.join(profiles())}")
        return 2
    cur = current()
    print(f"## {v['name']} — {v.get('title', '')}")
    for k in FIELDS:
        old, new = cur.get(k, ""), v.get(k, "")
        flag = "  " if old == new else "->"
        print(f"{flag} {k:12} {old if len(str(old)) < 60 else str(old)[:57] + '…'}")
        if old != new:
            print(f"   {'':12} {new if len(str(new)) < 60 else str(new)[:57] + '…'}")
    cur_out, new_out = dict(cur.get("output") or {}), norm_output(v)
    flag = "  " if cur_out == new_out else "->"
    print(f"{flag} {'output':12} {fmt_out(cur_out)}")
    if cur_out != new_out:
        print(f"   {'':12} {fmt_out(new_out)}")
    if v.get("note"):
        print(f"\nnote: {v['note']}")
    if v.get("ref_scan"):
        best = min(v["ref_scan"].items(), key=lambda kv: kv[1])
        print(f"ref_scan: 最好的是 {best[0]} (CER {best[1]})")
    return 0


def cmd_check(args) -> int:
    v = profiles().get(args.name)
    if v is None:
        print(f"没有这个声音: {args.name}")
        return 2
    bad = 0
    for k in ("gpt", "sovits", "ref_audio"):
        p = resolve(v[k])
        if not p.is_file():
            print(f"[缺] {k}: {p}")
            bad += 1
            continue
        head = p.read_bytes()[:4]
        extra = ""
        if k == "sovits":
            # 头两个字节是 ASCII 的版本标记 ("06" = 0x30 0x36), 不是 0x06; 详见 process_ckpt.py:78-86
            extra = {b"PK\x03\x04": " (PK, 可直接 load)",
                     b"06\x03\x04": ' ("06" = v2ProPlus, 走 load_sovits_new)',
                     b"07\x03\x04": ' ("07" = v5dev LoRA)',
                     b"08\x03\x04": ' ("08" = v5turbo LoRA)',
                     b"09\x03\x04": ' ("09" = v5dev 合并版, 需要 b"09" 一行补丁)'}.get(head, "")
        print(f"[ok] {k}: {p.name} {p.stat().st_size} B {head.hex(' ')}{extra}")
    ref_txt = resolve(v["ref_audio"]).with_suffix(".txt")
    if ref_txt.is_file():
        got = ref_txt.read_text(encoding="utf-8").strip()
        if got != v["prompt_text"].strip():
            print(f"[!] 参考音文本对不上: {ref_txt.name} 里是 «{got}», voice.json 里是 «{v['prompt_text']}»")
            bad += 1
        else:
            print(f"[ok] 参考音文本一致 ({len(got)} 字)")
    else:
        print(f"[i] 没有 {ref_txt.name}, 用 voice.json 里的 prompt_text")
    return 1 if bad else 0


def cmd_apply(args) -> int:
    v = profiles().get(args.name)
    if v is None:
        print(f"没有这个声音: {args.name}")
        return 2
    if cmd_check(args) != 0:
        print("先修好上面的问题再换。")
        return 1

    local = load_json(LOCAL_CFG)
    tts = local.setdefault("tts", {})
    http = tts.setdefault("http", {})
    srv = http.setdefault("server", {})

    cur = current()
    desired = {k: v.get(k) for k in FIELDS}
    changed = {k: n for k, n in desired.items() if n and cur.get(k) != n}
    # 换回「不认某些字段」的版本时 (例如 v5turbo 的 sample_steps/seed 换回 v2ProPlus), 把旧值删掉, 别留脏配置
    stale = [k for k in FIELDS if desired.get(k) in (None, "") and cur.get(k) not in (None, "")]
    cur_out, new_out = dict(cur.get("output") or {}), norm_output(v)
    out_changed = cur_out != new_out

    for k, new in changed.items():
        old = cur.get(k)
        print(f"改 {k:14} {old}  ->  {new}")
    for k in stale:
        print(f"删 {k:14} {cur.get(k)}  (这套声音不写这一项)")
    for k in FIELDS:
        if k not in changed and k not in stale:
            print(f"   {k:14} {cur.get(k)} (没变)")
    if out_changed:
        print(f"改 {'output':14} {fmt_out(cur_out)}  ->  {fmt_out(new_out)}")
    else:
        print(f"   {'output':14} {fmt_out(cur_out)} (没变)")

    if not changed and not out_changed:
        print("\n配置已经是这一套了（不改文件）。")
        if args.dry_run or args.no_restart:
            return 0
        return restart_tts()
    if args.dry_run:
        print("\n--dry-run: 没写文件, 也没重启。")
        return 0

    mapping = {"version": (srv, "version"), "gpt": (srv, "gpt"), "sovits": (srv, "sovits"),
               "ref_audio": (http, "ref_audio_path"), "prompt_text": (http, "prompt_text"),
               "prompt_lang": (http, "prompt_lang"), "text_lang": (http, "text_lang"),
               "sample_rate": (http, "sample_rate"), "sample_steps": (http, "sample_steps"),
               "seed": (http, "seed")}
    for k, new in changed.items():
        d, key = mapping[k]
        d[key] = new
    for k in stale:
        d, key = mapping[k]
        d.pop(key, None)

    if changed or stale:
        backup = LOCAL_CFG.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(LOCAL_CFG, backup)
        dump_json(LOCAL_CFG, local)
        print(f"\n写好了 config/local.json（备份 {backup.name}）")
    if out_changed:
        if new_out:
            dump_json(TONE_CFG, new_out)
        elif TONE_CFG.is_file():
            TONE_CFG.unlink()
        print(f"写好了 config/tts-tone.json（服务端按 mtime 热加载）")

    if not changed:
        print("\n只有补偿变了：已经生效，不用重启。")
        return 0
    if args.no_restart:
        print("没重启: 下次看守重启 TTS 时生效。")
        return 0
    return restart_tts()


def port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def restart_tts() -> int:
    """让看守重启 TTS, 并且等到「新实例真的起来了」才算成功。

    坑: 看守是异步的, 请求刚提交时**旧实例还在监听**, 直接探端口会立刻「成功」—— 实际换的是旧声音.
    所以这里等的是 start_tts.py 重写 TEMP/wm-tts-<port>.yaml (每次启动都会写), 再等端口.
    """
    py = sys.executable
    yaml_path = ROOT / "runtime" / "gpt-sovits" / "TEMP" / f"wm-tts-{PORT}.yaml"
    before = yaml_path.stat().st_mtime_ns if yaml_path.is_file() else 0
    print(f"重启 TTS: {Path(py).name} -m wm.services restart tts")
    r = subprocess.run([py, "-m", "wm.services", "restart", "tts"], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    deadline = time.time() + 150
    rewritten = False
    while time.time() < deadline:
        time.sleep(0.5)
        if not rewritten:
            rewritten = yaml_path.is_file() and yaml_path.stat().st_mtime_ns > before
            continue
        if port_open(PORT):
            time.sleep(1.5)  # 起来之后它自己还会跑一句预热, 给它留点时间
            print(f"{PORT} 已经用新配置起来了。")
            return 0
    print(f"{PORT} 等不到新实例, 看看守日志: {ROOT / 'logs' / 'services'}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="换声音 (权重 + 参考音一起换, 再重启 TTS)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有声音和现在生效的那个").set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="看某个声音会改哪几项")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("check", help="检查文件在不在、参考音文本对不对")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("apply", help="写进 config/local.json 并重启")
    p.add_argument("name")
    p.add_argument("--no-restart", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
