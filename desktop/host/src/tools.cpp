#include "tools.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <functional>
#include <thread>
#include <vector>
#include <shlobj.h>

#include "app.h"
#include "builtins.h"
#include "settings.h"
#include "autonomy.h"

namespace {

// 操控电脑: 一轮里最多调这么多次、这么久 (到了让她停下来问用户); "这一轮都允许"最长管多久
constexpr int kMaxControlCalls = 30;
constexpr ULONGLONG kMaxControlMs = 5 * 60 * 1000;
constexpr ULONGLONG kGrantMs = 5 * 60 * 1000;
// 渠道任务单 (QQ 上主人说的事): 主人不在电脑前, 一件事常要几十步, 上限放宽; 整张单子最多管这么久 (之后作废, 授权跟着没)
constexpr int kTaskControlCalls = 80;
constexpr ULONGLONG kTaskControlMs = 15 * 60 * 1000;
constexpr ULONGLONG kTaskMs = 16 * 60 * 1000;

const Json* find_by(const Json& list, const char* key, const std::string& value) {
  for (const Json& item : list.items())
    if (item.get_str(key) == value) return &item;
  return nullptr;
}

// 模型那里的函数名只能是 [A-Za-z0-9_-], 最多 64 个字; 太长的截短后加一段哈希, 重名的加序号
std::string public_name(const std::string& server, const std::string& raw, const std::set<std::string>& taken) {
  auto clean = [](std::string s) {
    for (auto& c : s)
      if (!isalnum(static_cast<unsigned char>(c)) && c != '_' && c != '-') c = '_';
    return s;
  };
  std::string name = "mcp__" + clean(server) + "__" + clean(raw);
  if (name.size() > 64) {
    char h[16];
    snprintf(h, sizeof h, "%08x", unsigned(std::hash<std::string>()(server + "/" + raw)));
    name = name.substr(0, 55) + "_" + h;
  }
  std::string base = name;
  for (int n = 2; taken.count(name); n++) name = base.substr(0, 60) + "_" + std::to_string(n);
  return name;
}

// 参数摘要: 审计日志和确认框里用, 太长的截断 (不截断 UTF-8 的半个字)
std::string preview(const Json& v, size_t limit = 300) {
  std::string s = v.dump();
  if (s.size() <= limit) return s;
  size_t cut = limit;
  while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) cut--;
  return s.substr(0, cut) + "…";
}

// 连接用的配置的指纹: 只看怎么连 (改了权限、名字不用重连)
std::string conn_key(const Json& cfg) {
  Json k = Json::object();
  for (const char* f : {"transport", "command", "args", "cwd", "env", "url", "headers"}) k.set(f, cfg[f]);
  return k.dump();
}

std::string now_iso() {
  SYSTEMTIME t;
  GetLocalTime(&t);
  char b[32];
  snprintf(b, sizeof b, "%04d-%02d-%02dT%02d:%02d:%02d", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
  return b;
}

// 确认卡片上的提醒: 读屏会把屏幕上的文字发给哪家, 动手会动鼠标键盘
std::string provider_label(const Json& s) {
  const Json* persona = find_by(s["personas"], "id", s.get_str("active"));
  std::string p = persona ? persona->get_str("provider", "local") : "local";
  std::string label = s["providers"][p].get_str("label");
  if (!label.empty()) return label;
  if (p == "deepseek") return "DeepSeek";
  if (p == "qwen") return "通义千问";
  if (p == "kimi") return "Kimi";
  return "云端模型";
}

}  // namespace

void ToolHub::refresh() {
  int gen;
  {
    std::lock_guard<std::mutex> lock(mu_);
    gen = ++generation_;
  }
  std::thread([this, gen] {
    Json s = settings::load();
    const auto policy = autonomy::fetch(app_.opt.server);
    const bool unrestricted = autonomy::enabled(policy);
    const Json guard_cfg = autonomy::guard_config(s["guard"], policy);
    const Json* persona = find_by(s["personas"], "id", s.get_str("active"));
    std::string provider = persona ? persona->get_str("provider", "local") : "local";
    bool cloud = !provider.empty() && provider != "local";
    // 要连的服务器: 当前预设勾选的、启用着的. 配置没变的沿用原来的连接
    std::map<std::string, std::shared_ptr<Server>> next;
    std::map<std::string, std::string> names;  // 服务器 id -> 现在的名字 (改名不用重连)
    std::map<std::string, std::string> slugs;  // 她自己装的: 工具名里用英文 id (中文名在工具名里只剩下划线), 说明里还是中文名
    bool stop_on_input = true;
    // 连一个服务器 (配置没变的沿用原来的连接)
    auto add = [&](const std::string& id, const std::string& name, const Json& cfg, const std::string& kind) {
      std::shared_ptr<Server> sp;
      {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = servers_.find(id);
        if (it != servers_.end() && it->second->key == conn_key(cfg) && it->second->connected) sp = it->second;
      }
      if (!sp) {  // 新的连接: 别的线程还看不到它, 不用锁
        sp = std::make_shared<Server>();
        sp->id = id;
        sp->key = conn_key(cfg);
        sp->kind = kind;
        sp->cfg = cfg;
        std::string error;
        sp->connected = sp->client.connect(cfg, error, guard::connect_timeout_ms(kind), guard::discover_timeout_ms(kind));
        Json listed = sp->connected ? sp->client.list_tools(error) : Json();
        sp->tools = listed.is_array() ? listed : Json::array();
        sp->error = error;
        if (!sp->connected) logf("工具: 连不上 MCP 服务器 %s: %s", name.c_str(), error.c_str());
      }
      names[sp->id] = name;
      next[sp->id] = sp;
    };
    if (cloud) {
      for (const Json& id : (*persona)["mcp"].items()) {
        const Json* srv = find_by(s["mcp"], "id", id.str());
        if (!srv || !(*srv)["enabled"].boolean(true)) continue;
        Json cfg = settings::mcp_server(s, id.str(), nullptr);
        if (cfg.get_str("transport") != "http") {
          auto cwd = cfg.get_str("cwd");
          cfg.set("cwd", utf8(full_path(cwd.empty() ? L"." : widen(cwd))));
        }
        std::string kind = srv->get_str("kind");
        if (cfg.has("args")) cfg.set("args", guard::args(kind, cfg["args"], guard::power_on(s["guard"])));  // 高风险档的 7 个按设置加上或去掉
        // 操控类服务器一定带上的环境变量 (关统计、聊天和密码软件不进元素树), 盖过面板里填的同名变量
        Json forced = guard::env(kind, guard_cfg);  // 先存下来: 直接遍历临时对象的成员会悬空
        for (const auto& kv : forced.members()) cfg.at("env").set(kv.first, kv.second);
        if (!kind.empty() && !(*srv)["stop_on_input"].boolean(true)) stop_on_input = false;
        add(id.str(), srv->get_str("name"), cfg, kind);
      }
      // 她自己写、自己装的 MCP (wm/selfmcp.py): 每个云端预设都带上; 权限照常 (只读的自动, 别的每次问)
      Json mine = settings::self_mcp_servers(s);  // 先存下来: 直接遍历临时对象的成员会悬空
      for (const Json& cfg : mine.items()) {
        if (next.count(cfg.get_str("id"))) continue;
        add(cfg.get_str("id"), cfg.get_str("name"), cfg, "");
        slugs[cfg.get_str("id")] = cfg.get_str("id");
      }
    }
    // 工具表: 禁用的、锁定的不交给模型
    std::map<std::string, Tool> tools;
    std::set<std::string> taken = {"load_skill", "add_self_restriction", "read_constraints"};
    Json declared = Json::array(), report = Json::array();
    bool control = false;
    for (auto& [id, sp] : next) {
      int count = 0;
      for (const Json& t : sp->tools.items()) {
        Tool tool; tool.generation = gen;
        tool.server_id = id;
        tool.server_name = names[id];
        tool.raw = t.get_str("name");
        tool.title = t.get_str("title", tool.raw);
        tool.read_only = t["annotations"]["readOnlyHint"].boolean();
        tool.kind = sp->kind;
        if (tool.kind == "windows-mcp") {
          if (tool.raw == "PowerShell") {
            // The installed adapter uses os.path.expanduser("~"), not Desktop.
            wchar_t home[32768];DWORD size = GetEnvironmentVariableW(L"USERPROFILE",home,32768);
            if (size && size < 32768) tool.workspace = utf8(home);
            for (const auto& kv : sp->cfg["env"].members())
              if (_stricmp(kv.first.c_str(), "USERPROFILE") == 0) tool.workspace = kv.second.str();
          } else if (tool.raw == "FileSystem") {
            wchar_t desktop[MAX_PATH];
            if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_DESKTOPDIRECTORY, nullptr, SHGFP_TYPE_CURRENT, desktop)))
              tool.workspace = utf8(desktop);
          } else if (tool.raw == "App") tool.workspace = sp->cfg.get_str("cwd");
        }
        tool.tier = guard::tier(sp->kind, tool.raw);
        if (tool.tier == guard::Tier::locked && unrestricted) {
          tool.requires_unrestricted = true;
          tool.tier = guard::Tier::power;
        }
        if (tool.raw.empty() || permission(tool, unrestricted) == "off") continue;
        tool.name = public_name(slugs.count(id) ? slugs[id] : names[id], tool.raw, taken);
        taken.insert(tool.name);
        tools[tool.name] = tool;
        std::string desc = t.get_str("description", tool.title);
        Json params = t["inputSchema"].is_object() ? t["inputSchema"] : Json::object();
        if (!params.has("type")) params.set("type", "object");
        params = guard::schema(sp->kind, tool.raw, params, unrestricted);
        Json d = Json::object();
        d.set("name", tool.name).set("description", "[" + names[id] + "] " + guard::hint(sp->kind, tool.raw, unrestricted) + desc).set("parameters", params);
        declared.push(d);
        count++;
        control = control || tool.control();
      }
      Json r = Json::object();
      r.set("name", names[id]).set("ok", sp->connected.load()).set("tools", count).set("error", sp->error);
      report.push(r);
    }
    // 桌宠自带的工具: 这个预设启用了的 (人设预设的 builtins; 没写 = 都启用)
    if (cloud) {
      Json decl = builtins::declare(s, *persona, unrestricted);  // 先存下来: 直接遍历临时对象的成员会悬空
      for (const Json& d : decl.items()) {
        Tool tool; tool.generation = gen;
        tool.builtin = true;
        tool.name = tool.raw = d.get_str("name");
        tool.title = builtins::title(tool.raw);
        tool.server_name = "桌宠";
        if (taken.count(tool.name)) continue;
        taken.insert(tool.name);
        tools[tool.name] = tool;
        declared.push(d);
      }
    }
    // 原生主聊天与工作台共用同一治理存储。模型只得到读取/追加，不能改模式或删规则。
    if (cloud) {
      declared.push(autonomy::declaration());
      Json schema = Json::object();
      schema.set("type", "object").set("properties", Json::object()).set("additionalProperties", false);
      Json d = Json::object();
      d.set("name", "read_constraints").set("description", "查看当前权限模式和主人/自己设下的约束；instruction 是规划时须遵守的行为约定。不能删除、停用约束或扩大权限。").set("parameters", schema);
      declared.push(d);
      for (const char* name : {"add_self_restriction", "read_constraints"}) {
        Tool tool; tool.generation = gen; tool.name = tool.raw = name; tool.builtin = true; tool.server_name = "权限与自我约束";
        tool.read_only = std::string(name) == "read_constraints"; tool.title = tool.read_only ? "查看约束" : "追加自我约束";
        tools[name] = tool;
      }
    }
    // load_skill: 当前预设勾选的 skill
    std::map<std::string, std::string> skills;
    if (cloud) {
      Json all = settings::scan_skills();
      for (const Json& want : (*persona)["skills"].items()) {
        const Json* sk = find_by(all, "name", want.str());
        if (sk) skills[want.str()] = sk->get_str("dir");
      }
      for (const std::string& want : settings::auto_skills(s, *persona)) {  // 操控电脑的预设自动带上操作要领 (随附 skill)
        const Json* sk = find_by(all, "name", want);
        if (sk) skills[want] = sk->get_str("dir");
      }
    }
    if (!skills.empty()) {
      Json names = Json::array();
      for (auto& kv : skills) names.push(kv.first);
      Json name = Json::object();
      name.set("type", "string").set("enum", names).set("description", "skill 的名字");
      Json props = Json::object();
      props.set("name", name);
      Json req = Json::array();
      req.push("name");
      Json params = Json::object();
      params.set("type", "object").set("properties", props).set("required", req);
      Json d = Json::object();
      d.set("name", "load_skill").set("description", "读取一个 skill 的全文 (某类事情具体该怎么做). 要用某个 skill 时先调用它.").set("parameters", params);
      declared.push(d);
    }
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (gen != generation_) return;  // 又有新的设置了, 这一份作废
      servers_ = next;  // 不再需要的连接: 最后一个引用 (可能是正在调的工具) 放下时关掉
      tools_ = tools;
      skills_ = skills;
      stop_on_input_ = stop_on_input;
    }
    policy_refresh_pending_ = false;
    declared_unrestricted_ = unrestricted;
    has_control_ = control;
    logf("工具: 当前预设%s, %zu 个 MCP 服务器, 交给模型 %zu 个工具 (含 load_skill: %s; 操控电脑: %s)",
         cloud ? "用云端模型" : "用本地模型 (不接工具)", next.size(), declared.items().size(), skills.empty() ? "否" : "是",
         control ? "有" : "没有");
    Json msg = Json::object();
    msg.set("t", "tools").set("tools", declared).set("servers", report).set("cloud", cloud).set("control", control);
    std::wstring w = widen(msg.dump());
    app_.ui([this, w, control] {
      app_.web.post_json(w);
      app_.set_stop_hotkey(control);
    });
  }).detach();
}

std::string ToolHub::permission(const Tool& tool, bool unrestricted) {
  if ((tool.tier == guard::Tier::locked || tool.requires_unrestricted) && !unrestricted) return "off";
  Json s = settings::load();
  const Json* persona = find_by(s["personas"], "id", s.get_str("active"));
  if (!persona || persona->get_str("provider", "local") == "local") return "off";
  if (tool.builtin) {
    if (tool.raw == "add_self_restriction" || tool.raw == "read_constraints") return "auto";
    if ((*persona)["builtins"].is_array()) {
      bool selected = false;
      for (const auto& g : (*persona)["builtins"].items()) selected = selected || g.str() == builtins::group(tool.raw);
      if (!selected) return "off";
    }
    auto p = builtins::permission(s, tool.raw);
    return p == "off" ? p : unrestricted ? "auto" : p;
  }
  if (tool.name == "load_skill") {
    for (const auto& name : (*persona)["skills"].items()) if (name.str() == tool.title) return "auto";
    for (const auto& name : settings::auto_skills(s, *persona)) if (name == tool.title) return "auto";
    return "off";
  }
  if (tool.power() && !guard::power_on(s["guard"])) return "off";
  const Json* srv = find_by(s["mcp"], "id", tool.server_id);
  if (srv) {
    bool selected = false;
    for (const auto& id : (*persona)["mcp"].items()) selected = selected || id.str() == tool.server_id;
    if (!selected || !(*srv)["enabled"].boolean(true)) return "off";
  } else {
    Json mine = settings::self_mcp_servers(s);
    if (!find_by(mine, "id", tool.server_id)) return "off";
  }
  std::string p = srv ? (*srv)["tools"].get_str(tool.raw) : "";
  if (p == "off") return "off";
  if (unrestricted) return "auto";
  if (tool.control()) return "ask";
  if (tool.tier == guard::Tier::read) return p == "ask" ? p : "auto";
  if (p == "auto" || p == "ask") return p;
  return tool.read_only ? "auto" : "ask";
}

autonomy::Call ToolHub::policy_call(const Tool& tool, const Json& call) {
  autonomy::Call request;
  request.aliases = {tool.raw, tool.name, tool.builtin || tool.name == "load_skill" ? "builtin." + tool.raw : "mcp." + tool.server_id + "." + tool.raw};
  request.args = call["arguments"];
  if (tool.name == "load_skill" || tool.raw == "read_constraints") request.operation = "read";
  if (tool.builtin && tool.raw != "read_constraints" && tool.raw != "room_capabilities") request.operation = "write";
  if (tool.kind == "windows-mcp") {
    auto mode = request.args.get_str("mode");
    if (tool.raw == "FileSystem") {
      request.recursive = mode == "list" || mode == "search" || mode == "copy" || mode == "move" || request.args["recursive"].boolean();
      if (mode == "read" || mode == "list" || mode == "search" || mode == "info") request.operation = "read";
      else if (mode == "write" || mode == "delete" || mode == "create") request.operation = "write";
      // Copy/move read a source and write a destination. Until per-argument
      // operations exist, apply both scopes to every explicit path, including
      // descendants of directory operands; never classify these as write-only.
      if (request.operation == "unknown") {
        request.aliases.push_back("read_file");
        request.aliases.push_back("write_file");
      } else request.aliases.push_back(request.operation == "read" ? (request.recursive ? "glob" : "read_file") : "write_file");
    } else if (tool.raw == "PowerShell") request.aliases.push_back("run_command");
    else if (tool.raw == "Scrape") { request.aliases.push_back("web_fetch"); request.operation = "read"; }
    if (tool.raw == "App" && mode == "launch_executable") {
      std::string command = request.args.get_str("executable");
      if (request.args["args"].is_string()) command += " " + request.args["args"].str();
      else for (const auto& arg : request.args["args"].items()) if (arg.is_string()) command += " " + arg.str();
      request.args.set("command", command);
    }
    if ((tool.raw == "App" || tool.raw == "Process") && request.args.has("name"))
      request.args.set("process_name", request.args["name"]);
  }
  // MCP implementations may choose their own cwd. Unknown stays unknown; matcher
  // rejects relative path access when a path rule cannot be resolved reliably.
  request.workspace = request.args.get_str("cwd", request.args.get_str("workdir"));
  if (tool.kind == "windows-mcp") {
    if (tool.raw == "PowerShell") {
      request.workspace = tool.workspace;
      request.args.set("cwd", tool.workspace);  // tool has no caller-supplied cwd parameter
      request.args.erase("workdir");
    } else if (tool.raw == "FileSystem") {
      request.workspace = tool.workspace;
      request.args.erase("cwd");request.args.erase("workdir");
    } else if (tool.raw == "App") {
      if (request.workspace.empty()) request.workspace = tool.workspace;
      else {
        auto requested = std::filesystem::path(widen(request.workspace));
        if (requested.is_relative() && !tool.workspace.empty()) requested = std::filesystem::path(widen(tool.workspace)) / requested;
        request.workspace = utf8(full_path(requested.wstring()));
      }
    }
  }
  return request;
}

std::string ToolHub::dispatch_error(const Tool& tool, const Json& call, const std::string& how, bool gui, autonomy::Policy* snapshot) {
  const auto policy = autonomy::fetch(app_.opt.server);
  if (auto why = autonomy::deny(policy, policy_call(tool, call)); !why.empty()) return why;
  if (autonomy::enabled(policy) != declared_unrestricted_.load()) {
    if (!policy_refresh_pending_.exchange(true)) refresh();
    return "权限模式已改变，正在刷新工具连接，请稍后重新发起调用";
  }
  const auto perm = permission(tool, autonomy::enabled(policy));
  if (perm == "off") return "当前预设或设置已禁用这个工具";
  if (how == "auto" && perm != "auto") return "自动授权已改变，请重新发起调用并确认";
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (stopped_.count(call.get_str("call_id")) || (tool.control() && call["turn"].dump() == halted_turn_)) return "用户已停止这次调用";
    if (how == "turn" && perm != "auto") {
      const auto turn = call["turn"].dump();
      const auto now = GetTickCount64();
      const auto grant = grants_.find(tool.server_id);
      const bool granted = grant != grants_.end() && grant->second.turn == turn && now < grant->second.until;
      const bool tasked = task_active_locked(now) && task_.turn == turn &&
          (task_.by == "owner" || tool.tier == guard::Tier::screen);
      if (!tool.control() || tool.power() || (!granted && !tasked)) return "本轮授权已结束，请重新确认";
    }
    if (tool.generation != generation_) return "设置或工具连接正在更新，请稍后重新发起调用";
    if (tool.name != "load_skill") {
      auto it = tools_.find(tool.name);
      if (it == tools_.end() || it->second.raw != tool.raw || it->second.server_id != tool.server_id) return "工具表已经更新，请重新发起调用";
    }
  }
  if (gui && tool.control()) {
    const auto extra = autonomy::guard_config(settings::load()["guard"], policy);
    auto v = guard::check(tool.kind, tool.raw, call["arguments"], extra);
    if (!v.ok) return v.why;
  }
  if (snapshot) *snapshot = policy;
  return "";
}

bool ToolHub::dispatch_ok(const Tool& tool, const Json& call, const std::string& how, bool gui, autonomy::Policy* snapshot) {
  auto why = dispatch_error(tool, call, how, gui, snapshot);
  if (why.empty()) return true;
  const auto id = call.get_str("call_id");
  { std::lock_guard<std::mutex> lock(mu_); control_calls_.erase(id); }
  reply(id, "没有执行：" + why, true);
  audit(call, tool, "policy", false, 0, 0, why);
  Json m = Json::object();m.set("t", "tool-blocked").set("call_id", id).set("text", why);post(m);
  return false;
}

void ToolHub::on_call(const Json& call) {
  std::string name = call.get_str("name"), id = call.get_str("call_id");
  { std::lock_guard<std::mutex> lock(mu_); in_flight_.insert(id); }
  if (name == "load_skill") {
    std::thread([this, call] { skill(call); }).detach();
    return;
  }
  Tool tool;
  bool found = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = tools_.find(name);
    if ((found = it != tools_.end())) tool = it->second;
    if (found && tool.control()) control_calls_.insert(id);
  }
  if (!found) {
    tool.name = name;
    reply(id, "桌宠这边没有这个工具 (可能刚改了设置)", true);
    audit(call, tool, "unknown", false, 0, 0, "没有这个工具");
    return;
  }
  std::thread([this, tool, call] { dispatch(tool, call); }).detach();
}

void ToolHub::dispatch(Tool tool, Json call) {
  const auto id = call.get_str("call_id"), name = tool.name;
  if (!dispatch_ok(tool, call, "", false)) return;
  if (tool.control()) {  // 守门、UI 自动化可能要等别的程序回话, 不在 UI 线程上做
    std::thread([this, tool, call] { control_call(tool, call); }).detach();
    return;
  }
  if (tool.builtin && tool.raw == "open_url" && !autonomy::enabled(autonomy::fetch(app_.opt.server))) {  // 网址先查: 本机地址、付款页面这类不用问用户, 直接拦
    std::string why = builtins::check_url(call["arguments"].get_str("url"));
    if (!why.empty()) {
      reply(id, "这一步要用户自己来: " + why + ". 没有打开", true);
      audit(call, tool, "guard", false, 0, 0, why);
      return;
    }
  }
  std::string perm = permission(tool, autonomy::enabled(autonomy::fetch(app_.opt.server)));
  if (perm == "off") {
    reply(id, "用户在设置里禁用了这个工具", true);
    audit(call, tool, "off", false, 0, 0, "禁用");
  } else if (perm == "auto") {
    std::thread([this, tool, call] { run(tool, call, "auto"); }).detach();
  } else {
    {
      std::lock_guard<std::mutex> lock(mu_);
      asks_[id] = Ask{call, tool};
    }
    Json ask = Json::object();
    ask.set("t", "tool-ask").set("call_id", id).set("server", tool.server_name).set("tool", tool.raw).set("title", tool.title);
    ask.set("args", preview(call["arguments"], 200));
    if (tool.builtin) ask.set("options", builtins::options(tool.raw));  // 网址没有"总是允许"
    post(ask);
    logf("工具: 问用户要不要调 %s", name.c_str());
    ask_shot();
  }
}

void ToolHub::ask_shot() {
  if (app_.opt.ask_shot.empty() || shot_taken_.exchange(true)) return;  // 自检: 第一次弹确认框时截一张页面 (看卡片的样子)
  std::thread([this] {
    Sleep(600);
    app_.ui([this] { app_.web.capture_png(app_.opt.ask_shot, [](bool ok) { logf("确认框截图%s", ok ? "好了" : "失败"); }); });
  }).detach();
}

void ToolHub::control_call(Tool tool, Json call) {
  std::string id = call.get_str("call_id"), turn = call["turn"].dump();
  std::string refuse;
  bool granted = false;
  const bool unrestricted = autonomy::enabled(autonomy::fetch(app_.opt.server));
  {
    std::lock_guard<std::mutex> lock(mu_);
    ULONGLONG now = GetTickCount64();
    if (turn != last_turn_) {  // 新的一轮: 上一轮的"这一轮都允许"作废
      for (auto it = grants_.begin(); it != grants_.end();) it = it->second.turn == turn ? std::next(it) : grants_.erase(it);
      last_turn_ = turn;
    }
    bool tasked = false;  // 这一轮是渠道任务单的那一轮
    if (task_active_locked(now)) {
      if (task_.turn.empty()) task_.turn = turn;  // task_begin 之后的第一轮就是任务的那一轮
      tasked = task_.turn == turn;
    }
    if (turn_use_.size() > 50) turn_use_.clear();
    auto& use = turn_use_[turn];
    if (!use.second) use.second = now;
    int max_calls = tasked ? kTaskControlCalls : kMaxControlCalls;
    ULONGLONG max_ms = tasked ? kTaskControlMs : kMaxControlMs;
    if (turn == halted_turn_) refuse = "用户刚才喊停了, 这一轮不再操作电脑";
    else if (!unrestricted && (++use.first > max_calls || now - use.second > max_ms))
      refuse = tasked ? "这件事操作电脑的次数或时间到上限了 (80 次 / 15 分钟), 先停下, 把做到哪了告诉主人"
                      : "这一轮操作电脑的次数或时间到上限了 (30 次 / 5 分钟), 先停下来问问用户要不要接着";
    auto g = grants_.find(tool.server_id);
    granted = g != grants_.end() && g->second.turn == turn && now < g->second.until;
    // 主人在 QQ 上亲口说的事: 读屏、动手都算同意过了; 她自己想看的: 只有读屏算. 守门 (guard_ok) 在下面照旧先查
    if (tasked && (task_.by == "owner" || tool.tier == guard::Tier::screen)) granted = true;
    if (tool.power()) granted = false;  // 高风险档每次都问, 不吃"这一轮都允许", 也不吃 QQ 任务单的"主人说了就算"
    if (!refuse.empty()) control_calls_.erase(id);
  }
  if (!refuse.empty()) {
    reply(id, refuse, true);
    audit(call, tool, "halted", false, 0, 0, refuse);
    return;
  }
  if (permission(tool, autonomy::enabled(autonomy::fetch(app_.opt.server))) == "off") {
    { std::lock_guard<std::mutex> lock(mu_); control_calls_.erase(id); }
    reply(id, "用户在设置里禁用了这个工具", true);
    audit(call, tool, "off", false, 0, 0, "禁用");
    return;
  }
  if (!dispatch_ok(tool, call, "", true)) return;
  const bool automatic = permission(tool, autonomy::enabled(autonomy::fetch(app_.opt.server))) == "auto";
  if (granted || automatic) {
    run(tool, call, automatic ? "auto" : "turn", true);
    return;
  }
  {
    std::lock_guard<std::mutex> lock(mu_);
    asks_[id] = Ask{call, tool};
  }
  Json ask = Json::object();
  ask.set("t", "tool-ask").set("call_id", id).set("server", tool.server_name).set("tool", tool.raw).set("title", tool.title);
  std::string shown = tool.raw == "PowerShell" ? call["arguments"].get_str("command") : "";  // 命令原文 (换行、引号照原样), 不是 JSON 转义过的
  if (shown.size() > 1600) shown = shown.substr(0, 1600) + "…";
  ask.set("args", shown.empty() ? preview(call["arguments"], tool.power() ? 1600 : 200) : shown)
      .set("tier", guard::tier_name(tool.tier)).set("turn_ok", !tool.power());
  if (tool.power()) {  // 高风险: 只有"允许一次 / 拒绝", 看清楚上面写的再点; 结果会发给云端模型
    Json opts = Json::array();
    opts.push("once").push("deny");
    ask.set("options", opts).set("note", "这是高风险的工具, 只允许这一次, 先看清楚上面要做的事. 结果 (命令输出、文件内容) 会发给 " +
                                         provider_label(settings::load()) + ". 不放心就点拒绝");
  } else {
    ask.set("note", tool.tier == guard::Tier::screen ? "会把屏幕上的文字 (窗口名、按钮、看得到的内容) 发给 " + provider_label(settings::load()) + "; 截图不发"
                                                      : "她会动你的鼠标键盘. 你一动鼠标键盘、按 Ctrl+Alt+Shift+Backspace 或点「停」, 她就停");
  }
  post(ask);
  logf("工具: 问用户要不要让她操作电脑 (%s)", tool.name.c_str());
  ask_shot();
}

void ToolHub::on_answer(const std::string& id, const std::string& answer) {
  Ask a;
  std::string how = answer;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = asks_.find(id);
    if (it == asks_.end()) return;
    a = it->second;
    asks_.erase(it);
    if (a.tool.power() && (how == "turn" || how == "always")) how = "once";  // 高风险档只有"允许一次" (页面不给别的按钮, 这里再兜一道)
    if (how != "once" && how != "always" && how != "turn") control_calls_.erase(id);
  }
  std::thread([this, a, how] { finish_answer(a, how); }).detach();
}

void ToolHub::finish_answer(Ask a, std::string how) {
  const auto id = a.call.get_str("call_id");
  if (how == "once" || how == "always" || how == "turn") {
    if (!dispatch_ok(a.tool, a.call, how == "turn" ? "once" : how, false)) return;
    if (how == "turn" && a.tool.control()) {
      std::lock_guard<std::mutex> lock(mu_);
      grants_[a.tool.server_id] = Grant{a.call["turn"].dump(), GetTickCount64() + kGrantMs};
    }
    if (how == "always") {
      // Serialize preference updates with panel events on the UI thread. Recheck
      // disabled settings after network preflight, so an old card cannot restore off.
      app_.ui([this, a] { persist_answer(a); });
      return;
    }
    if (how == "turn") {  // 到点了提示条要收起来
      update_control();
      std::thread([this] {
        Sleep(DWORD(kGrantMs + 200));
        update_control();
      }).detach();
    }
    std::thread([this, a, how] { run(a.tool, a.call, how == "always" && a.tool.control() ? "once" : how); }).detach();
    return;
  }
  std::string why = how == "timeout" ? "用户没有及时确认, 没有调用"
                    : how == "halt"  ? "用户喊停了, 没有调用"
                                     : "用户拒绝了这次调用";
  reply(id, why, true);
  audit(a.call, a.tool, how == "timeout" ? "timeout" : how == "halt" ? "halted" : "deny", false, 0, 0, why);
}

void ToolHub::persist_answer(Ask a) {
  const auto id = a.call.get_str("call_id");
  bool stale;
  { std::lock_guard<std::mutex> lock(mu_); stale = a.tool.generation != generation_ || stopped_.count(id); }
  if (stale || permission(a.tool, false) == "off") {
    reply(id, "设置已改变或调用已停止，旧确认没有保存", true);
    audit(a.call, a.tool, "policy", false, 0, 0, "旧确认失效");return;
  }
  Json s = settings::load();bool changed = false;
  if (a.tool.builtin && builtins::group(a.tool.raw) != "open_url") {
    s.at("builtins").set(builtins::group(a.tool.raw), "auto");changed = true;
  } else if (!a.tool.control() && !a.tool.builtin) {
    for (auto& srv : s.at("mcp").mut_items()) if (srv.get_str("id") == a.tool.server_id) {
      if (!srv["tools"].is_object()) srv.set("tools", Json::object());
      srv.at("tools").set(a.tool.raw, "auto");changed = true;
    }
  }
  if (changed && !settings::save(s)) {
    reply(id, "没有保存授权：设置写入失败", true);audit(a.call,a.tool,"policy",false,0,0,"设置写入失败");return;
  }
  std::thread([this,a] { run(a.tool,a.call,"always"); }).detach();
}

void ToolHub::on_cancel(const std::string& id) {
  Ask a;
  bool asking = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = asks_.find(id);
    if ((asking = it != asks_.end())) {
      a = it->second;
      asks_.erase(it);
      control_calls_.erase(id);
      in_flight_.erase(id);
    }
  }
  if (!asking) {
    stop(id, "server");
    return;
  }
  Json m = Json::object();
  m.set("t", "tool-ask-cancel").set("call_id", id);
  app_.web.post_json(widen(m.dump()));
  audit(a.call, a.tool, "cancelled", false, 0, 0, "服务端不等了");
}

void ToolHub::on_stop(const std::string& id) {
  logf("工具: 用户在对话栏里停掉 %s", id.c_str());
  bool asking, control;
  {
    std::lock_guard<std::mutex> lock(mu_);
    asking = asks_.count(id) > 0;
    control = control_calls_.count(id) > 0;
  }
  if (control) {  // 操控电脑时的"停": 停整件事, 不只是这一下
    emergency_stop("你在对话栏里点了停");
    return;
  }
  if (asking) {  // 确认卡片还没弹出来就点了"停": 当拒绝
    Json m = Json::object();
    m.set("t", "tool-ask-cancel").set("call_id", id);
    app_.web.post_json(widen(m.dump()));
    on_answer(id, "deny");
    return;
  }
  stop(id, "user");
}

void ToolHub::task_begin(const std::string& id, const std::string& by) {
  if (id.empty() || id.size() > 40) return;
  {
    std::lock_guard<std::mutex> lock(mu_);
    task_ = Task{};
    task_.id = id;
    task_.by = by == "owner" ? "owner" : "self";
    task_.until = GetTickCount64() + kTaskMs;
  }
  logf("工具: 渠道任务单 %s 开始 (%s)", id.c_str(), by == "owner" ? "主人说了就算同意" : "她自己想看, 只放行读屏");
  update_control();
  std::thread([this] {  // 到点了提示条要收起来
    Sleep(DWORD(kTaskMs + 200));
    update_control();
  }).detach();
}

void ToolHub::task_end(const std::string& id) {
  Task t;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (task_.id != id) return;
    t = std::move(task_);
    task_ = Task{};
  }
  logf("工具: 渠道任务单 %s 结束 (%s)", id.c_str(), t.shot.empty() ? (t.skipped.empty() ? "没有截图" : "截图没留: 屏幕上有不该发出去的窗口") : "留了一张截图");
  update_control();
  Json m = Json::object();
  m.set("t", "task-shot").set("id", id).set("mime", t.mime).set("data", t.shot).set("skipped", t.skipped);
  post(m);
}

// 渠道任务单里 Windows-MCP 返回的截图: 暂存最后一张 (只有主人的单子, 且当时屏幕上没有聊天、邮件、密码、付款、机器人后台这类窗口).
// 不交给模型 (run 里照旧换成"只留在本机"); 只在 task_end 时回给页面, 页面压成小图发到主人自己的 QQ 上.
void ToolHub::stash_shot(const Json& call, const Json& image) {
  std::string turn = call["turn"].dump();
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (!task_active_locked(GetTickCount64()) || task_.by != "owner" || task_.turn != turn) return;
  }
  Json guard_cfg = autonomy::guard_config(settings::load()["guard"], autonomy::fetch(app_.opt.server));
  std::string who;
  const char* why = !guard_cfg["task_shot"].boolean(true) ? "设置里关了任务截图"
                    : app_.opt.task_shot_any             ? nullptr  // 自检专用 (--task-shot-any)
                                                         : guard::screen_private(guard_cfg, &who);
  if (why) logf("工具: 任务截图不留: %s (%s)", why, who.empty() ? "-" : who.c_str());
  std::lock_guard<std::mutex> lock(mu_);
  if (why) {
    task_.shot.clear();
    task_.skipped = why;
  } else {
    task_.shot = image.get_str("data");
    task_.mime = image.get_str("mimeType", "image/png");
    task_.skipped.clear();
  }
}

void ToolHub::on_turn_done() {
  bool had;
  {
    std::lock_guard<std::mutex> lock(mu_);
    had = !grants_.empty();
    grants_.clear();
  }
  if (had) update_control();
}

void ToolHub::emergency_stop(const std::string& why) {
  std::vector<std::string> asking, running;
  bool active;
  {
    std::lock_guard<std::mutex> lock(mu_);
    for (auto& [id, a] : asks_)
      if (a.tool.control()) asking.push_back(id);
    for (const std::string& id : control_calls_) {
      // Also stop calls still fetching policy or waiting before run(). Their
      // turn may not have reached control_call(), so last_turn_ is insufficient.
      stopped_[id] = "halt:" + why;
      if (running_.count(id)) running.push_back(id);
    }
    active = control_on_ || !control_calls_.empty() || !grants_.empty();
    grants_.clear();
    task_.until = 0;  // 渠道任务单的授权也作废 (单子留着, 页面 task_end 时还能取回已经暂存的截图)
    if (active) halted_turn_ = last_turn_;
  }
  if (!active) return;  // 没在操作电脑: 快捷键按着玩、真人输入, 都不用管
  logf("工具: 一键停 (%s), 在问的 %zu 个, 在调的 %zu 个", why.c_str(), asking.size(), running.size());
  for (const std::string& id : asking) {
    Json m = Json::object();
    m.set("t", "tool-ask-cancel").set("call_id", id);
    post(m);
    on_answer(id, "halt");
  }
  for (const std::string& id : running) stop(id, "halt:" + why);
  Json m = Json::object();
  m.set("t", "control-stop").set("why", why);
  post(m);
  update_control();
}

void ToolHub::disconnect() {
  emergency_stop("断开了电脑操控");
  Json s = settings::load();
  int n = 0;
  for (Json& srv : s.at("mcp").mut_items())
    if (!srv.get_str("kind").empty() && srv["enabled"].boolean(true)) {
      srv.set("enabled", false);
      n++;
    }
  if (n) settings::save(s);
  logf("工具: 断开电脑操控, 停用了 %d 个操控类服务器", n);
}

// 记下谁停的 (server | user | halt:<一键停的原因>); 正在这个服务器上调的就是它, 马上让 MCP 客户端放弃等待 (还在排队等服务器空出来的, 轮到时直接不调)
void ToolHub::stop(const std::string& id, const std::string& who) {
  bool room = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = running_.find(id);
    if (it == running_.end() && !in_flight_.count(id) && !pending_.count(id)) return;
    stopped_.emplace(id, who);
    auto pending = pending_.find(id);
    room = pending != pending_.end() && pending->second.tool.raw == "room_do";
    if (it != running_.end() && it->second && it->second->current == id) it->second->client.cancel();
  }
  if (room) {
    Json msg = Json::object();
    msg.set("t", "room-action-cancel").set("call_id", id).set("reason", who);
    post(msg);
    on_builtin_done(id, false, "已取消等待小屋活动完成");
  }
}

void ToolHub::run(Tool tool, Json call, std::string how, bool checked) {
  (void)checked;  // Permission snapshots never survive a queue or approval wait.
  if (!dispatch_ok(tool, call, how, false)) return;
  if (tool.builtin) {
    builtin_run(tool, call, how);
    return;
  }
  ULONGLONG t0 = GetTickCount64();
  std::string id = call.get_str("call_id"), error;
  bool control = tool.control();
  if (control) {
    bool halted;
    {
      std::lock_guard<std::mutex> lock(mu_);
      halted = call["turn"].dump() == halted_turn_;
      if (halted) control_calls_.erase(id);
    }
    if (halted) {  // 等用户确认的时候喊停了
      reply(id, "用户喊停了, 没有调用", true);
      audit(call, tool, "halted", false, 0, 0, "用户喊停了");
      return;
    }
    if (!dispatch_ok(tool, call, how, true)) return;
  }
  std::shared_ptr<Server> sp;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = servers_.find(tool.server_id);
    if (it != servers_.end()) sp = it->second;
    running_[id] = sp;
    if (control) control_running_++;
  }
  if (control) update_control();
  Json result;
  if (!sp) {
    error = "这个 MCP 服务器已经不在当前预设里了";
  } else {
    std::lock_guard<std::mutex> l(sp->mu);
    bool skip;
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (!(skip = stopped_.count(id) > 0)) {
        sp->client.clear_cancel();
        sp->current = id;
      }
    }
    // 每个服务器的超时可以在面板里改 (5 到 600 秒, 默认 60); 服务端等 90 秒, 超过的话它会先发 tool_cancel
    int timeout_ms = int(std::clamp(sp->cfg["timeout_s"].number(60), 5.0, 600.0) * 1000);
    if (skip) error = "还没开始调就停掉了";
    else if (!sp->connected) sp->connected = sp->client.connect(sp->cfg, error, guard::connect_timeout_ms(sp->kind), guard::discover_timeout_ms(sp->kind));
    auto invoke = [&] {
      error = dispatch_error(tool, call, how, control);
      if (error.empty()) {
        std::lock_guard<std::mutex> lock(mu_);
        auto current = servers_.find(tool.server_id);
        if (current == servers_.end() || current->second != sp) error = "服务器配置已更新，请重新发起调用";
      }
      if (!error.empty()) { result = Json(); return false; }
      result = sp->client.call_tool(tool.raw, call["arguments"], error, timeout_ms);
      return true;
    };
    if (!skip && sp->connected) {
      invoke();
      // Windows-MCP 开始前就拒了 (控制权暂时不可用、你刚动过鼠标): 什么都没做, 等一会儿再试 (等的时候点停、按快捷键马上打断)
      for (int i = 0, wait; i < 2 && (wait = guard::busy_wait_ms(sp->kind, result)) > 0; i++) {
        bool halted = false;
        for (ULONGLONG until = GetTickCount64() + wait; !halted && GetTickCount64() < until;) {
          Sleep(100);
          std::lock_guard<std::mutex> lock(mu_);
          halted = stopped_.count(id) > 0;
        }
        if (halted) break;
        if (!invoke()) break;
      }
      // 停掉的、超时的不算断线, 连接接着用; 真断了 (进程退出) 才重连
      auto dropped = [&] { return result.is_null() && !sp->client.cancelled() && !sp->client.alive(); };
      // 连接断了: 只读的工具重连再试一次. 有副作用的不重试, 免得第一次其实做成了、又做一遍
      if (dropped() && tool.read_only && !control) {
        sp->connected = sp->client.connect(sp->cfg, error, guard::connect_timeout_ms(sp->kind), guard::discover_timeout_ms(sp->kind));
        if (sp->connected) invoke();
      }
      if (dropped()) sp->connected = false;
    }
    std::lock_guard<std::mutex> lock(mu_);
    sp->current.clear();
  }
  std::string stopped_by;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = stopped_.find(id);
    if (it != stopped_.end()) {
      stopped_by = it->second;
      stopped_.erase(it);
    }
    running_.erase(id);
    in_flight_.erase(id);
    if (control) {
      control_running_--;
      control_calls_.erase(id);
    }
  }
  if (control) update_control();
  double ms = double(GetTickCount64() - t0);
  if (result.is_null()) {
    if (stopped_by == "user") error = "用户在对话栏里停掉了这次调用";
    else if (stopped_by.rfind("halt:", 0) == 0) error = "用户喊停了 (" + stopped_by.substr(5) + ")";
    else if (stopped_by == "server") error = "服务端不等了, 停掉了这次调用";
    if (stopped_by != "server") reply(id, stopped_by.empty() ? "工具调用失败: " + error : error, true);
    audit(call, tool, how, false, 0, ms, error);
    return;
  }
  bool cancelled = stopped_by == "server";  // 调完了, 但服务端已经不等了: 结果不用发
  Json raw = result["content"], content = Json::array();
  if (!raw.is_array() || raw.items().empty()) {  // 只给了结构化结果: 转成一段文字
    Json part = Json::object();
    part.set("type", "text").set("text", result.has("structuredContent") ? result["structuredContent"].dump() : "(没有内容)");
    raw = Json::array();
    raw.push(part);
  }
  // 图片 (截图) 只留在本机, 不交给服务端; 操控类服务器的文字里, 聊天和密码软件的窗口标题换成占位
  Json extra = tool.kind.empty() ? Json() : autonomy::guard_config(settings::load()["guard"], autonomy::fetch(app_.opt.server));
  int images = 0;
  for (const Json& c : raw.items()) {
    if (c.get_str("type") == "image") {
      if (images++) continue;
      if (control) stash_shot(call, c);
      Json part = Json::object();
      part.set("type", "text").set("text", "[截图只留在本机, 没有发给模型]");
      content.push(part);
    } else if (c.get_str("type") == "text" && !tool.kind.empty()) {
      Json part = c;
      part.set("text", guard::redact(c.get_str("text"), extra));
      content.push(part);
    } else {
      content.push(c);
    }
  }
  bool is_error = result["isError"].boolean();
  if (!cancelled) reply(id, content, is_error);
  std::string first = content.items()[0].get_str("text");
  audit(call, tool, how, !is_error, content.dump().size(), ms, is_error ? first.substr(0, 200) : images ? "截图 " + std::to_string(images) + " 张留在本机" : "");
}

void ToolHub::builtin_run(Tool tool, Json call, std::string how) {
  ULONGLONG t0 = GetTickCount64();
  std::string id = call.get_str("call_id");
  autonomy::Policy policy;
  if (!dispatch_ok(tool, call, how, false, &policy)) return;
  if (tool.raw == "read_constraints" || tool.raw == "add_self_restriction") {
    std::string text;
    bool ok = true;
    if (tool.raw == "read_constraints") {
      const auto& p = policy;Json data = Json::object();
      data.set("global_unrestricted", p.unrestricted).set("revision", double(p.revision)).set("rules", p.rules).set("error", p.error);
      ok = p.error.empty(); text = data.dump();
    } else {
      ok = autonomy::add_self(app_.opt.server, call["arguments"], text);
      if (ok) refresh();
    }
    reply(id, text, !ok);audit(call, tool, how, ok, text.size(), double(GetTickCount64()-t0), ok ? "" : text);
    return;
  }
  if (tool.raw == "room_do" || tool.raw == "room_capabilities") {  // Wait for terminal result, not queue acknowledgement.
    const bool query = tool.raw == "room_capabilities";
    const int timeout_ms = query ? 8000 : builtins::room_timeout_ms(call["arguments"]);
    {
      std::lock_guard<std::mutex> lock(mu_);
      pending_[id] = Pending{call, tool, how, t0};
    }
    Json m = Json::object();
    m.set("t", query ? "room-capabilities" : "room-do").set("call_id", id)
        .set("act", call["arguments"].get_str("act")).set("timeout_ms", timeout_ms);
    app_.ui([this, m, id, t0] {
      bool stopped;
      {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = pending_.find(id);
        if (it == pending_.end() || it->second.t0 != t0) return;  // Cancelled/settled before UI delivery, or reused id.
        stopped = stopped_.count(id) > 0;
      }
      if (stopped) on_builtin_done(id, false, "已停止，未向小屋发送动作");
      else app_.web.post_json(widen(m.dump()));
    });
    std::thread([this, id, timeout_ms, query, t0] {
      const ULONGLONG until = GetTickCount64() + timeout_ms;
      for (;;) {
        { std::lock_guard<std::mutex> lock(mu_); if (!pending_.count(id) || pending_.at(id).t0 != t0) return; }
        if (GetTickCount64() >= until) break;
        Sleep(100);
      }
      if (!query) {
        Json cancel = Json::object();
        cancel.set("t", "room-action-cancel").set("call_id", id).set("reason", "timeout");
        post(cancel);
      }
      on_builtin_done(id, false, query ? "页面未回复当前小屋能力" : "等待超时，未收到小屋执行完成回执；已请求取消，不能当作成功");
    }).detach();
    return;
  }
  std::string text;
  bool ok = builtins::run(tool.raw, call["arguments"], app_.opt.dry_open, text, autonomy::enabled(policy));
  reply(id, text, !ok);
  audit(call, tool, how, ok, text.size(), double(GetTickCount64() - t0), ok ? "" : text);
}

void ToolHub::on_builtin_done(const std::string& id, bool ok, const std::string& text) {
  Pending p;
  bool stopped = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = pending_.find(id);
    if (it == pending_.end()) return;  // 已经回过了 (超时之后页面才回, 或者反过来)
    p = it->second;
    pending_.erase(it);
    stopped = stopped_.count(id) > 0;
  }
  if (stopped) {
    const auto message = "已收到停止请求；若动作已经开始，以小屋当前状态为准。反馈：" + text;
    reply(id, message, true);
    audit(p.call, p.tool, "halted", false, message.size(), double(GetTickCount64() - p.t0), message);
    return;
  }
  if (p.tool.raw == "room_do") ok = builtins::room_result_ok(ok, text);
  reply(id, text, !ok);
  audit(p.call, p.tool, p.how, ok, text.size(), double(GetTickCount64() - p.t0), ok ? "" : text);
}

void ToolHub::start() {
  // 提醒到点: 告诉页面 (她在对话栏里说、挥挥手, 小屋里过来看你). 提醒的文字可能是私事, 日志里只记编号
  builtins::reminders().start([this](const Json& r, double late) {
    Json m = Json::object();
    m.set("t", "remind").set("id", r.get_str("id")).set("text", r.get_str("text")).set("late", late);
    post(m);
  });
}

void ToolHub::skill(Json call) {
  ULONGLONG t0 = GetTickCount64();
  std::string id = call.get_str("call_id"), name = call["arguments"].get_str("name");
  std::string dir;
  int generation;
  {
    std::lock_guard<std::mutex> lock(mu_);
    generation = generation_;
    auto it = skills_.find(name);
    if (it != skills_.end()) dir = it->second;
  }
  Tool tool;
  tool.name = "load_skill";
  tool.title = name; tool.generation = generation;
  tool.server_name = "(skill)";
  tool.raw = "load_skill";
  tool.read_only = true;
  if (!dir.empty()) {
    Json check = call;
    check.at("arguments").set("path", dir + "\\SKILL.md");
    if (!dispatch_ok(tool, check, "auto", false)) return;
  }
  std::vector<char> data;
  // Disk/network I/O must not hold mu_: stop and settings changes remain responsive.
  bool loaded = !dir.empty() && read_file(widen(dir) + L"\\SKILL.md", data);
  {
    std::lock_guard<std::mutex> lock(mu_);
    const auto it = skills_.find(name);
    loaded = loaded && tool.generation == generation_ && it != skills_.end() && it->second == dir && !stopped_.count(id);
  }
  if (!loaded) {
    std::string why = dir.empty() ? "当前预设没有这个 skill: " + name : "读不到 " + name + " 的 SKILL.md";
    reply(id, why, true);
    audit(call, tool, "auto", false, 0, double(GetTickCount64() - t0), why);
    return;
  }
  Json final_check = call;final_check.at("arguments").set("path", dir + "\\SKILL.md");
  if (!dispatch_ok(tool, final_check, "auto", false)) return;
  std::string text(data.begin(), data.end());
  if (text.rfind("\xEF\xBB\xBF", 0) == 0) text.erase(0, 3);
  reply(id, text, false);
  audit(call, tool, "auto", true, text.size(), double(GetTickCount64() - t0), "");
}

void ToolHub::reply(const std::string& call_id, Json content, bool is_error) {
  { std::lock_guard<std::mutex> lock(mu_); in_flight_.erase(call_id); stopped_.erase(call_id); }
  Json m = Json::object();
  m.set("t", "tool-result").set("call_id", call_id).set("content", std::move(content)).set("is_error", is_error);
  post(m);
}

void ToolHub::post(Json msg) {
  std::wstring w = widen(msg.dump());
  app_.ui([this, w] { app_.web.post_json(w); });
}

void ToolHub::update_control() {
  bool on, watch, granted = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    ULONGLONG now = GetTickCount64();
    for (auto& kv : grants_) granted = granted || now < kv.second.until;
    granted = granted || task_active_locked(now);  // 渠道任务单在做: 提示条也亮着 (主人不在电脑前, 回来一眼看得到)
    on = control_running_ > 0 || granted;
    watch = on && stop_on_input_;
    if (on == control_on_) return;
    control_on_ = on;
  }
  logf("工具: %s", on ? "开始操作电脑 (提示条、真人输入检测打开)" : "不再操作电脑");
  Json m = Json::object();
  m.set("t", "control").set("on", on).set("hotkey", "Ctrl+Alt+Shift+Backspace").set("watch", watch);
  std::wstring w = widen(m.dump());
  app_.ui([this, w, watch, on] {
    app_.set_controlling(on);
    app_.web.post_json(w);
    app_.set_input_watch(watch);
  });
  // 自检: "这一轮都允许"期间提示条出来时也截一张 (文件名加 -control); 截的时候已经收起了就等下一次
  if (on && granted && !app_.opt.ask_shot.empty() && !control_shot_.exchange(true)) {
    std::wstring path = app_.opt.ask_shot;
    size_t dot = path.rfind(L'.');
    path = (dot == std::wstring::npos ? path : path.substr(0, dot)) + L"-control.png";
    std::thread([this, path] {
      Sleep(400);
      bool still;
      {
        std::lock_guard<std::mutex> lock(mu_);
        still = control_on_;
      }
      if (!still) {
        control_shot_ = false;
        return;
      }
      app_.ui([this, path] { app_.web.capture_png(path, [](bool ok) { logf("提示条截图%s", ok ? "好了" : "失败"); }); });
    }).detach();
  }
}

void ToolHub::audit(const Json& call, const Tool& tool, const std::string& how, bool ok, size_t chars, double ms,
                    const std::string& error) {
  static std::mutex file_mu;
  Json line = Json::object();
  line.set("time", now_iso()).set("call_id", call.get_str("call_id")).set("turn", call["turn"]).set("tool", tool.name);
  line.set("server", tool.server_name).set("raw", tool.raw).set("args", preview(call["arguments"]));
  if (tool.tier != guard::Tier::none) line.set("tier", guard::tier_name(tool.tier));
  if (tool.builtin) line.set("builtin", true);
  line.set("how", how).set("ok", ok).set("chars", double(chars)).set("ms", ms);
  if (!error.empty()) line.set("error", error);
  std::string text = line.dump() + "\n";
  std::lock_guard<std::mutex> lock(file_mu);
  if (FILE* f = _wfopen(settings::audit_path().c_str(), L"ab")) {
    fwrite(text.data(), 1, text.size(), f);
    fclose(f);
  }
  logf("工具: %s %s (%s, %.0f ms)", tool.name.c_str(), ok ? "成功" : "没成", how.c_str(), ms);
}
