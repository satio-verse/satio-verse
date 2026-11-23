"""假命令行替身 (只在 tests/ 里; 生产代码里没有"假命令行开关"): 测试通过 agentmsg.ENGINE_ARGS["launch"] 把它当成 claude / codex 起.
用法: python agentmsg_fake_cli.py --ctl <控制文件> <真模板里的参数…>   (提示文字走标准输入, 和真的一样)

控制文件 (JSON): 默认行为在顶层, 第 n 次被起 (从 0 数) 的行为用 calls[n] 盖掉.
  mode   ok (默认) | fail (对方报错) | garbage (输出不是 JSON) | crash (没输出就退出 3) | sleep (睡 sleep 秒再回) |
         big (输出 5 MB) | grandchild (起一个孙进程再睡, 孙进程号写到 <控制文件>.pid)
  reply  回话正文   cost_usd  Claude 报的花费 (不给就不报)   session  会话号   tokens  Codex 的 usage
每次被起记一行到 <控制文件>.log: 参数、工作目录、环境变量名、标准输入原文 (测试核对"提示走标准输入、环境白名单、模板对得上").
"""
import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
ctl_path = argv[argv.index("--ctl") + 1]
args = argv[argv.index("--ctl") + 2:]
raw = sys.stdin.buffer.read()
prompt = raw.decode("utf-8", errors="replace")
log_path = ctl_path + ".log"
n = 0
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8") as f:
        n = sum(1 for _ in f)
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"n": n, "args": args, "cwd": os.getcwd(), "env": sorted(os.environ), "env_values": dict(os.environ),
                        "stdin": prompt, "pid": os.getpid()}, ensure_ascii=False) + "\n")
with open(ctl_path, encoding="utf-8") as f:
    ctl = json.load(f)
ctl = {**ctl, **(ctl.get("calls") or {}).get(str(n), {})} if isinstance(ctl.get("calls"), dict) else ctl
mode = ctl.get("mode", "ok")
reply = ctl.get("reply", "收到。结论：一切正常。")
is_claude = "-p" in args

if mode == "crash":
    sys.exit(3)
if mode in ("sleep", "grandchild"):
    if mode == "grandchild":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(ctl_path + ".pid", "w") as f:
            f.write(str(child.pid))
    time.sleep(float(ctl.get("sleep", 30)))
if mode == "garbage":
    sys.stdout.write("这不是 JSON\n")
    sys.exit(0)
if mode == "big":
    sys.stdout.write("x" * (5 * 1024 * 1024))
    sys.stdout.flush()
    sys.exit(0)
session = ctl.get("session", "11111111-2222-3333-4444-%012d" % n)
if is_claude:
    out = {"type": "result", "subtype": "error_max_budget_usd" if mode == "fail" else "success", "is_error": mode == "fail",
           "result": reply, "session_id": session, "num_turns": 1}
    if "cost_usd" in ctl:
        out["total_cost_usd"] = ctl["cost_usd"]
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
else:
    lines = [{"type": "thread.started", "thread_id": session}, {"type": "turn.started"}]
    if mode == "fail":
        lines.append({"type": "turn.failed", "error": {"message": reply}})
    else:
        lines.append({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": reply}})
        lines.append({"type": "turn.completed", "usage": ctl.get("tokens", {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 100})})
    for ev in lines:
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
sys.stdout.flush()
