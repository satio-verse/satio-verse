"""工作台的权限: 保留原三档，增加按可编辑黑名单执行的完全授权档。

| 档 | 读 (文件、目录、搜) | 写、改文件 | 跑命令 | 鼠标点按 |
|---|---|---|---|---|
| read 仅可查看 | 自动 | 不让 | 不让 | 不让 |
| workspace 工作区内修改 (默认) | 自动 | 工作区里自动, 外面不让 | 只读的命令自动 (readonly), 别的每次问 (可以"这一轮都允许"、"以后都允许") | 每次问 (点下去可能触发什么都说不准) |
| full 自动执行 | 自动 | 自动 (系统目录不让) | 自动; 高风险的每次问 | 每次问 |
| unrestricted 完全授权 | 自动 | 自动 | 自动 | 自动 |

read / workspace / full 保留原来的固定守门:
- 保护的地方读写都不让: 桌宠的设置和 token (%APPDATA%\\WorldModel)、~/.worldmodel 里的 token / Cookie / 微信账号 / 邮箱授权码 / 账号保管箱、
  记忆库 (memory.vault)、~/.ssh 这类密钥、浏览器的登录数据;
- 命令里有格式化、关机、改注册表 / 服务 / 计划任务 / 账户、提权、编码过的命令、下载来就运行、碰保护的地方的: 不让;
- 删东西、联网下载、装软件、强推 git、结束进程这些"高风险"的: 哪一档都要你点头 (完全权限也问).
unrestricted 不执行下面这些固定守门。调用方必须先通过 governance.BlacklistStore.check，
其中规则对所有模式优先，默认保护项可见、可由用户编辑。它是工具授权策略，不是操作系统沙箱。
提权 (runas、sudo、Start-Process -Verb …) 也是这样: 固定守门里有一份 (下面 _DENY), 完全授权档靠黑名单的默认项拦
(governance.BlacklistStore._elevation_defaults; 2026-11-11 以前黑名单里没有, 那一档的提权是放行的)。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

MODES = {"read": "仅可查看", "workspace": "工作区内修改", "full": "自动执行", "unrestricted": "完全授权"}
READ_TOOLS = {"read_file", "list_dir", "glob", "grep"}
WRITE_TOOLS = {"write_file", "edit_file"}
ACTION_TOOLS = {"mouse_click"}  # 会动主人电脑的工具: 只可查看不让, 完全授权之外每次问


@dataclass
class Decision:
    how: str          # auto / ask / deny
    why: str = ""     # 不让、要问的原因 (给人看, 也交给模型)
    risk: str = ""    # 问的时候: write / command / power (高风险)


def _home() -> Path:
    return Path.home()


def protected_paths(vault: str | None = None) -> list[Path]:
    """读写都不让碰的地方."""
    home = _home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    out = [appdata / "WorldModel", home / ".worldmodel" / "weixin", home / ".worldmodel" / "bili_cookie.txt",
           home / ".worldmodel" / "serve", home / ".worldmodel" / "mail", home / ".worldmodel" / "vault",
           home / ".ssh", home / ".aws", home / ".gnupg",
           home / ".git-credentials",
           local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
           appdata / "Mozilla" / "Firefox" / "Profiles", local / "Packages"]
    if vault:
        out.append(Path(vault))
    return [p for p in out]


def system_paths() -> list[Path]:
    """不让写的系统目录 (读可以)."""
    windir = Path(os.environ.get("WINDIR") or "C:/Windows")
    pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
    return [windir] + [Path(p) for p in pf if p]


# MSIX 打包的应用 (比如 Claude 桌面版) 里跑的进程, 文件系统看 %APPDATA% / %LOCALAPPDATA% 时会被重定向到
# …\AppData\Local\Packages\<包名>\LocalCache\Roaming (或 Local): Path.resolve() 拿到的就是这种路径. 2026-10-28 守门名单在这样的
# 进程里初始化, 保护桌宠设置和 token 的三条规则记成了这种路径, 用户自己起的服务端看的是真正的位置 —— 完全授权档 (只认名单)
# 里真的设置和 token 反而没挡住 (2026-11-07 底层对标时查到; 用户定"只修名单里的路径"). 比较路径前一律换回真正的位置.
_VIRTUAL = re.compile(r"^(?P<home>[a-z]:\\users\\[^\\]+)\\appdata\\local\\packages\\[^\\]+\\localcache\\(?P<kind>roaming|local)"
                      r"(?P<rest>\\.*)?$", re.IGNORECASE)


def unvirtualize(raw: str) -> str:
    """打包应用里重定向过的路径换回真正的位置 (…\\Packages\\<包>\\LocalCache\\Roaming\\X -> …\\AppData\\Roaming\\X);
    不是这种路径原样返回. 只认 Windows 的写法 (反斜杠)."""
    text = str(raw or "")
    m = _VIRTUAL.match(text.replace("/", "\\"))
    if not m:
        return text
    return m["home"] + "\\AppData\\" + ("Roaming" if m["kind"].lower() == "roaming" else "Local") + (m["rest"] or "")


def _norm(p: Path) -> str:
    return os.path.normcase(unvirtualize(os.path.abspath(str(p))))


def inside(path: Path, root: Path) -> bool:
    a, b = _norm(path), _norm(root)
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


# 命令守门: 不让的 (哪一档都不跑) 和高风险的 (哪一档都问)
_DENY = [
    (r"\bformat(\.com)?\s+[a-z]:", "格式化磁盘"), (r"\bdiskpart\b", "改分区"), (r"\b(shutdown|restart-computer|stop-computer)\b", "关机重启"),
    (r"\b(reg(\.exe)?\s+(add|delete|import)|set-itemproperty\s+.*hk(lm|cu):|new-itemproperty)", "改注册表"),
    (r"\b(sc(\.exe)?\s+(create|delete|config)|new-service|set-service|remove-service)\b", "改系统服务"),
    (r"\b(schtasks(\.exe)?\s+/(create|change|delete)|register-scheduledtask|unregister-scheduledtask)", "改计划任务"),
    (r"\bnet\s+(user|localgroup)\b|\b(new|remove|set)-localuser\b", "改系统账户"),
    (r"-verb\s+runas|\brunas(user)?(\.exe)?\b|\bg?sudo(\.exe)?\b", "要管理员权限"),
    (r"-e(nc(odedcommand)?)?\s+[a-z0-9+/=]{16,}", "编码过的命令 (看不出在做什么)"),
    (r"\b(iex|invoke-expression)\b", "把文字当命令执行"),
    (r"(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)[^|]*\|\s*(iex|invoke-expression|powershell|cmd|bash|sh)\b", "下载来就运行"),
    (r"\b(bcdedit|vssadmin|cipher\s+/w|takeown|icacls\s+.*/grant|set-executionpolicy|set-mppreference|add-mppreference)\b", "改系统安全设置"),
    (r"\b(rm\s+-rf\s+/|remove-item\s+[\"']?[a-z]:\\?[\"']?\s|del\s+/s\s+/q\s+[a-z]:\\?\s*$)", "删整个盘"),
]
_POWER = [
    (r"\b(remove-item|rm|rmdir|rd|del|erase|ri)\b", "删东西"),
    (r"\b(move-item|mv|move)\b", "挪文件"),
    (r"\b(invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|start-bitstransfer)\b", "联网下载"),
    (r"\b(pip|pip3|npm|pnpm|yarn|winget|choco|scoop|uv)\s+(install|add|i|update|upgrade)\b", "装软件"),
    (r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+--|rebase|filter-branch)\b", "改 git 历史或推到远端"),
    (r"\b(stop-process|taskkill|kill)\b", "结束进程"),
    (r"\b(start-process|start)\s+.*\.(exe|msi|bat|cmd|ps1)\b", "启动程序"),
    (r"\bset-content\b|\bout-file\b|>\s*[a-z]:\\", "写文件 (用命令写)"),
]
_DENY_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _DENY]
_POWER_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _POWER]
# 只读的命令 (看目录、读文件、搜、看 git 状态、看版本): 工作区内修改档也不用问 (2026-10-26 用户试的时候,
# 她想用 Get-ChildItem 再看一眼隐藏文件也弹了申请, 太勤). 只认一条命令 (不许 ; && ||), 管道后面只许接整理输出的
READONLY_HEAD = re.compile(
    r"^\s*(get-childitem|gci|dir|ls|get-content|gc|cat|type|get-item|gi|test-path|resolve-path|get-location|pwd|"
    r"select-string|sls|measure-object|get-filehash|get-date|get-command|where(\.exe)?|tree|hostname|"
    r"git\s+(status|log|diff|show|branch|rev-parse|ls-files|blame)|"
    r"(python|py|node|npm|pip|git|dotnet|java|go|cargo)\s+(--version|-v|-V|version))\b", re.IGNORECASE)
_READONLY_PIPE = re.compile(r"^\s*(select-object|select|sort-object|sort|format-table|ft|format-list|fl|measure-object|measure|"
                            r"where-object|where|\?|select-string|sls|out-string|group-object|group)\b", re.IGNORECASE)


def readonly(cmd: str) -> bool:
    """只看不改的一条命令: 开头在 READONLY_HEAD 里, 没有 ; && || 和重定向, 管道后面只接整理输出的."""
    if re.search(r";|&&|\|\||[<>]|`|\$\(", cmd):
        return False
    first, *rest = cmd.split("|")
    return bool(READONLY_HEAD.match(first)) and all(_READONLY_PIPE.match(p) for p in rest)
_ENVISH = re.compile(r"%[A-Za-z_]+%|\$env:|~[\\/]", re.IGNORECASE)

# "以后都允许" (2026-10-28 用户: "允许一次, 也要加入默认许可或者以后都允许的白名单"): 名单里记命令开头几个词, 以后这样开头的
# 单条命令 (不许 ; && || 重定向, 管道后面只许接整理输出的) 在"工作区内修改"档不再问. 守门拦的照样拦, 高风险的照样每次问.
# 记几个词: 解释器记到脚本 (python a.py; 带 -c / -e 这种直接给代码的不给记), git / npm 这类记到子命令, 别的记程序名.
_INTERP = {"python", "python3", "py", "node", "deno", "bun", "powershell", "pwsh", "cmd", "bash", "sh", "ruby", "perl"}
_TWO_WORDS = {"git", "npm", "pnpm", "yarn", "npx", "pip", "pip3", "uv", "dotnet", "cargo", "go", "conda", "poetry"}


def _words(cmd: str) -> list[str]:
    words = str(cmd or "").split()
    if not words:
        return []
    prog = re.split(r"[\\/]", words[0].strip("\"'"))[-1].lower()
    return [prog[:-4] if prog.endswith(".exe") else prog, *words[1:]]


def single(cmd: str) -> bool:
    """一条命令: 没有 ; && || 重定向和 $(...) 这些, 管道后面只接整理输出的."""
    if re.search(r";|&&|\|\||[<>]|`|\$\(", cmd or ""):
        return False
    _, *rest = cmd.split("|")
    return all(_READONLY_PIPE.match(p) for p in rest)


def allow_rule(cmd: str) -> str | None:
    """点"以后都允许"时记下的规则 (命令开头几个词); 不是单条命令、解释器直接给代码的不给记 (返回 None)."""
    words = _words(cmd)
    if not words or not single(cmd):
        return None
    if words[0] in _INTERP:
        if len(words) < 2 or words[1][:1] in "-/":
            return None
        return " ".join(words[:2])
    if words[0] in _TWO_WORDS and len(words) >= 2 and words[1][:1] not in "-/":
        return " ".join(words[:2])
    return words[0]


def allowed(cmd: str, rules: list[str] | None) -> str | None:
    """在名单里: 返回对上的那条规则."""
    if not rules or not single(cmd):
        return None
    words = [w.lower() for w in _words(cmd)]
    for rule in rules:
        rw = rule.lower().split()
        if rw and words[:len(rw)] == rw:
            return rule
    return None


def check_command(command: str, mode: str, vault: str | None = None, allow: list[str] | None = None) -> Decision:
    if mode not in MODES:
        return Decision("deny", "未知权限模式")
    cmd = str(command or "")
    if not cmd.strip():
        return Decision("deny", "命令是空的")
    if len(cmd) > 2000:
        return Decision("deny", "命令太长 (超过 2000 字), 拆成几步")
    if mode == "unrestricted":
        return Decision("auto")
    if mode == "read":
        return Decision("deny", "现在是\"仅可查看\", 不能跑命令")
    for rx, why in _DENY_RE:
        if rx.search(cmd):
            return Decision("deny", f"守门拦下: {why}, 这一步要主人自己来")
    low = cmd.lower().replace("/", "\\")
    for p in protected_paths(vault):
        if _norm(p).lower() in os.path.normcase(low) or str(p).lower().replace("/", "\\") in low:
            return Decision("deny", "守门拦下: 命令碰到了保护的地方 (桌宠设置、token、记忆库、密钥、浏览器登录数据)")
    # 用变量写的也认 ($env:APPDATA\WorldModel、%APPDATA%\WorldModel、~\.worldmodel\weixin …)
    if re.search(r"appdata\\roaming\\worldmodel|(\$env:appdata|%appdata%)\\worldmodel|pet-settings\.json|pet-api(-guest)?-token"
                 r"|\.worldmodel\\(weixin|bili_cookie|serve|mail\b|vault\b)|ayuki_remember|\\\.ssh\\|\\\.aws\\|user data\\default\\(login data|cookies)", low):
        return Decision("deny", "守门拦下: 命令碰到了保护的地方 (桌宠设置、token、记忆库、密钥、浏览器登录数据)")
    for rx, why in _POWER_RE:
        if rx.search(cmd):
            return Decision("ask", f"高风险: {why}", "power")
    if mode == "full" or readonly(cmd):
        return Decision("auto")
    if rule := allowed(cmd, allow):
        return Decision("auto", f"在\"以后都允许\"的名单里 ({rule})")
    return Decision("ask", "跑命令 (工作区内修改档每次问)", "command")


def check_action(tool: str, mode: str) -> Decision:
    """动鼠标这类"做一下就收不回来"的工具: 只可查看不让; 完全授权直接做; 别的档每次问 (算高风险)."""
    if mode not in MODES:
        return Decision("deny", "未知权限模式")
    if tool not in ACTION_TOOLS:
        return Decision("deny", f"不是动手类的工具: {tool}")
    if mode == "read":
        return Decision("deny", "现在是\"仅可查看\", 不能动鼠标")
    if mode == "unrestricted":
        return Decision("auto")
    return Decision("ask", "要动主人的鼠标 (点下去会碰到什么说不准)", "power")


def check_path(tool: str, path: Path, mode: str, workspace: Path, vault: str | None = None) -> Decision:
    if mode not in MODES:
        return Decision("deny", "未知权限模式")
    if mode == "unrestricted":
        return Decision("auto")
    for p in protected_paths(vault):
        if inside(path, p):
            return Decision("deny", "守门拦下: 这是保护的地方 (桌宠设置、token、记忆库、密钥、浏览器登录数据), 不能读也不能改")
    if tool in READ_TOOLS:
        return Decision("auto")
    if mode == "read":
        return Decision("deny", "现在是\"仅可查看\", 不能改文件")
    if mode == "workspace":
        if inside(path, workspace):
            return Decision("auto")
        return Decision("deny", f"工作区外面不能改 (工作区是 {workspace}); 要改得主人切到\"完全权限\"")
    for p in system_paths():
        if inside(path, p):
            return Decision("deny", "守门拦下: 系统目录不能改")
    return Decision("auto")


def raw_path_ok(raw: str) -> str | None:
    """路径里有环境变量 / ~ 这类要展开的写法: 不认 (看不出最后落在哪)."""
    return "路径里别写 %变量%、$env: 或 ~, 写完整的路径或相对工作区的路径" if _ENVISH.search(str(raw or "")) else None
