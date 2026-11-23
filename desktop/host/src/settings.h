// 桌宠的设置 (操作面板): 人设预设、模型提供方、MCP 服务器、skill. 存在 %APPDATA%\WorldModel\pet-settings.json, 不进版本库.
// 密钥 (云端模型的 API key、MCP 服务器的环境变量和请求头) 用 DPAPI 加密后再存, 只有本机这个用户解得开.
// 面板拿不到密钥的明文, 只能看到"有没有"、换新的、清掉.
//
// 文件的样子:
//   {"version":1, "active":"<人设 id>",
//    "personas":[{"id","name","prompt","provider":"local|deepseek|qwen|kimi|custom","model","mcp":[服务器 id],"skills":[skill 名]}],
//    "providers":{"deepseek":{"base_url","model","key":"<DPAPI>"}, ...},
//    "mcp":[{"id","name","transport":"stdio|http","command","args":[],"cwd","url","enabled","tools":{"工具名":"auto|ask|off"},
//            "secret":"<DPAPI: {env:{}, headers:{}}>"}],
//    "qq":{"enabled", "snowluma_dir", "token":"<DPAPI>", "self_id", "owner_id", "names":[], "persona":"<人设 id>",
//          "convs":[{"type":"group|private","id","name","trigger":"mention|always","send":"draft|auto"}], "block":[QQ 号],
//          "group_send", "private_send", "owner_send", "daily_tokens", "paused"}}   (QQ 渠道, 见根目录 README "外部渠道：QQ")
#pragma once
#include <string>
#include <vector>

#include "json.h"

namespace settings {

void set_dir(const std::wstring& dir);  // 自检用: 换一个目录, 不碰用户真正的设置 (默认 %APPDATA%\WorldModel)
std::wstring path();
std::wstring audit_path();  // 工具调用的审计日志 (tools.h), 和设置放在一起
std::wstring skills_dir();  // %APPDATA%\WorldModel\skills: 一个文件夹一个 skill, 里面有 SKILL.md
Json load();                // 读不到或格式不对时返回默认设置; 缺的内置提供方、默认人设会补上
bool save(const Json& s);
Json for_panel(const Json& s);  // 去掉密钥: 提供方换成 has_key, MCP 服务器换成 secret_names
// 面板发来的设置 + 新填的密钥 -> 要存的设置.
//   keys: {"提供方": "新 key" | "" (清掉)}, 没提到的保留原来的;
//   secrets: {"服务器 id": {"env":{k:v}, "headers":{k:v}, "remove":{"env":[k], "headers":[k]}}}
Json merge(const Json& old, const Json& from_panel, const Json& keys, const Json& secrets);
// 给桌宠页面 (再转给服务端的 profile 消息): 当前人设的名字和人设文字、模型 (云端的带明文 key)、启用的 skill (名字和描述)
Json active_profile(const Json& s);
// 给页面 (再转给服务端) 的 channel_config: token 解密; 人设和模型取 qq.persona 那个预设的 (MCP 和 skill 不给, 外部渠道没有工具).
// 没配过 qq 段返回 null: 不发, 服务端用自己 config 里的 channels.qq
Json channel_config(const Json& s);
Json mcp_server(const Json& s, const std::string& id, std::string* error);  // 连接用: env 和 headers 已解密
// 她自己写、自己装的 MCP (服务端 wm/selfmcp.py 登记): %USERPROFILE%\.worldmodel\self-mcp (自检换了设置目录时用它下面的 self-mcp).
// self_mcp_servers: 名单里开着的、文件 SHA-256 对得上的, 每个 {id, name, transport: stdio, command, args, cwd, env: {}};
// 设置里 self_mcp.enabled 是 false 时一个都不给. 对不上的记日志、不起
std::wstring self_mcp_dir();
Json self_mcp_servers(const Json& s);
// [{name, description, dir, bundled?}]: 用户的 skill, 加上桌宠随附的 (desktop/skills, 用户有同名的就用用户的)
Json scan_skills();
void set_bundled_skills_dir(const std::wstring& dir);  // 随附 skill 放在哪 (main 里按页面目录定)
// 面板 Skill 页的新建 / 改 / 删 (只动用户的 skill 文件夹; 改随附的 = 在用户文件夹存一份同名的, 盖过随附的)
Json read_skill(const std::string& name);  // {name, description, dir, bundled?, body}; 没有就是 null
bool save_skill(const std::string& name, const std::string& description, const std::string& body, std::string& error);
bool delete_skill(const std::string& name, std::string& error);  // 挪进 skills\.trash\<名字>-<时间> (扫描跳过), 不真删
// 这个预设自动带上的 skill: 勾了操控电脑的服务器 (Windows-MCP) 就带 computer-use (操作电脑的要领)
std::vector<std::string> auto_skills(const Json& s, const Json& persona);
std::string protect(const std::string& plain);
bool unprotect(const std::string& b64, std::string& plain);

}  // namespace settings
