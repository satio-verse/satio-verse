#include "settings.h"

#include <windows.h>
#include <wincrypt.h>

#include <algorithm>
#include <cctype>
#include <set>
#include <vector>

#include "extpack.h"
#include "util.h"

namespace settings {
namespace {

// 内置的云端提供方: 都是 OpenAI 兼容的接口. 模型名不写死 (各家改名很勤), 面板可以从 /models 拉
const char* kProviders[][3] = {
    {"deepseek", "DeepSeek", "https://api.deepseek.com/v1"},
    {"qwen", "通义千问 (DashScope)", "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"kimi", "Kimi (Moonshot)", "https://api.moonshot.cn/v1"},
    {"custom", "自定义 (OpenAI 兼容)", ""},
};

const char kEntropy[] = "WorldModel-pet-settings";

std::wstring g_dir;

std::wstring appdata_dir() {
  if (!g_dir.empty()) return g_dir;
  const wchar_t* appdata = _wgetenv(L"APPDATA");
  return std::wstring(appdata ? appdata : L".") + L"\\WorldModel";
}

std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
  return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

std::string unquote(std::string v) {
  v = trim(v);
  if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front()) v = v.substr(1, v.size() - 2);
  return v;
}

Json default_persona() {
  Json p = Json::object();
  p.set("id", "default").set("name", "默认").set("prompt", "").set("provider", "local").set("model", "");
  p.set("mcp", Json::array()).set("skills", Json::array());
  return p;
}

const Json* find_by(const Json& list, const char* key, const std::string& value) {
  for (const Json& item : list.items())
    if (item.get_str(key) == value) return &item;
  return nullptr;
}

// MCP 服务器的密钥: {"env":{...}, "headers":{...}}
Json open_secret(const Json& server) {
  std::string plain;
  Json s;
  if (!server.get_str("secret").empty() && unprotect(server.get_str("secret"), plain) && Json::parse(plain, s) && s.is_object())
    return s;
  s = Json::object();
  s.set("env", Json::object()).set("headers", Json::object());
  return s;
}

}  // namespace

void set_dir(const std::wstring& dir) { g_dir = dir; }
std::wstring path() { return appdata_dir() + L"\\pet-settings.json"; }
std::wstring audit_path() {
  CreateDirectoryW(appdata_dir().c_str(), nullptr);
  return appdata_dir() + L"\\pet-tool-audit.jsonl";
}
std::wstring skills_dir() { return appdata_dir() + L"\\skills"; }

std::wstring self_mcp_dir() {
  if (!g_dir.empty()) return g_dir + L"\\self-mcp";
  wchar_t home[MAX_PATH];
  DWORD n = GetEnvironmentVariableW(L"USERPROFILE", home, MAX_PATH);
  return (n > 0 && n < MAX_PATH ? std::wstring(home, n) : appdata_dir()) + L"\\.worldmodel\\self-mcp";
}

Json self_mcp_servers(const Json& s) {
  Json out = Json::array();
  if (!s["self_mcp"]["enabled"].boolean(true)) return out;
  std::vector<char> data;
  Json reg;
  if (!read_file(self_mcp_dir() + L"\\registry.json", data) || !Json::parse(std::string(data.begin(), data.end()), reg)) return out;
  std::wstring root = full_path(self_mcp_dir());
  for (const Json& e : reg["servers"].items()) {
    std::string id = e.get_str("id"), name = e.get_str("name");
    if (id.rfind("self-", 0) != 0 || !e["enabled"].boolean(false)) continue;
    // 只起名单所在目录下面的、文件没被改过的 (SHA-256 和服务端装的时候记的一样)
    std::wstring dir = full_path(widen(e.get_str("dir")));
    bool ok = dir.size() > root.size() && dir.compare(0, root.size(), root) == 0 && dir[root.size()] == L'\\';
    for (const char* f : {"manifest.json", "server.py", "selfmcp_kit.py"}) {
      std::vector<char> body;
      ok = ok && read_file(dir + L"\\" + widen(f), body) &&
           extpack::sha256_hex(std::string(body.begin(), body.end())) == e["sha256"].get_str(f);
    }
    std::string python = e.get_str("python");
    std::string low = python;
    std::transform(low.begin(), low.end(), low.begin(), [](unsigned char c) { return char(std::tolower(c)); });
    ok = ok && low.size() > 10 && low.compare(low.size() - 10, 10, "python.exe") == 0;
    if (!ok) {
      logf("工具: 她装的 MCP %s 和登记的对不上 (目录、文件或 Python), 不起", name.c_str());
      continue;
    }
    Json args = Json::array();
    args.push("-E").push("-s").push("server.py");
    Json cfg = Json::object();
    cfg.set("id", id).set("name", name).set("transport", "stdio").set("command", python).set("args", args)
        .set("cwd", utf8(dir)).set("env", Json::object());
    out.push(cfg);
  }
  return out;
}

std::string protect(const std::string& plain) {
  DATA_BLOB in{DWORD(plain.size()), reinterpret_cast<BYTE*>(const_cast<char*>(plain.data()))};
  DATA_BLOB entropy{DWORD(sizeof kEntropy), reinterpret_cast<BYTE*>(const_cast<char*>(kEntropy))};
  DATA_BLOB out{};
  if (!CryptProtectData(&in, L"WorldModel pet", &entropy, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) return "";
  std::string b64 = base64_encode(out.pbData, out.cbData);
  LocalFree(out.pbData);
  return b64;
}

bool unprotect(const std::string& b64, std::string& plain) {
  std::vector<uint8_t> raw;
  if (!base64_decode(b64, raw) || raw.empty()) return false;
  DATA_BLOB in{DWORD(raw.size()), raw.data()};
  DATA_BLOB entropy{DWORD(sizeof kEntropy), reinterpret_cast<BYTE*>(const_cast<char*>(kEntropy))};
  DATA_BLOB out{};
  if (!CryptUnprotectData(&in, nullptr, &entropy, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) return false;
  plain.assign(reinterpret_cast<char*>(out.pbData), out.cbData);
  SecureZeroMemory(out.pbData, out.cbData);
  LocalFree(out.pbData);
  return true;
}

Json load() {
  Json s;
  std::vector<char> data;
  if (!read_file(path(), data) || !Json::parse(std::string(data.begin(), data.end()), s) || !s.is_object()) s = Json::object();
  s.set("version", 1);
  if (!s["personas"].is_array()) s.set("personas", Json::array());
  if (!find_by(s["personas"], "id", "default")) {
    Json& list = s.at("personas");
    list.mut_items().insert(list.mut_items().begin(), default_persona());
  }
  if (!s["providers"].is_object()) s.set("providers", Json::object());
  Json& providers = s.at("providers");
  for (auto& p : kProviders) {
    Json& one = providers.at(p[0]);
    if (!one.is_object()) one = Json::object();
    one.set("label", p[1]);
    if (!one["base_url"].is_string()) one.set("base_url", p[2]);
    if (!one["model"].is_string()) one.set("model", "");
  }
  if (!s["mcp"].is_array()) s.set("mcp", Json::array());
  if (!find_by(s["personas"], "id", s.get_str("active"))) s.set("active", "default");
  return s;
}

bool save(const Json& s) {
  CreateDirectoryW(appdata_dir().c_str(), nullptr);
  std::wstring tmp = path() + L".tmp";
  HANDLE f = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (f == INVALID_HANDLE_VALUE) return false;
  std::string text = s.dump();
  DWORD written = 0;
  bool ok = WriteFile(f, text.data(), DWORD(text.size()), &written, nullptr) && written == text.size();
  CloseHandle(f);
  // 先写临时文件再替换: 写到一半断电也不会把原来的设置弄坏
  return ok && MoveFileExW(tmp.c_str(), path().c_str(), MOVEFILE_REPLACE_EXISTING);
}

Json for_panel(const Json& s) {
  Json out = s;
  for (auto& kv : out.at("providers").members()) {
    Json& p = out.at("providers").at(kv.first);
    p.set("has_key", !p.get_str("key").empty());
    p.erase("key");
  }
  for (Json& server : out.at("mcp").mut_items()) {
    Json secret = open_secret(server);
    Json names = Json::object();
    for (const char* part : {"env", "headers"}) {
      Json list = Json::array();
      for (const auto& kv : secret[part].members()) list.push(kv.first);
      names.set(part, list);
    }
    server.set("secret_names", names);
    server.erase("secret");
  }
  if (out["qq"].is_object()) {  // QQ 的协议端 token: 面板只看得到有没有
    Json& q = out.at("qq");
    q.set("has_token", !q.get_str("token").empty());
    q.erase("token");
  }
  return out;
}

Json merge(const Json& old, const Json& from_panel, const Json& keys, const Json& secrets) {
  Json s = from_panel.is_object() ? from_panel : Json::object();
  // 提供方: 面板只能换 key 或清掉, 看不到原来的
  Json& providers = s.at("providers");
  if (!providers.is_object()) providers = Json::object();
  for (const auto& kv : old["providers"].members()) {
    Json& p = providers.at(kv.first);
    if (!p.is_object()) p = kv.second;
    p.erase("has_key");
    const Json& fresh = keys[kv.first];
    std::string key = fresh.is_string() ? (fresh.str().empty() ? "" : protect(trim(fresh.str()))) : kv.second.get_str("key");
    if (key.empty()) p.erase("key");
    else p.set("key", key);
  }
  // MCP 服务器: 密钥按服务器 id 跟着走 (改名不丢)
  Json& servers = s.at("mcp");
  if (!servers.is_array()) servers = Json::array();
  for (Json& server : servers.mut_items()) {
    server.erase("secret_names");
    server.erase("secret");
    std::string id = server.get_str("id");
    if (id.empty()) server.set("id", id = "s" + std::to_string(GetTickCount64()) + std::to_string(rand() % 1000));
    const Json* before = find_by(old["mcp"], "id", id);
    Json secret = before ? open_secret(*before) : open_secret(Json::object());
    const Json& change = secrets[id];
    for (const char* part : {"env", "headers"}) {
      for (const auto& kv : change[part].members())
        if (kv.second.is_string()) secret.at(part).set(kv.first, kv.second.str());
      for (const Json& k : change["remove"][part].items()) secret.at(part).erase(k.str());
    }
    if (!secret["env"].members().empty() || !secret["headers"].members().empty()) server.set("secret", protect(secret.dump()));
  }
  // 人设: 至少有默认的那个, id 不重复, 当前的要存在
  Json& personas = s.at("personas");
  if (!personas.is_array()) personas = Json::array();
  std::set<std::string> seen;
  Json clean = Json::array();
  for (const Json& p : personas.items()) {
    std::string id = p.get_str("id");
    if (id.empty() || !seen.insert(id).second) continue;
    clean.push(p);
  }
  if (!seen.count("default")) clean.mut_items().insert(clean.mut_items().begin(), default_persona());
  s.set("personas", clean);
  if (!find_by(s["personas"], "id", s.get_str("active"))) s.set("active", "default");
  // QQ: token 和 API key 一样, 面板只能换新的 (keys["qq"]) 或清掉 (""), 没提到就留原来的
  if (s["qq"].is_object()) {
    Json& q = s.at("qq");
    q.erase("has_token");
    const Json& fresh = keys["qq"];
    std::string token = fresh.is_string() ? (fresh.str().empty() ? "" : protect(trim(fresh.str()))) : old["qq"].get_str("token");
    if (token.empty()) q.erase("token");
    else q.set("token", token);
  }
  // 桌宠自己的开关 (置顶、眼睛跟着鼠标) 不在面板里改: 用磁盘上的, 免得面板开着时在侧菜单里切的又被盖回去
  if (old.has("pet")) s.set("pet", old["pet"]);
  else s.erase("pet");
  s.set("version", 1);
  return s;
}

namespace {
// 一个人设预设用的模型: {"provider":"local"} 或 {provider, base_url, model, api_key (明文)}
Json model_of(const Json& s, const Json& p) {
  Json llm = Json::object();
  std::string provider = p.get_str("provider", "local");
  const Json& cfg = s["providers"][provider];
  if (provider == "local" || !cfg.is_object()) {
    llm.set("provider", "local");
  } else {
    std::string key;
    if (!cfg.get_str("key").empty()) unprotect(cfg.get_str("key"), key);
    std::string model = p.get_str("model");
    llm.set("provider", provider).set("base_url", cfg.get_str("base_url")).set("model", model.empty() ? cfg.get_str("model") : model);
    llm.set("api_key", key);
  }
  return llm;
}
}  // namespace

Json active_profile(const Json& s) {
  const Json* persona = find_by(s["personas"], "id", s.get_str("active"));
  Json p = persona ? *persona : default_persona();
  Json out = Json::object();
  Json who = Json::object();
  who.set("id", p.get_str("id")).set("name", p.get_str("name")).set("prompt", p.get_str("prompt"));
  out.set("persona", who);
  out.set("llm", model_of(s, p));
  Json skills = Json::array();
  std::set<std::string> wanted;
  for (const Json& name : p["skills"].items()) wanted.insert(name.str());
  for (const std::string& name : auto_skills(s, p)) wanted.insert(name);  // 操控电脑的预设自动带上操作要领
  Json all = scan_skills();  // 先存下来: 直接遍历临时对象的成员会悬空
  for (const Json& sk : all.items())
    if (wanted.count(sk.get_str("name"))) {
      Json one = Json::object();
      one.set("name", sk.get_str("name")).set("description", sk.get_str("description"));
      skills.push(one);
    }
  out.set("skills", skills);
  Json mcp = Json::array();
  for (const Json& id : p["mcp"].items()) {
    const Json* server = find_by(s["mcp"], "id", id.str());
    if (server && (*server)["enabled"].boolean(true)) mcp.push(server->get_str("name"));
  }
  out.set("mcp", mcp);
  // 门面: 3D (VRM, 服务端默认的角色)、3D 的某个模型 (vrm:文件名, 宿主打开页面时带 avatar=) 或 2D (Live2D)
  std::string face = p.get_str("face");
  out.set("face", face == "live2d" || face.rfind("vrm:", 0) == 0 ? face : "vrm");
  return out;
}

Json channel_config(const Json& s) {
  const Json& q = s["qq"];
  if (!q.is_object()) return Json();
  Json out = Json::object();
  out.set("type", "channel_config").set("channel", "qq").set("enabled", q["enabled"].boolean());
  std::string token;
  if (!q.get_str("token").empty()) unprotect(q.get_str("token"), token);
  out.set("token", token).set("snowluma_dir", q.get_str("snowluma_dir"));
  out.set("snowluma_autostart", q["snowluma_autostart"].boolean(true));
  out.set("self_id", q["self_id"].number(0)).set("owner_id", q["owner_id"].number(0));
  Json names = Json::array(), block = Json::array(), convs = Json::array();
  for (const Json& n : q["names"].items())
    if (n.is_string() && !trim(n.str()).empty()) names.push(trim(n.str()));
  for (const Json& b : q["block"].items())
    if (b.number(0) > 0) block.push(b.number(0));
  for (const Json& c : q["convs"].items()) {  // 只转服务端认的字段
    Json one = Json::object();
    one.set("type", c.get_str("type")).set("id", c["id"].number(0)).set("name", c.get_str("name"));
    if (!c.get_str("trigger").empty()) one.set("trigger", c.get_str("trigger"));
    if (!c.get_str("send").empty()) one.set("send", c.get_str("send"));
    if (c.has("enabled")) one.set("enabled", c["enabled"].boolean(true));
    if (c.has("core")) one.set("core", c["core"].boolean());
    if (c.has("log_raw")) one.set("log_raw", c["log_raw"]);  // Preserve false/invalid values for server validation.
    if (c.has("notify")) one.set("notify", c["notify"].boolean(true));  // 只关这一行的重要消息汇报, 聊天照常 (服务端 2026-11-05 起认; 以前这里没转, 面板上关了也白关)
    convs.push(one);
  }
  if (!names.items().empty()) out.set("names", names);
  out.set("block", block).set("convs", convs);
  out.set("access_scope", q.get_str("access_scope", "follow"));
  out.set("cross_session", q["cross_session"].boolean(true));  // 私聊时记得别处聊过什么: 面板还没这一项时默认开
  if (q["blocked_groups"].is_array()) out.set("blocked_groups", q["blocked_groups"]);
  Json group = Json::object(), priv = Json::object();
  if (!q.get_str("group_send").empty()) group.set("send", q.get_str("group_send"));
  if (!q.get_str("private_send").empty()) priv.set("send", q.get_str("private_send"));
  if (q["group"].has("log_raw")) group.set("log_raw", q["group"]["log_raw"]);
  if (q.has("api_log_raw")) out.set("api_log_raw", q["api_log_raw"]);
  out.set("group", group).set("private", priv);
  if (!q.get_str("owner_send").empty()) out.set("owner_send", q.get_str("owner_send"));
  if (q["daily_tokens"].number(-1) >= 0) out.set("daily_tokens", q["daily_tokens"].number(0));
  // 她自己开口 (主动找你、发 QQ 空间说说): 原样转, 字段和范围由服务端 (wm/channels/manager.py parse_conf) 检查
  for (const char* k : {"proactive", "qzone"})
    if (q[k].is_object()) out.set(k, q[k]);
  // 人设和模型取所选的预设 (它的 MCP 服务器和 skill 在 QQ 上不生效: 外部渠道没有工具); 没选就是服务端默认人设 + 本地模型
  const Json* p = find_by(s["personas"], "id", q.get_str("persona"));
  out.set("persona", p ? p->get_str("prompt") : "");
  // 没选预设: 人设用服务端的默认人设 (绫音), 模型用 QQ 页自己选的 (provider / model, 空着是本地模型)
  Json own = Json::object();
  own.set("provider", q.get_str("provider", "local")).set("model", q.get_str("model"));
  Json llm = model_of(s, p ? *p : own);
  if (llm.get_str("provider", "local") != "local") {
    Json model = Json::object();
    model.set("provider", llm.get_str("provider")).set("base_url", llm.get_str("base_url")).set("model", llm.get_str("model"));
    model.set("api_key", llm.get_str("api_key"));
    out.set("model", model);
  } else {
    out.set("model", Json());
  }
  return out;
}

Json mcp_server(const Json& s, const std::string& id, std::string* error) {
  const Json* server = find_by(s["mcp"], "id", id);
  if (!server) {
    if (error) *error = "设置里没有这个 MCP 服务器 (先保存)";
    return Json();
  }
  Json cfg = *server;
  Json secret = open_secret(*server);
  cfg.erase("secret");
  cfg.set("env", secret["env"]).set("headers", secret["headers"]);
  return cfg;
}

std::wstring g_bundled;  // 随附 skill 的目录 (desktop/skills)

void set_bundled_skills_dir(const std::wstring& dir) { g_bundled = dir; }

std::vector<std::string> auto_skills(const Json& s, const Json& persona) {
  std::vector<std::string> out;
  for (const Json& id : persona["mcp"].items()) {
    const Json* server = find_by(s["mcp"], "id", id.str());
    if (server && (*server)["enabled"].boolean(true) && server->get_str("kind") == "windows-mcp") {
      out.push_back("computer-use");
      break;
    }
  }
  return out;
}

namespace {

void scan_dir(const std::wstring& dir, bool bundled, Json& out);

}  // namespace

Json scan_skills() {
  std::wstring dir = skills_dir();
  CreateDirectoryW(appdata_dir().c_str(), nullptr);
  CreateDirectoryW(dir.c_str(), nullptr);  // 第一次就建好, 面板的"打开文件夹"才有地方去
  Json out = Json::array();
  scan_dir(dir, false, out);
  if (!g_bundled.empty()) scan_dir(g_bundled, true, out);  // 随附的放后面; 和用户的同名时不要
  return out;
}

namespace {

void scan_dir(const std::wstring& dir, bool bundled, Json& out) {
  WIN32_FIND_DATAW fd;
  HANDLE h = FindFirstFileW((dir + L"\\*").c_str(), &fd);
  if (h == INVALID_HANDLE_VALUE) return;
  do {
    if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || fd.cFileName[0] == L'.') continue;
    std::wstring folder = dir + L"\\" + fd.cFileName;
    std::vector<char> data;
    if (!read_file(folder + L"\\SKILL.md", data)) continue;
    std::string text(data.begin(), data.end());
    if (text.rfind("\xEF\xBB\xBF", 0) == 0) text.erase(0, 3);
    // frontmatter: 开头两行 --- 之间的 name / description (description 可以是 > 或 | 开头的多行)
    std::string name, desc;
    if (text.rfind("---", 0) == 0) {
      size_t end = text.find("\n---", 3);
      std::string head = text.substr(3, end == std::string::npos ? std::string::npos : end - 3);
      std::vector<std::string> lines;
      for (size_t a = 0, b; a < head.size(); a = b + 1) {
        b = head.find('\n', a);
        if (b == std::string::npos) b = head.size();
        lines.push_back(head.substr(a, b - a));
      }
      for (size_t i = 0; i < lines.size(); i++) {
        std::string line = lines[i];
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t colon = line.find(':');
        if (colon == std::string::npos || line[0] == ' ' || line[0] == '\t') continue;
        std::string key = trim(line.substr(0, colon)), value = trim(line.substr(colon + 1));
        if (value == ">" || value == "|" || value == ">-" || value == "|-" || value.empty()) {
          value.clear();
          while (i + 1 < lines.size() && !lines[i + 1].empty() && (lines[i + 1][0] == ' ' || lines[i + 1][0] == '\t'))
            value += (value.empty() ? "" : " ") + trim(lines[++i]);
        }
        if (key == "name") name = unquote(value);
        else if (key == "description") desc = unquote(value);
      }
    }
    Json sk = Json::object();
    sk.set("name", name.empty() ? utf8(fd.cFileName) : name).set("description", desc).set("dir", utf8(folder));
    if (bundled) {
      if (find_by(out, "name", sk.get_str("name"))) continue;  // 用户自己有同名的 skill
      sk.set("bundled", true);
    }
    out.push(sk);
  } while (FindNextFileW(h, &fd));
  FindClose(h);
}

// 用户自己的 skill (不是随附的) 里叫这个名字的
const Json* user_skill(const Json& all, const std::string& name) {
  const Json* sk = find_by(all, "name", name);
  return sk && !(*sk)["bundled"].boolean() ? sk : nullptr;
}

bool new_name_ok(const std::string& n) {  // 和 Claude、dsh 的 skill 一样: 小写英文、数字、-
  if (n.empty() || n.size() > 64 || n[0] == '-') return false;
  for (char c : n)
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
  return true;
}

}  // namespace

Json read_skill(const std::string& name) {
  Json all = scan_skills();
  const Json* sk = find_by(all, "name", name);
  if (!sk) return Json();
  std::vector<char> data;
  if (!read_file(widen(sk->get_str("dir")) + L"\\SKILL.md", data)) return Json();
  std::string text(data.begin(), data.end());
  if (text.rfind("\xEF\xBB\xBF", 0) == 0) text.erase(0, 3);
  std::string body = text;
  if (text.rfind("---", 0) == 0) {  // 正文是 frontmatter 后面的部分
    size_t end = text.find("\n---", 3);
    size_t start = end == std::string::npos ? std::string::npos : text.find('\n', end + 4);
    body = start == std::string::npos ? "" : text.substr(start + 1);
  }
  Json out = *sk;
  out.set("body", trim(body));
  return out;
}

bool save_skill(const std::string& name, const std::string& description, const std::string& body, std::string& error) {
  Json all = scan_skills();
  const Json* mine = user_skill(all, name);
  if (!mine && !new_name_ok(name)) {
    error = "名字只能用小写英文、数字和 -, 不超过 64 个字, 不能以 - 开头";
    return false;
  }
  std::string desc;  // 描述写成一行 (frontmatter 里用 >- 折叠, 不怕引号和冒号)
  for (char c : description) desc += (c == '\r' || c == '\n' || c == '\t') ? ' ' : c;
  desc = trim(desc);
  if (desc.empty()) {
    error = "要写描述: 什么时候用这个 skill (模型靠它决定要不要读全文)";
    return false;
  }
  if (desc.size() > 3000 || body.size() > 64 * 1024) {
    error = "描述最多 1000 字左右, 正文最多 64 KB";
    return false;
  }
  CreateDirectoryW(appdata_dir().c_str(), nullptr);
  CreateDirectoryW(skills_dir().c_str(), nullptr);
  std::wstring dir = mine ? widen(mine->get_str("dir")) : skills_dir() + L"\\" + widen(name);
  CreateDirectoryW(dir.c_str(), nullptr);
  std::string text = "---\nname: " + name + "\ndescription: >-\n  " + desc + "\n---\n\n" + trim(body) + "\n";
  std::wstring file = dir + L"\\SKILL.md", tmp = file + L".tmp";
  HANDLE f = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  DWORD written = 0;
  bool ok = f != INVALID_HANDLE_VALUE && WriteFile(f, text.data(), DWORD(text.size()), &written, nullptr) && written == text.size();
  if (f != INVALID_HANDLE_VALUE) CloseHandle(f);
  if (!ok || !MoveFileExW(tmp.c_str(), file.c_str(), MOVEFILE_REPLACE_EXISTING)) {
    error = "写不进 " + utf8(file);
    return false;
  }
  return true;
}

bool delete_skill(const std::string& name, std::string& error) {
  Json all = scan_skills();
  const Json* mine = user_skill(all, name);
  if (!mine) {
    error = find_by(all, "name", name) ? "桌宠随附的 skill 删不了 (可以在用户文件夹存一份同名的盖过它)" : "没有这个 skill";
    return false;
  }
  std::wstring trash = skills_dir() + L"\\.trash";
  CreateDirectoryW(trash.c_str(), nullptr);
  SYSTEMTIME t;
  GetLocalTime(&t);
  wchar_t stamp[32];
  swprintf(stamp, 32, L"%04d%02d%02d-%02d%02d%02d", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
  std::wstring dir = widen(mine->get_str("dir"));
  std::wstring target = trash + L"\\" + dir.substr(dir.find_last_of(L"\\/") + 1) + L"-" + stamp;
  if (!MoveFileExW(dir.c_str(), target.c_str(), 0)) {
    error = "挪不动 (文件可能被别的程序开着)";
    return false;
  }
  return true;
}

}  // namespace settings
