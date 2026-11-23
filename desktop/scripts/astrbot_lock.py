"""把 AstrBot (启动器装的实例) 锁成"只当转接头", 按 docs/roadmap/astrbot-wm-guest.md 第 4 节改它的 data/cmd_config.json 和 data_v4.db.

AstrBot 要先停 (启动器里点停止; 它开着时改配置会被它存回去盖掉), 改完再启动. 第一次改之前备份 cmd_config.json (同目录 .bak-before-wm).
  python astrbot_lock.py --data <实例>\\core\\data                 锁好, 模型服务商指到桌宠本机接口 8793 (访客 token、wm-guest)
  python astrbot_lock.py --data ... --api-port 8795              自检时指到测试桌宠 (astrbottest.ps1 用), 测完再按默认端口跑一次改回来
  python astrbot_lock.py --data ... --new-password               另生成一个 WebUI 密码, 明文写到启动器目录的 wm-guest-webui-password.txt
                                                                 (第一次登录后 AstrBot 会要求改掉; 改完把这个文件删了)
  python astrbot_lock.py --data ... --check                      只核对不改: 有不对的项就列出来, 返回 1
只动 AstrBot 自己的数据目录, 不碰我们的项目和桌宠的设置. 4.29.3 核对过键名 (见方案第 4 节).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import string
import sys
from pathlib import Path

SOURCE_ID = "wm-pet"        # 模型服务商 (provider source): 桌宠的本机接口
PROVIDER_ID = "wm-guest"    # 模型: 只有 wm-guest
PERSONA_ID = "wm-guest-empty"
# 人格: 系统提示桌宠那边不看 (宿主只取最后一条 user 消息); 不选工具、不选 Skills.
# 我们这边不回 (外发检查拦下、限频、额度用完、她选了不说) 时 AstrBot 会当成模型出错, 发这个人格的"出错时回复"给对方, 不发错误详情
PERSONA_PROMPT = "这里故意留空: 人设和回复都在桌宠那边的 wm-guest (没有工具、读不到主人的记忆). 不要在这里加工具."
PERSONA_ERROR = "……"
# 白名单: 空着就是不过滤. 放一条谁都对不上的占位, 以后接了平台默认谁也进不来;
# 对方发消息时 AstrBot 日志里会打出他的会话号 (Session ID ... is not in the session allowlist), 确认是你自己再加进来
WL_PLACEHOLDER = "wm:只放自己-接平台后从日志里抄会话号替换这一条"
OFF = {"allow_execution": False, "allow_network": False, "filesystem_scope": "none"}


def locks(api_port: int, token: str) -> list[tuple[str, object]]:
    """(键路径, 值): 第 4 节那张表. 键路径用点分; 值是整个替换 (列表、字典也是)."""
    return [
        # 模型服务商: 只有桌宠本机接口一个; modalities 不含 tool_use 时 AstrBot 自己就不往请求里放工具
        ("provider_sources", [{"id": SOURCE_ID, "provider": "openai", "type": "openai_chat_completion",
                               "provider_type": "chat_completion", "enable": True, "key": [token],
                               "api_base": f"http://127.0.0.1:{api_port}/v1", "timeout": 120, "proxy": "",
                               "custom_headers": {}}]),
        ("provider", [{"id": PROVIDER_ID, "provider_source_id": SOURCE_ID, "model": "wm-guest", "enable": True,
                       "modalities": ["text"]}]),
        ("provider_stt_settings.enable", False),
        ("provider_tts_settings.enable", False),
        ("agent_runner.runner_type", "local"),
        ("agent_runner.config.model.provider_id", PROVIDER_ID),
        ("agent_runner.config.model.fallback_provider_ids", []),
        ("agent_runner.config.model.request_max_retries", 1),       # 默认 5 (含 429): 每次重试都把同一句再塞进访客会话一次
        ("agent_runner.config.persona.persona_id", PERSONA_ID),     # 不能用 default: ChatUI 在默认人格下用"全部工具"
        # 上下文: 默认"由 LLM 压缩", 会额外给 wm-guest 发一条"请总结", 混进访客会话
        ("agent_runner.config.compression.overflow_strategy", "truncate_by_turns"),
        ("agent_runner.config.compression.max_turns", 10),
        ("agent_runner.config.misc.max_steps", 4),
        ("subagent_orchestrator.main_enable", False),
        ("subagent_orchestrator.agents", []),
        # 工具来源: 插件、未来任务 (默认每次请求都带)、使用电脑能力、网页搜索、知识库
        ("plugin_set", []),
        ("disable_builtin_commands", True),                         # /sid /help /new /reset /provider ... 都不认
        ("provider_settings.proactive_capability.add_cron_tools", False),
        ("provider_settings.computer_use_runtime", "none"),
        ("provider_settings.computer_use_require_admin", True),
        ("provider_settings.computer_use_local_permissions", {"member": dict(OFF), "admin": dict(OFF)}),
        ("provider_settings.web_search", False),
        ("provider_settings.file_extract.enable", False),
        ("provider_settings.streaming_response", False),
        # 往对方那句话后面拼的 <system_reminder> (时间、平台用户 ID 和昵称、群名): 宿主只取最后一条 user 消息, 拼了就进她的上下文
        ("provider_settings.datetime_system_prompt", False),
        ("provider_settings.identifier", False),
        ("provider_settings.group_name_display", False),
        ("provider_settings.prompt_prefix", "{{prompt}}"),
        ("default_kb_collection", ""),
        ("kb_names", []),
        ("kb_agentic_mode", False),
        ("provider_ltm_settings.group_icl_enable", False),
        ("provider_ltm_settings.image_caption", False),
        ("provider_ltm_settings.group_message_history_enable", False),
        ("provider_ltm_settings.active_reply.enable", False),
        ("t2i", False),
        # 文转图地址空着时, 收到第一条消息后会去 api.soulter.top 拉官方地址列表 (文转图关着也拉); 填个本机地址就不拉, 反正用不到
        ("t2i_endpoint", "http://127.0.0.1:9"),
        ("admins_id", []),
        # 平台侧: 白名单、限流、关键词检查也查回复
        ("platform_settings.enable_id_white_list", True),
        ("platform_settings.rate_limit", {"time": 60, "count": 10, "strategy": "discard"}),
        ("content_safety.internal_keywords.enable", True),
        ("content_safety.also_use_in_response", True),
        # WebUI 只听本机 (启动器还会用环境变量 DASHBOARD_HOST 传, 那个优先), 文件服务不对外, 不上报统计
        ("dashboard.host", "127.0.0.1"),
        ("dashboard.port", 6185),
        ("callback_api_base", ""),
        ("disable_metrics", True),
        ("log_file_enable", True),                                  # 日志落盘 (data/logs), 自检和查问题用
        ("http_proxy", ""),
    ]


def get(cfg: dict, path: str):
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return KeyError
        cur = cur[k]
    return cur


def put(cfg: dict, path: str, value) -> None:
    keys = path.split(".")
    cur = cfg
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def whitelist_ok(cfg: dict) -> bool:
    wl = get(cfg, "platform_settings.id_whitelist")
    return isinstance(wl, list) and any(str(i).strip() for i in wl)


def persona_row(db: sqlite3.Connection):
    return db.execute("select system_prompt, tools, skills, custom_error_message from personas where persona_id=?",
                      (PERSONA_ID,)).fetchone()


def check(cfg: dict, db: sqlite3.Connection, api_port: int, token: str) -> list[str]:
    bad = []
    for path, want in locks(api_port, token):
        have = get(cfg, path)
        if have != want:
            shown = "<没有>" if have is KeyError else json.dumps(have, ensure_ascii=False)
            if path == "provider_sources":  # 里面有 token, 不打出来
                shown = "<不一样>"
            bad.append(f"{path} = {shown}")
    if not whitelist_ok(cfg):
        bad.append("platform_settings.id_whitelist 是空的 (空着就是不过滤)")
    row = persona_row(db)
    if not row or json.loads(row[1] or "null") != [] or json.loads(row[2] or "null") != []:
        bad.append(f"人格 {PERSONA_ID} 没有, 或者选了工具 / Skills")
    mcp = Path(cfg["_data"]) / "mcp_server.json"
    if mcp.exists() and json.loads(mcp.read_text(encoding="utf-8-sig") or "{}").get("mcpServers"):
        bad.append("mcp_server.json 里有 MCP 服务器")
    plugins = [p.name for p in (Path(cfg["_data"]) / "plugins").glob("*") if p.is_dir() and not p.name.startswith((".", "_"))]
    if plugins:
        bad.append(f"data/plugins 里装了插件: {', '.join(plugins)}")
    if get(cfg, "platform") not in (KeyError, []):
        names = [str(p.get("type", "?")) + ":" + str(p.get("id", "?")) for p in cfg["platform"] if isinstance(p, dict)]
        print(f"提示: 接了平台 {', '.join(names)}; 确认白名单里只有你自己的会话号")
    return bad


def new_password() -> str:
    """和 AstrBot 自己生成初始密码一样: 24 位, 至少一个大写、一个小写、一个数字."""
    alphabet = string.ascii_letters + string.digits
    chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits)]
    chars += [secrets.choice(alphabet) for _ in range(21)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def set_password(cfg: dict, raw: str) -> None:
    """AstrBot 的 auth_password: PBKDF2-SHA256 60 万轮 (另存一份 MD5 兼容旧版); 第一次登录要求改密码."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), bytes.fromhex(salt), 600_000).hex()
    put(cfg, "dashboard.pbkdf2_password", f"pbkdf2_sha256$600000${salt}${digest}")
    put(cfg, "dashboard.password", hashlib.md5(raw.encode("utf-8")).hexdigest())
    put(cfg, "dashboard.password_storage_upgraded", True)
    put(cfg, "dashboard.password_change_required", True)


def running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="AstrBot 实例的 data 目录 (启动器: <实例>\\core\\data)")
    ap.add_argument("--api-port", type=int, default=8793, help="桌宠本机接口的端口 (自检时是测试桌宠的)")
    ap.add_argument("--token-file", default=os.path.join(os.environ.get("APPDATA", "."), "WorldModel", "pet-api-guest-token.txt"))
    ap.add_argument("--new-password", action="store_true", help="另生成 WebUI 密码 (明文写到 --password-file)")
    ap.add_argument("--password-file", default="")
    ap.add_argument("--check", action="store_true", help="只核对, 不改")
    args = ap.parse_args()

    data = Path(args.data).resolve()
    conf_path, db_path = data / "cmd_config.json", data / "data_v4.db"
    if not conf_path.exists() or not db_path.exists():
        print(f"{data} 里没有 cmd_config.json / data_v4.db: 先在启动器里把实例启动一次再停掉")
        return 2
    if not os.path.exists(args.token_file):
        print(f"没有访客 token 文件 {args.token_file}: 先在桌宠面板里打开本机接口")
        return 2
    token = open(args.token_file, encoding="utf-8").read().strip()
    cfg = json.loads(conf_path.read_text(encoding="utf-8-sig"))
    port = get(cfg, "dashboard.port")
    port = port if isinstance(port, int) else 6185

    if args.check:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cfg["_data"] = str(data)
        bad = check(cfg, db, args.api_port, token)
        for b in bad:
            print("不对:", b)
        print("全部锁好" if not bad else f"{len(bad)} 项不对")
        return 1 if bad else 0

    if running(port):
        print(f"AstrBot 还开着 (127.0.0.1:{port}): 先在启动器里停掉, 不然它会把改动盖掉")
        return 2
    backup = conf_path.with_name("cmd_config.json.bak-before-wm")
    if not backup.exists():
        backup.write_bytes(conf_path.read_bytes())
        print("备份了原来的配置:", backup)
    old_ids = [p.get("id") for p in cfg.get("provider", []) if isinstance(p, dict) and p.get("id") != PROVIDER_ID]
    if old_ids:
        print("去掉了别的模型:", ", ".join(map(str, old_ids)))
    for path, value in locks(args.api_port, token):
        put(cfg, path, value)
    if not whitelist_ok(cfg):
        put(cfg, "platform_settings.id_whitelist", [WL_PLACEHOLDER])
    if args.new_password:
        pw_file = Path(args.password_file) if args.password_file else (
            data.parents[3] if data.parents[3].name == ".astrbot_launcher" else data.parent) / "wm-guest-webui-password.txt"
        raw = new_password()
        set_password(cfg, raw)
        pw_file.write_text(f"AstrBot WebUI (http://127.0.0.1:{port}) 用户名 {get(cfg, 'dashboard.username')}\n密码 {raw}\n"
                           "第一次登录后它会要求改密码; 改完把这个文件删了.\n", encoding="utf-8")
        print("新的 WebUI 密码写在:", pw_file)
    conf_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8-sig")

    db = sqlite3.connect(db_path)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    if persona_row(db):
        db.execute("update personas set system_prompt=?, tools='[]', skills='[]', begin_dialogs='[]', custom_error_message=?, "
                   "updated_at=? where persona_id=?", (PERSONA_PROMPT, PERSONA_ERROR, now, PERSONA_ID))
    else:
        order = db.execute("select coalesce(max(sort_order), 0) + 1 from personas").fetchone()[0]
        db.execute("insert into personas (created_at, updated_at, persona_id, system_prompt, begin_dialogs, tools, skills, "
                   "custom_error_message, folder_id, sort_order) values (?, ?, ?, ?, '[]', '[]', '[]', ?, null, ?)",
                   (now, now, PERSONA_ID, PERSONA_PROMPT, PERSONA_ERROR, order))
    db.commit()
    cfg["_data"] = str(data)
    bad = check(cfg, db, args.api_port, token)
    for b in bad:
        print("还不对:", b)
    print(f"锁好了: 模型只有 wm-guest (127.0.0.1:{args.api_port}), 人格 {PERSONA_ID} 不带工具" if not bad else "有项没锁上")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
