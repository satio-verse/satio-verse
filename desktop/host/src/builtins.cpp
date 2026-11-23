#include "builtins.h"

#include <windows.h>
#include <shellapi.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <ctime>

#include "settings.h"
#include "autonomy.h"
#include "util.h"

namespace builtins {
namespace {

struct Def {
  const char* name;
  const char* group;
  const char* title;
};
const Def kDefs[] = {
    {"remind", "remind", "提醒"}, {"cancel_reminder", "remind", "取消提醒"}, {"room_do", "room_do", "在小屋里做事"},
    {"open_url", "open_url", "打开网址"}, {"room_capabilities", "room_do", "查询小屋活动"},
};

// Protocol vocabulary only. Query live room_capabilities before selecting an activity.
const char* kActs[][2] = {
    {"computer", "使用房间里的虚拟电脑"}, {"read", "在阅读椅休息"}, {"rest", "坐床边歇一会"}, {"coffee", "冲咖啡喝"},
    {"window", "看窗外"},           {"books", "在书架前挑书"},  {"visit", "走过来看用户"}, {"drink", "从冰箱拿饮料喝"},
    {"snack", "吃零食"},            {"milk", "喝牛奶"},         {"yoga", "做瑜伽"},       {"sleep", "上床睡觉"},
    {"rearrange", "挪一挪家具"},    {"tidy", "收拾东西"},
    {"phone", "查看房间手机"}, {"chair_turn", "只转动椅子方向，不是翻倒椅子"},
    {"inspect_temperature", "检查杯子温度"}, {"explore", "探索房间"},
};

constexpr size_t kMaxReminders = 50;
constexpr double kMaxDays = 30;

std::wstring reminders_path() {
  std::wstring p = settings::path();
  size_t slash = p.find_last_of(L"\\/");
  return (slash == std::wstring::npos ? std::wstring(L".") : p.substr(0, slash)) + L"\\pet-reminders.json";
}

bool enabled(const Json& persona, const std::string& grp) {
  if (!persona["builtins"].is_array()) return true;  // 没写 = 都启用
  for (const Json& g : persona["builtins"].items())
    if (g.str() == grp) return true;
  return false;
}

Json prop(const char* type, const std::string& desc) {
  Json p = Json::object();
  p.set("type", type).set("description", desc);
  return p;
}

Json tool(const char* name, const std::string& desc, Json props, Json required) {
  Json params = Json::object();
  params.set("type", "object").set("properties", std::move(props));
  if (!required.items().empty()) params.set("required", std::move(required));
  Json d = Json::object();
  d.set("name", name).set("description", desc).set("parameters", params);
  return d;
}

std::string lower(std::string s) {
  for (auto& c : s) c = char(tolower(static_cast<unsigned char>(c)));
  return s;
}

// %xx 解开 (查关键词用, 不拿去打开)
std::string percent_decode(const std::string& s) {
  std::string out;
  for (size_t i = 0; i < s.size(); i++) {
    if (s[i] == '%' && i + 2 < s.size() && isxdigit(static_cast<unsigned char>(s[i + 1])) && isxdigit(static_cast<unsigned char>(s[i + 2]))) {
      out += char(strtol(s.substr(i + 1, 2).c_str(), nullptr, 16));
      i += 2;
    } else {
      out += s[i];
    }
  }
  return out;
}

bool private_host(const std::string& host) {
  if (host == "localhost" || (host.size() > 10 && host.compare(host.size() - 10, 10, ".localhost") == 0)) return true;
  if (host == "[::1]" || host == "0.0.0.0" || host.rfind("[fe80", 0) == 0 || host.rfind("[fc", 0) == 0 || host.rfind("[fd", 0) == 0) return true;
  int a = -1, b = -1, c = -1, d = -1;
  if (sscanf(host.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) == 4) {
    if (a == 127 || a == 10 || a == 0 || (a == 192 && b == 168) || (a == 169 && b == 254) || (a == 172 && b >= 16 && b <= 31)) return true;
  }
  return false;
}

// "YYYY-MM-DD HH:MM" 或 "HH:MM" (今天的这个点; 已经过了就是明天) -> 本地时间的 time_t; 认不得返回 -1
double parse_at(const std::string& at) {
  int y = 0, mo = 0, d = 0, h = -1, mi = -1;
  std::tm tm{};
  time_t now = time(nullptr);
  localtime_s(&tm, &now);
  if (sscanf(at.c_str(), "%d-%d-%d %d:%d", &y, &mo, &d, &h, &mi) == 5 || sscanf(at.c_str(), "%d-%d-%dT%d:%d", &y, &mo, &d, &h, &mi) == 5) {
    tm.tm_year = y - 1900;
    tm.tm_mon = mo - 1;
    tm.tm_mday = d;
  } else if (sscanf(at.c_str(), "%d:%d", &h, &mi) != 2) {
    return -1;
  }
  if (h < 0 || h > 23 || mi < 0 || mi > 59) return -1;
  tm.tm_hour = h;
  tm.tm_min = mi;
  tm.tm_sec = 0;
  tm.tm_isdst = -1;
  time_t t = mktime(&tm);
  if (t == time_t(-1)) return -1;
  if (!y && t <= now) t += 24 * 3600;  // 只给了几点: 已经过了就是明天
  return double(t);
}

}  // namespace

bool known(const std::string& name) {
  for (const Def& d : kDefs)
    if (name == d.name) return true;
  return false;
}

std::string group(const std::string& name) {
  for (const Def& d : kDefs)
    if (name == d.name) return d.group;
  return "";
}

std::string title(const std::string& name) {
  for (const Def& d : kDefs)
    if (name == d.name) return d.title;
  return name;
}

Json declare(const Json& settings, const Json& persona, bool unrestricted) {
  Json out = Json::array();
  auto on = [&](const char* grp) { return enabled(persona, grp) && permission(settings, grp) != "off"; };
  if (on("remind")) {
    Json p = Json::object();
    p.set("text", prop("string", "到点要提醒用户的事, 最多 200 字"));
    p.set("in_minutes", prop("number", "几分钟后提醒 (和 at 二选一), 比如 10"));
    p.set("at", prop("string", "什么时候提醒 (本地时间, 和 in_minutes 二选一): \"YYYY-MM-DD HH:MM\", 或者只写 \"HH:MM\" (今天这个点, 已经过了就是明天)"));
    Json req = Json::array();
    req.push("text");
    out.push(tool("remind", "让桌宠到点提醒用户 (在用户电脑上本机定时, 到点她在对话栏里说). 最多 30 天以内. 返回提醒的时间和编号; 用户问起有哪些提醒时用 cancel_reminder 不带编号来列.", p, req));
    Json c = Json::object();
    c.set("id", prop("string", "要取消的提醒编号 (remind 返回的, 比如 r3); 不给就只列出现有的提醒"));
    out.push(tool("cancel_reminder", "取消一个提醒, 或者列出现有的提醒.", c, Json::array()));
  }
  if (on("room_do")) {
    Json ids = Json::array();
    std::string list;
    for (const auto& a : kActs) {
      ids.push(a[0]);
      list += std::string(list.empty() ? "" : ", ") + a[0] + " " + a[1];
    }
    Json act = prop("string", "做什么: " + list);
    act.set("enum", ids);
    Json p = Json::object();
    p.set("act", act);
    Json timeout = prop("number", "等待终态的秒数，默认180，范围1-180；超时不会当作成功，不能以此等待八分钟冷却");
    timeout.set("minimum", 1).set("maximum", 180);
    p.set("timeout_s", timeout);
    out.push(tool("room_capabilities", "查询当前小屋实际活动能力与不可用原因。静态room_do枚举不是当前能力，先查询再选择。", Json::object(), Json::array()));
    Json req = Json::array();
    req.push("act");
    out.push(tool("room_do", "请求当前小屋活动并等待完成或失败回执。先查room_capabilities，只有enabled活动可用；已排队不是完成，流程完成不证明所有物理质量合格。不会操作现实电脑或真实手机。", p, req));
  }
  if (on("open_url")) {
    Json p = Json::object();
    p.set("url", prop("string", "要打开的网址, 必须是 http:// 或 https:// 开头"));
    Json req = Json::array();
    req.push("url");
    out.push(tool("open_url", unrestricted ? "用用户电脑上的默认浏览器打开 HTTP(S) 网址；全局授权下黑名单优先。" : "用用户电脑上的默认浏览器打开一个网址 (每次都要用户确认). 本机和内网地址、付款页面不开.", p, req));
  }
  return out;
}

bool room_result_ok(bool transport_ok, const std::string& text) {
  Json result;
  if (!(transport_ok && Json::parse(text, result) && result["ok"].boolean(false)
        && result["started"].boolean(false) && result["completed"].boolean(false)))
    return false;
  // 新页面还回传 outcome / cleanupConfirmed / cancelled: 带了任何一项, 就和 wm/room_action.py 的 terminal 一样严格,
  // 要收尾确认过、没取消、outcome 是 completed, 矛盾的回执不算完成. 旧页面不带这三项, 照旧只看上面三个.
  if (!result.has("outcome") && !result.has("cleanupConfirmed") && !result.has("cancelled")) return true;
  const Json &cleanup = result["cleanupConfirmed"], &cancelled = result["cancelled"], &outcome = result["outcome"];
  return cleanup.type() == Json::Bool && cleanup.boolean()
      && (!result.has("cancelled") || (cancelled.type() == Json::Bool && !cancelled.boolean()))
      && (!result.has("outcome") || (outcome.is_string() && outcome.str() == "completed"));
}

int room_timeout_ms(const Json& args) {
  double seconds = args["timeout_s"].number(180);
  if (!std::isfinite(seconds)) seconds = 180;
  return int(std::clamp(seconds, 1.0, 180.0) * 1000);
}

std::string permission(const Json& settings, const std::string& name) {
  std::string grp = group(name).empty() ? name : group(name);
  std::string p = settings["builtins"].get_str(grp);
  if (grp == "open_url") return p == "off" ? "off" : "ask";  // 网址每次确认, 不给"总是允许"
  if (p == "auto" || p == "ask" || p == "off") return p;
  return "auto";
}

Json options(const std::string& name) {
  Json o = Json::array();
  o.push("once");
  if (group(name) != "open_url") o.push("always");
  o.push("deny");
  return o;
}

std::string check_url(const std::string& url) {
  if (url.empty()) return "没给网址";
  if (url.size() > 2048) return "网址太长";
  for (char c : url)
    if (static_cast<unsigned char>(c) < 0x21) return "网址里不能有空格和控制字符";
  std::string u = lower(url);
  size_t start = u.rfind("https://", 0) == 0 ? 8 : u.rfind("http://", 0) == 0 ? 7 : 0;
  if (!start) return "只能打开 http:// 或 https:// 的网址";
  size_t end = u.find_first_of("/?#", start);
  std::string host = u.substr(start, end == std::string::npos ? std::string::npos : end - start);
  if (host.find('@') != std::string::npos) return "带用户名密码的网址不开";
  if (host.empty() || host[0] != '[') host = host.substr(0, host.find(':'));
  else host = host.substr(0, host.find(']') + 1);
  if (host.empty()) return "网址不对";
  if (private_host(host)) return "本机和内网的网页 (机器人后台、路由器管理页这类) 要你自己开";
  std::string all = lower(percent_decode(url));
  for (const char* w : {"snowluma", "napcat", "astrbot"})
    if (all.find(w) != std::string::npos) return "QQ 机器人的后台要你自己开";
  for (const char* w : {"pay", "checkout", "cashier", "alipay", "bank", "支付", "收银", "付款", "转账"})
    if (all.find(w) != std::string::npos) return "像是付款、银行的页面, 要你自己开";
  return "";
}

bool run(const std::string& name, const Json& args, bool dry_open, std::string& text, bool unrestricted) {
  if (name == "remind") {
    std::string error;
    Json r = reminders().add(args.get_str("text"), args["in_minutes"], args.get_str("at"), error);
    if (r.is_null()) {
      text = error;
      return false;
    }
    text = "好了, " + r.get_str("due") + " 提醒用户: " + r.get_str("text") + " (编号 " + r.get_str("id") + ")";
    return true;
  }
  if (name == "cancel_reminder") {
    std::string id = args.get_str("id");
    if (!id.empty() && !reminders().cancel(id)) {
      text = "没有编号是 " + id + " 的提醒";
      return false;
    }
    Json list = reminders().list();
    text = id.empty() ? "" : "取消了 " + id + ". ";
    if (list.items().empty()) text += "现在没有提醒.";
    else {
      text += "现在的提醒:";
      for (const Json& r : list.items()) text += "\n- " + r.get_str("id") + " " + r.get_str("due") + ": " + r.get_str("text");
    }
    return true;
  }
  if (name == "open_url") {
    std::string url = args.get_str("url");
    std::string why = unrestricted ? autonomy::url_error(url) : check_url(url);
    if (!why.empty()) {
      text = why;
      return false;
    }
    if (dry_open) {  // 自检: 不真开浏览器
      logf("内置工具: 打开网址 (自检, 没真开)");
      text = "打开了 " + url + " (自检没真开)";
      return true;
    }
    HRESULT co = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    INT_PTR rc = reinterpret_cast<INT_PTR>(ShellExecuteW(nullptr, L"open", widen(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL));
    if (SUCCEEDED(co)) CoUninitialize();
    if (rc <= 32) {
      text = "浏览器没打开 (错误 " + std::to_string(rc) + ")";
      return false;
    }
    // 浏览器多半已经开着, 新标签页在后台打开, Windows 不让桌宠把它抢到前台; 网页模式 (Snapshot use_dom) 只读前台的浏览器
    text = "在用户的浏览器里打开了 " + url + ". 浏览器窗口可能还在别的窗口后面: 要看网页内容, 先用 App (mode=\"switch\", name=\"" +
           default_browser() + "\") 把它切到前台, 再 Snapshot (use_dom=true)";
    return true;
  }
  text = "不认识这个工具: " + name;
  return false;
}

std::string default_browser() {
  wchar_t prog[128] = L"";
  DWORD size = sizeof prog;
  RegGetValueW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\https\\UserChoice", L"ProgId",
               RRF_RT_REG_SZ, nullptr, prog, &size);
  std::wstring p = prog;
  if (p.rfind(L"Firefox", 0) == 0) return "Firefox";
  if (p.rfind(L"Chrome", 0) == 0) return "Chrome";
  if (p.rfind(L"MSEdge", 0) == 0) return "Edge";
  return "浏览器";
}

std::string local_time(double epoch_s) {
  time_t t = time_t(epoch_s);
  std::tm tm{};
  localtime_s(&tm, &t);
  static const char* week[] = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
  char b[64];
  snprintf(b, sizeof b, "%d 月 %d 日 %02d:%02d (%s)", tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, week[tm.tm_wday]);
  return b;
}

Reminders& reminders() {
  static Reminders r;
  return r;
}

void Reminders::start(Fire fire) {
  std::lock_guard<std::mutex> lock(mu_);
  if (started_) return;
  started_ = true;
  fire_ = std::move(fire);
  load();
  thread_ = std::thread([this] { loop(); });
  thread_.detach();
}

void Reminders::load() {
  if (loaded_) return;
  loaded_ = true;
  std::vector<char> data;
  Json parsed;
  std::string text(data.begin(), data.end());
  if (read_file(reminders_path(), data)) text.assign(data.begin(), data.end());
  if (text.rfind("\xEF\xBB\xBF", 0) == 0) text.erase(0, 3);  // 别的编辑器存的可能带 BOM
  if (Json::parse(text, parsed) && parsed["items"].is_array()) {
    items_ = parsed["items"];
    seq_ = int(parsed["seq"].number(0));
  }
}

void Reminders::save() {
  Json doc = Json::object();
  doc.set("seq", seq_).set("items", items_);
  std::string text = doc.dump();
  std::wstring path = reminders_path(), tmp = path + L".tmp";
  if (FILE* f = _wfopen(tmp.c_str(), L"wb")) {
    fwrite(text.data(), 1, text.size(), f);
    fclose(f);
    MoveFileExW(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING);
  }
}

Json Reminders::add(const std::string& raw, const Json& in_minutes, const std::string& at, std::string& error) {
  std::string text = raw;
  while (!text.empty() && (text.back() == ' ' || text.back() == '\n')) text.pop_back();
  if (text.empty()) {
    error = "没说要提醒什么";
    return Json();
  }
  if (text.size() > 600) {  // 约 200 个汉字; 不截断 UTF-8 的半个字
    size_t cut = 600;
    while (cut > 0 && (static_cast<unsigned char>(text[cut]) & 0xC0) == 0x80) cut--;
    text = text.substr(0, cut);
  }
  double now = double(time(nullptr)), due;
  if (in_minutes.type() == Json::Number || (in_minutes.is_string() && !in_minutes.str().empty())) {
    double m = in_minutes.is_string() ? atof(in_minutes.str().c_str()) : in_minutes.number();
    if (!(m > 0)) {
      error = "in_minutes 要大于 0";
      return Json();
    }
    due = now + m * 60;
  } else if (!at.empty()) {
    due = parse_at(at);
    if (due < 0) {
      error = "认不得时间 \"" + at + "\", 要写成 \"YYYY-MM-DD HH:MM\" 或 \"HH:MM\"";
      return Json();
    }
  } else {
    error = "in_minutes 和 at 要给一个";
    return Json();
  }
  if (due <= now) {
    error = "这个时间已经过了";
    return Json();
  }
  if (due - now > kMaxDays * 86400) {
    error = "最多只能提醒 30 天以内的事";
    return Json();
  }
  Json item = Json::object();
  {
    std::lock_guard<std::mutex> lock(mu_);
    load();
    if (items_.items().size() >= kMaxReminders) {
      error = "提醒太多了 (最多 50 个), 先取消几个";
      return Json();
    }
    item.set("id", "r" + std::to_string(++seq_)).set("text", text).set("due_s", std::floor(due)).set("created_s", now);
    items_.push(item);
    save();
  }
  cv_.notify_all();
  logf("内置工具: 加了提醒 %s, %s", item.get_str("id").c_str(), local_time(due).c_str());
  Json out = item;
  out.set("due", local_time(due));
  return out;
}

bool Reminders::cancel(const std::string& id) {
  bool found = false;
  {
    std::lock_guard<std::mutex> lock(mu_);
    load();
    auto& v = items_.mut_items();
    auto it = std::remove_if(v.begin(), v.end(), [&](const Json& r) { return r.get_str("id") == id; });
    found = it != v.end();
    v.erase(it, v.end());
    if (found) save();
  }
  cv_.notify_all();
  return found;
}

Json Reminders::list() {
  std::lock_guard<std::mutex> lock(mu_);
  load();
  Json out = Json::array();
  std::vector<Json> v = items_.items();
  std::sort(v.begin(), v.end(), [](const Json& a, const Json& b) { return a["due_s"].number() < b["due_s"].number(); });
  for (Json r : v) {
    r.set("due", local_time(r["due_s"].number()));
    out.push(r);
  }
  return out;
}

void Reminders::loop() {
  std::unique_lock<std::mutex> lock(mu_);
  for (;;) {
    double now = double(time(nullptr)), next = -1;
    std::vector<Json> due;
    auto& v = items_.mut_items();
    for (auto it = v.begin(); it != v.end();) {
      double t = (*it)["due_s"].number();
      if (t <= now) {
        due.push_back(*it);
        it = v.erase(it);
      } else {
        if (next < 0 || t < next) next = t;
        ++it;
      }
    }
    if (!due.empty()) {
      save();
      lock.unlock();
      for (const Json& r : due) {
        logf("内置工具: 提醒到了 %s", r.get_str("id").c_str());
        if (fire_) fire_(r, (now - r["due_s"].number()) / 60.0);
      }
      lock.lock();
      continue;
    }
    // 最多睡 30 秒再看 (改了系统时间、睡眠唤醒后也能补上)
    double wait = next < 0 ? 30 : std::min(30.0, std::max(0.05, next - now));
    cv_.wait_for(lock, std::chrono::milliseconds(int(wait * 1000)));
  }
}

}  // namespace builtins
