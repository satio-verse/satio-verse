#include "extpack.h"

#include <windows.h>
#include <bcrypt.h>

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <map>
#include <regex>
#include <set>
#include <vector>

#include "settings.h"
#include "util.h"

namespace extpack {
namespace {

constexpr size_t kMaxPack = 2 * 1024 * 1024;  // 整个包
constexpr size_t kMaxFile = 200 * 1024;       // 一个文件
constexpr size_t kMaxFiles = 100;
constexpr size_t kMaxSkillFiles = 20;

const std::regex kIdRe("^[a-z0-9][a-z0-9._-]{0,63}$");
const std::regex kNameRe("^[A-Za-z0-9_-]{1,64}$");
const std::regex kFileRe("^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\\.(md|txt|json)$");

// 文字里有这些 (不分大小写) 就提醒用户多看一眼: 像是在让模型忽略规则、要密码 key、执行命令、转钱
const char* kSuspicious[] = {"忽略之前", "忽略以上", "忽略上面", "无视之前", "ignore previous", "ignore all previous", "disregard previous",
                             "system prompt", "系统提示", "api key", "api_key", "apikey", "密码", "password", "token", "执行命令",
                             "powershell", "cmd.exe", "rm -rf", "del /", "base64", "转账", "付款", "发给"};

std::string lower(std::string s) {
  for (auto& c : s) c = char(tolower(static_cast<unsigned char>(c)));
  return s;
}

Json warnings_of(const std::string& text) {
  Json w = Json::array();
  std::string t = lower(text);
  for (const char* s : kSuspicious)
    if (t.find(s) != std::string::npos) w.push(std::string("有\"") + s + "\"");
  return w;
}

std::wstring settings_dir() {
  std::wstring p = settings::path();
  size_t slash = p.find_last_of(L"\\/");
  return slash == std::wstring::npos ? L"." : p.substr(0, slash);
}

std::string now_iso() {
  SYSTEMTIME t;
  GetLocalTime(&t);
  char b[32];
  snprintf(b, sizeof b, "%04d-%02d-%02dT%02d:%02d:%02d", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
  return b;
}

bool write_text(const std::wstring& path, const std::string& text) {
  FILE* f = _wfopen(path.c_str(), L"wb");
  if (!f) return false;
  bool ok = fwrite(text.data(), 1, text.size(), f) == text.size();
  fclose(f);
  return ok;
}

// SKILL.md 开头的 frontmatter: ---\nname: x\ndescription: y\n---
bool frontmatter(const std::string& md, std::string& name, std::string& desc) {
  std::string t = md.rfind("\xEF\xBB\xBF", 0) == 0 ? md.substr(3) : md;
  if (t.rfind("---", 0) != 0) return false;
  size_t end = t.find("\n---", 3);
  if (end == std::string::npos) return false;
  size_t pos = t.find('\n');
  while (pos != std::string::npos && pos < end) {
    size_t next = t.find('\n', pos + 1);
    std::string line = t.substr(pos + 1, (next == std::string::npos ? t.size() : next) - pos - 1);
    if (!line.empty() && line.back() == '\r') line.pop_back();
    size_t colon = line.find(':');
    if (colon != std::string::npos) {
      std::string k = line.substr(0, colon), v = line.substr(colon + 1);
      while (!v.empty() && v.front() == ' ') v.erase(0, 1);
      if (k == "name") name = v;
      if (k == "description") desc = v;
    }
    pos = next;
  }
  return !name.empty() && !desc.empty();
}

// 人设只带这几项: 名字、人设文字、门面、用哪家模型 (不带地址和 key)、用哪些 skill. MCP 服务器、内置工具不带
Json clean_persona(const Json& p, std::string& error) {
  Json out = Json::object();
  std::string name = p.get_str("name"), prompt = p.get_str("prompt");
  if (name.empty() || name.size() > 120) {
    error = "人设的名字是空的或太长";
    return Json();
  }
  if (prompt.size() > 16000) {
    error = "人设文字太长 (最多约 5000 字)";
    return Json();
  }
  out.set("name", name).set("prompt", prompt);
  std::string face = p.get_str("face");
  if (face == "live2d" || face == "vrm") out.set("face", face);
  std::string provider = p.get_str("provider", "local"), model = p.get_str("model");
  out.set("provider", provider.size() <= 32 ? provider : "local").set("model", model.size() <= 120 ? model : "");
  Json skills = Json::array();
  for (const Json& s : p["skills"].items())
    if (s.is_string() && std::regex_match(s.str(), kNameRe)) skills.push(s.str());
  out.set("skills", skills);
  return out;
}

struct Pack {
  Json manifest;
  std::vector<std::pair<std::string, Json>> personas;                         // 文件名 -> 清理过的人设
  std::map<std::string, std::vector<std::pair<std::string, std::string>>> skills;  // skill 名 -> [(文件名, 文字)]
  std::string sha;
};

bool parse(const std::string& text, Pack& pk, std::string& error) {
  if (text.size() > kMaxPack) {
    error = "包太大了 (最多 2 MB)";
    return false;
  }
  Json doc;
  if (!Json::parse(text.rfind("\xEF\xBB\xBF", 0) == 0 ? text.substr(3) : text, doc) || !doc.is_object() || doc["wmext"].number() != 1) {
    error = "不是桌宠的内容包 (.wmext 第 1 版)";
    return false;
  }
  pk.manifest = doc["manifest"];
  const Json& m = pk.manifest;
  if (!m.is_object() || m.get_str("type") != "content") {
    error = "清单不对: 第一版只认内容包 (type: content)";
    return false;
  }
  if (!std::regex_match(m.get_str("id"), kIdRe)) {
    error = "清单里的 id 不对 (小写字母、数字、. _ -, 最多 64 个字)";
    return false;
  }
  if (m.get_str("name").empty() || m.get_str("name").size() > 120 || m.get_str("version").empty() || m.get_str("version").size() > 32) {
    error = "清单里的名字或版本不对";
    return false;
  }
  if (m.get_str("license").empty() || m.get_str("license").size() > 80) {
    error = "清单里要写许可 (license)";
    return false;
  }
  const auto& files = doc["files"].items();
  if (files.empty() || files.size() > kMaxFiles) {
    error = "包里的文件数不对 (1 到 100 个)";
    return false;
  }
  std::set<std::string> seen;
  for (const Json& f : files) {
    std::string path = f.get_str("path"), body = f.get_str("text"), sha = lower(f.get_str("sha256"));
    if (!seen.insert(path).second) {
      error = "文件重复: " + path;
      return false;
    }
    if (body.size() > kMaxFile) {
      error = "文件太大 (最多 200 KB): " + path;
      return false;
    }
    if (sha256_hex(body) != sha) {
      error = "SHA-256 对不上 (文件被改过或坏了): " + path;
      return false;
    }
    std::vector<std::string> parts;
    for (size_t a = 0, b; a <= path.size(); a = b + 1) {
      b = path.find('/', a);
      if (b == std::string::npos) b = path.size();
      parts.push_back(path.substr(a, b - a));
    }
    if (parts.size() == 2 && parts[0] == "personas" && std::regex_match(parts[1], kFileRe) && parts[1].size() > 5 &&
        parts[1].compare(parts[1].size() - 5, 5, ".json") == 0) {
      Json p;
      if (!Json::parse(body, p) || !p.is_object()) {
        error = "人设文件不是 JSON: " + path;
        return false;
      }
      Json clean = clean_persona(p, error);
      if (clean.is_null()) {
        error += ": " + path;
        return false;
      }
      pk.personas.emplace_back(parts[1], clean);
    } else if (parts.size() == 3 && parts[0] == "skills" && std::regex_match(parts[1], kNameRe) && std::regex_match(parts[2], kFileRe)) {
      auto& list = pk.skills[parts[1]];
      if (list.size() >= kMaxSkillFiles) {
        error = "一个 skill 最多 20 个文件: " + parts[1];
        return false;
      }
      list.emplace_back(parts[2], body);
    } else {
      error = "不认识的文件路径 (只能是 personas/<名字>.json 或 skills/<skill>/<文件>.md|txt|json): " + path;
      return false;
    }
  }
  for (auto& [name, list] : pk.skills) {
    auto md = std::find_if(list.begin(), list.end(), [](const auto& x) { return x.first == "SKILL.md"; });
    std::string fm_name, fm_desc;
    if (md == list.end() || !frontmatter(md->second, fm_name, fm_desc)) {
      error = "skill " + name + " 没有 SKILL.md, 或者开头没写 name 和 description";
      return false;
    }
    if (fm_name != name) {
      error = "skill " + name + " 的 SKILL.md 里写的名字是 " + fm_name + ", 要和文件夹一样";
      return false;
    }
  }
  pk.sha = sha256_hex(text);
  return true;
}

// 和已有的东西冲突: 同名的 skill 已经有了、同一个包已经装过
Json conflicts_of(const Pack& pk, const Json& s) {
  Json c = Json::array();
  for (const Json& e : s["extensions"].items())
    if (e.get_str("id") == pk.manifest.get_str("id")) c.push("这个包已经装过了 (" + e.get_str("version") + "), 先卸载再装");
  for (const auto& kv : pk.skills) {
    DWORD attr = GetFileAttributesW((settings::skills_dir() + L"\\" + widen(kv.first)).c_str());
    if (attr != INVALID_FILE_ATTRIBUTES) c.push("已经有一个叫 " + kv.first + " 的 skill 了 (先在 Skill 页删掉或改名)");
  }
  return c;
}

}  // namespace

std::string sha256_hex(const std::string& data) {
  BCRYPT_ALG_HANDLE alg = nullptr;
  BCRYPT_HASH_HANDLE h = nullptr;
  unsigned char out[32];
  std::string hex;
  if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0) return "";
  if (BCryptCreateHash(alg, &h, nullptr, 0, nullptr, 0, 0) == 0) {
    BCryptHashData(h, reinterpret_cast<PUCHAR>(const_cast<char*>(data.data())), ULONG(data.size()), 0);
    if (BCryptFinishHash(h, out, sizeof out, 0) == 0) {
      char b[3];
      for (unsigned char c : out) {
        snprintf(b, sizeof b, "%02x", c);
        hex += b;
      }
    }
    BCryptDestroyHash(h);
  }
  BCryptCloseAlgorithmProvider(alg, 0);
  return hex;
}

std::wstring exports_dir() { return settings_dir() + L"\\exports"; }

Json inspect(const std::string& text, std::string& error) {
  Pack pk;
  if (!parse(text, pk, error)) return Json();
  Json s = settings::load();
  Json items = Json::array(), all_warn = Json::array();
  for (const auto& [file, p] : pk.personas) {
    Json it = Json::object();
    std::string provider = p.get_str("provider");
    std::string model = provider == "local" ? "本地模型" : provider + (p.get_str("model").empty() ? "" : " · " + p.get_str("model"));
    if (provider != "local" && !s["providers"][provider].is_object()) model += " (你这里没有这家, 导入后改成本地模型)";
    it.set("kind", "persona").set("name", p.get_str("name")).set("file", "personas/" + file).set("model", model);
    it.set("skills", p["skills"]).set("text", p.get_str("prompt")).set("warnings", warnings_of(p.get_str("name") + "\n" + p.get_str("prompt")));
    items.push(it);
  }
  for (const auto& [name, list] : pk.skills) {
    Json it = Json::object(), files = Json::array();
    std::string all;
    for (const auto& [file, body] : list) {
      files.push(file);
      all += (all.empty() ? "" : "\n\n") + std::string("── ") + file + " ──\n" + body;
    }
    it.set("kind", "skill").set("name", name).set("files", files).set("text", all).set("warnings", warnings_of(all));
    items.push(it);
  }
  for (const Json& it : items.items())
    for (const Json& w : it["warnings"].items()) all_warn.push(it.get_str("name") + ": " + w.str());
  Json out = Json::object();
  out.set("manifest", pk.manifest).set("items", items).set("warnings", all_warn).set("conflicts", conflicts_of(pk, s));
  out.set("sha256", pk.sha).set("size", double(text.size()));
  return out;
}

Json install(const std::string& text, std::string& error) {
  Pack pk;
  if (!parse(text, pk, error)) return Json();
  Json s = settings::load();
  Json conflicts = conflicts_of(pk, s);
  if (!conflicts.items().empty()) {
    error = conflicts.items()[0].str();
    return Json();
  }
  // skill: 一个文件夹一个
  Json skill_names = Json::array();
  for (const auto& [name, list] : pk.skills) {
    std::wstring dir = settings::skills_dir() + L"\\" + widen(name);
    CreateDirectoryW(settings::skills_dir().c_str(), nullptr);
    if (!CreateDirectoryW(dir.c_str(), nullptr)) {
      error = "建不了 skill 文件夹: " + name;
      return Json();
    }
    for (const auto& [file, body] : list)
      if (!write_text(dir + L"\\" + widen(file), body)) {
        error = "写不进 skill 文件: " + name + "/" + file;
        return Json();
      }
    skill_names.push(name);
  }
  // 人设: 新的 id; 没有的模型提供方改成本地; 不带 MCP 服务器和内置工具 (内容包不能给她加工具)
  Json persona_ids = Json::array();
  int n = 0;
  for (const auto& [file, p] : pk.personas) {
    Json np = p;
    std::string id = "x" + std::to_string(GetTickCount64()) + "_" + std::to_string(n++);
    if (np.get_str("provider") != "local" && !s["providers"][np.get_str("provider")].is_object()) np.set("provider", "local").set("model", "");
    Json skills = Json::array();
    for (const Json& k : np["skills"].items())
      if (pk.skills.count(k.str()) || GetFileAttributesW((settings::skills_dir() + L"\\" + widen(k.str())).c_str()) != INVALID_FILE_ATTRIBUTES)
        skills.push(k.str());
    np.set("id", id).set("skills", skills).set("mcp", Json::array()).set("builtins", Json::array()).set("source", pk.manifest.get_str("id"));
    s.at("personas").push(np);
    persona_ids.push(id);
  }
  Json rec = Json::object();
  const Json& m = pk.manifest;
  rec.set("id", m.get_str("id")).set("name", m.get_str("name")).set("version", m.get_str("version")).set("type", "content");
  rec.set("author", m.get_str("author")).set("license", m.get_str("license")).set("description", m.get_str("description"));
  rec.set("sha256", pk.sha).set("installed", now_iso()).set("personas", persona_ids).set("skills", skill_names);
  s.at("extensions").push(rec);
  if (!settings::save(s)) {
    error = "设置文件写不进去";
    return Json();
  }
  logf("扩展: 装了内容包 %s %s (人设 %zu 个, skill %zu 个)", m.get_str("id").c_str(), m.get_str("version").c_str(), persona_ids.items().size(),
       skill_names.items().size());
  return rec;
}

bool uninstall(const std::string& id, std::string& error) {
  Json s = settings::load();
  Json rec;
  Json keep = Json::array();
  for (const Json& e : s["extensions"].items()) {
    if (e.get_str("id") == id) rec = e;
    else keep.push(e);
  }
  if (rec.is_null()) {
    error = "没装过这个包";
    return false;
  }
  std::set<std::string> pids, skills;
  for (const Json& p : rec["personas"].items()) pids.insert(p.str());
  for (const Json& k : rec["skills"].items()) skills.insert(k.str());
  Json personas = Json::array();
  for (Json p : s["personas"].items()) {
    if (pids.count(p.get_str("id"))) continue;
    Json left = Json::array();
    for (const Json& k : p["skills"].items())
      if (!skills.count(k.str())) left.push(k);
    p.set("skills", left);
    personas.push(p);
  }
  s.set("personas", personas);
  if (pids.count(s.get_str("active"))) s.set("active", "default");
  for (const std::string& k : skills) {
    if (!std::regex_match(k, kNameRe)) continue;  // 记录里的名字也要合规, 才拼进路径
    std::error_code ec;
    std::filesystem::remove_all(std::filesystem::path(settings::skills_dir() + L"\\" + widen(k)), ec);
  }
  s.set("extensions", keep);
  if (!settings::save(s)) {
    error = "设置文件写不进去";
    return false;
  }
  logf("扩展: 卸载了内容包 %s", id.c_str());
  return true;
}

Json export_pack(const Json& req, std::string& error) {
  std::string id = req.get_str("id"), version = req.get_str("version", "1.0.0");
  if (!std::regex_match(id, kIdRe)) {
    error = "包的 id 只能用小写字母、数字、. _ -, 最多 64 个字";
    return Json();
  }
  if (req.get_str("name").empty() || req.get_str("license").empty()) {
    error = "要填名字和许可";
    return Json();
  }
  Json s = settings::load(), files = Json::array(), personas_out = Json::array(), skills_out = Json::array();
  auto add = [&](const std::string& path, const std::string& body) {
    Json f = Json::object();
    f.set("path", path).set("sha256", sha256_hex(body)).set("text", body);
    files.push(f);
  };
  int n = 0;
  for (const Json& want : req["personas"].items())
    for (const Json& p : s["personas"].items())
      if (p.get_str("id") == want.str()) {
        Json clean = clean_persona(p, error);
        if (clean.is_null()) return Json();
        add("personas/p" + std::to_string(++n) + ".json", clean.dump());
        personas_out.push(clean.get_str("name"));
      }
  Json all = settings::scan_skills();
  for (const Json& want : req["skills"].items()) {
    const Json* sk = nullptr;
    for (const Json& k : all.items())
      if (k.get_str("name") == want.str()) sk = &k;
    if (!sk || !std::regex_match(want.str(), kNameRe)) {
      error = "没有这个 skill (或名字不合规): " + want.str();
      return Json();
    }
    int count = 0;
    WIN32_FIND_DATAW fd;
    std::wstring dir = widen(sk->get_str("dir"));
    HANDLE h = FindFirstFileW((dir + L"\\*").c_str(), &fd);
    if (h != INVALID_HANDLE_VALUE) {
      do {
        std::string file = utf8(fd.cFileName);
        if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || !std::regex_match(file, kFileRe)) continue;
        std::vector<char> data;
        if (!read_file(dir + L"\\" + fd.cFileName, data) || data.size() > kMaxFile || ++count > int(kMaxSkillFiles)) continue;
        add("skills/" + want.str() + "/" + file, std::string(data.begin(), data.end()));
      } while (FindNextFileW(h, &fd));
      FindClose(h);
    }
    skills_out.push(want.str());
  }
  if (files.items().empty()) {
    error = "什么都没选";
    return Json();
  }
  Json m = Json::object(), contains = Json::object();
  contains.set("personas", personas_out).set("skills", skills_out);
  m.set("id", id).set("name", req.get_str("name")).set("version", version).set("type", "content").set("author", req.get_str("author"));
  m.set("license", req.get_str("license")).set("description", req.get_str("description")).set("contains", contains);
  Json doc = Json::object();
  doc.set("wmext", 1).set("manifest", m).set("files", files);
  std::string text = doc.dump();
  std::string check;
  Pack pk;
  if (!parse(text, pk, check)) {  // 导出来的自己也要能导回去
    error = "导出的包自己检查没过: " + check;
    return Json();
  }
  CreateDirectoryW(exports_dir().c_str(), nullptr);
  std::wstring path = exports_dir() + L"\\" + widen(id + "-" + version) + L".wmext";
  if (!write_text(path, text)) {
    error = "写不进 " + utf8(path);
    return Json();
  }
  logf("扩展: 导出了内容包 %s %s (%zu 字节)", id.c_str(), version.c_str(), text.size());
  Json out = Json::object();
  out.set("path", utf8(path)).set("size", double(text.size())).set("sha256", sha256_hex(text));
  return out;
}

Json audit_tail(int limit) {
  std::vector<char> data;
  Json out = Json::array();
  if (!read_file(settings::audit_path(), data)) return out;
  std::string text(data.begin(), data.end());
  std::vector<std::string> lines;
  for (size_t a = 0, b; a < text.size(); a = b + 1) {
    b = text.find('\n', a);
    if (b == std::string::npos) b = text.size();
    if (b > a) lines.push_back(text.substr(a, b - a));
  }
  for (int i = int(lines.size()) - 1, n = 0; i >= 0 && n < limit; i--) {
    Json line;
    if (Json::parse(lines[i], line)) {
      out.push(line);
      n++;
    }
  }
  return out;
}

}  // namespace extpack
