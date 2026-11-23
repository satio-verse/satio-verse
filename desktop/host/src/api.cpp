#include "api.h"

#include <bcrypt.h>

#include <chrono>
#include <cstdlib>
#include <ctime>

#include "app.h"

namespace {

const long long kStarted = static_cast<long long>(time(nullptr));

std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
  return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

// 逐字节比较完, 不因为前面不同就提前返回 (不让对方按耗时猜 token)
bool same_secret(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  unsigned char diff = 0;
  for (size_t i = 0; i < a.size(); i++) diff |= static_cast<unsigned char>(a[i] ^ b[i]);
  return diff == 0;
}

bool local_authority(const std::string& v, int port) {
  std::string p = ":" + std::to_string(port);
  return v == "127.0.0.1" + p || v == "localhost" + p;
}

Json done_error(int status, const std::string& error) {
  Json e = Json::object();
  e.set("ev", "done").set("ok", false).set("status", status).set("error", error);
  return e;
}

// OpenAI 的错误格式: {"error":{"message","type","param","code"}}
void openai_error(HttpConn& conn, int status, const std::string& message, const char* param = nullptr, const char* code = nullptr) {
  Json e = Json::object();
  e.set("message", message).set("type", status >= 500 ? "server_error" : "invalid_request_error");
  e.set("param", param ? Json(param) : Json()).set("code", code ? Json(code) : Json());
  Json body = Json::object();
  body.set("error", e);
  conn.respond(status, "application/json; charset=utf-8", body.dump());
}

// 服务端自己记对话上下文, 这里只取最后一条 user 消息 (system 和之前的历史都不用); content 可以是字符串, 也可以是 [{type:"text",text}] 数组
std::string last_user_text(const Json& messages) {
  const auto& items = messages.items();
  for (auto it = items.rbegin(); it != items.rend(); ++it) {
    if ((*it).get_str("role") != "user") continue;
    const Json& c = (*it)["content"];
    if (c.is_string()) return trim(c.str());
    std::string text;
    for (const Json& part : c.items())
      if (part.get_str("type") == "text") text += part.get_str("text");
    return trim(text);
  }
  return "";
}

}  // namespace

std::wstring api_token_path() {
  const wchar_t* appdata = _wgetenv(L"APPDATA");
  return std::wstring(appdata ? appdata : L".") + L"\\WorldModel\\pet-api-token.txt";
}

std::wstring api_guest_token_path() {
  const wchar_t* appdata = _wgetenv(L"APPDATA");
  return std::wstring(appdata ? appdata : L".") + L"\\WorldModel\\pet-api-guest-token.txt";
}

bool load_or_create_token(std::string& token) { return load_or_create_token(token, api_token_path()); }

bool load_or_create_token(std::string& token, const std::wstring& path) {
  std::vector<char> data;
  if (read_file(path, data)) {
    token = trim(std::string(data.begin(), data.end()));
    if (token.size() >= 32) return true;
  }
  unsigned char raw[32];
  if (!BCRYPT_SUCCESS(BCryptGenRandom(nullptr, raw, sizeof raw, BCRYPT_USE_SYSTEM_PREFERRED_RNG))) return false;
  static const char* hex = "0123456789abcdef";
  token.clear();
  for (unsigned char b : raw) {
    token += hex[b >> 4];
    token += hex[b & 15];
  }
  CreateDirectoryW(path.substr(0, path.rfind(L'\\')).c_str(), nullptr);
  HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (f == INVALID_HANDLE_VALUE) return false;
  DWORD written = 0;
  WriteFile(f, token.data(), DWORD(token.size()), &written, nullptr);
  CloseHandle(f);
  logf("生成了本机接口的 token: %s", utf8(path).c_str());
  return written == token.size();
}

bool PetApi::start(int port) {
  {
    std::lock_guard<std::mutex> lock(mu_);
    stopping_ = false;
  }
  if (!load_or_create_token(token_) || !load_or_create_token(guest_token_, api_guest_token_path())) {
    logf("本机接口没开: token 文件读写失败 (%s)", utf8(api_token_path()).c_str());
    return false;
  }
  if (!http_.start(port, [this](const HttpRequest& r, HttpConn& c) { handle(r, c); })) return false;
  logf("本机接口已开: http://127.0.0.1:%d/v1 (OpenAI 兼容) 和 /mcp (MCP), token 在 %s", port, utf8(api_token_path()).c_str());
  return true;
}

void PetApi::stop() {
  {
    std::lock_guard<std::mutex> lock(mu_);
    stopping_ = true;
    for (auto& [id, c] : calls_) {
      std::lock_guard<std::mutex> l(c->mu);
      c->events.push_back(done_error(503, "桌宠退出了"));
      c->cv.notify_all();
    }
  }
  http_.stop();
}

void PetApi::handle(const HttpRequest& req, HttpConn& conn) {
  int port = http_.port();
  // 防 DNS 重绑定: 网页把自己的域名解析到 127.0.0.1 也连得进来, 但 Host 是它的域名、Origin 是它的网址
  if (!local_authority(req.header("host"), port)) {
    conn.respond(403, "text/plain; charset=utf-8", "Host 不对: 只接受 127.0.0.1 或 localhost");
    return;
  }
  std::string origin = req.header("origin");
  if (!origin.empty() && !local_authority(origin.rfind("http://", 0) == 0 ? origin.substr(7) : std::string(), port)) {
    logf("本机接口: 拒绝了来自 %s 的请求 (Origin 不是本机)", origin.c_str());
    conn.respond(403, "text/plain; charset=utf-8", "Origin 不对: 不接受网页里发来的请求");
    return;
  }
  std::string auth = req.header("authorization");
  std::string given = auth.rfind("Bearer ", 0) == 0 ? trim(auth.substr(7)) : std::string();
  bool owner = !given.empty() && same_secret(given, token_);
  bool guest = !owner && !given.empty() && same_secret(given, guest_token_);  // 访客 token: 只有 wm-guest
  if (!owner && !guest) {
    conn.respond(401, "text/plain; charset=utf-8", "要 Authorization: Bearer <token>, token 在 " + utf8(api_token_path()),
                 "WWW-Authenticate: Bearer\r\n");
    return;
  }
  if (req.path == "/v1/models" && req.method == "GET") {
    models(conn, guest);
  } else if (req.path == "/v1/chat/completions" && req.method == "POST") {
    chat(req, conn, guest);
  } else if (req.path == "/mcp" && guest) {
    logf("本机接口: 访客 token 想用 /mcp, 拒绝了");
    conn.respond(403, "text/plain; charset=utf-8", "访客 token 不能用 MCP (只能用 wm-guest)");
  } else if (req.path == "/mcp") {
    mcp(req, conn);
  } else if (req.path == "/v1/models" || req.path == "/v1/chat/completions") {
    conn.respond(405, "text/plain; charset=utf-8", "方法不对", req.path == "/v1/models" ? "Allow: GET\r\n" : "Allow: POST\r\n");
  } else {
    openai_error(conn, 404, "没有这个接口: " + req.path + " (有 /v1/models、/v1/chat/completions、/mcp)");
  }
}

void PetApi::models(HttpConn& conn, bool guest) {
  Json data = Json::array();
  for (const char* id : {"wm-pet", "wm-text", "wm-guest"}) {
    if (guest && std::string(id) != "wm-guest") continue;
    Json m = Json::object();
    m.set("id", id).set("object", "model").set("created", double(kStarted)).set("owned_by", "worldmodel");
    data.push(m);
  }
  Json body = Json::object();
  body.set("object", "list").set("data", data);
  conn.respond(200, "application/json; charset=utf-8", body.dump());
}

// 三个模型: wm-pet 是桌面上的她 (开口说、带表情和手势, 和桌面上的对话共用上下文); wm-text 只回文字 (另一条对话, 不出声、不动);
// wm-guest 是访客 (服务端的 /guest: 没有工具、读不到主人的记忆、不写记忆, 按 user 分会话). 访客 token 一律按 wm-guest
void PetApi::chat(const HttpRequest& req, HttpConn& conn, bool guest) {
  Json body;
  std::string err;
  if (!Json::parse(req.body, body, &err) || !body.is_object()) {
    openai_error(conn, 400, "请求体不是 JSON 对象: " + err);
    return;
  }
  std::string model = guest ? "wm-guest" : body.get_str("model");
  if (model != "wm-pet" && model != "wm-text" && model != "wm-guest") {
    openai_error(conn, 404, "没有这个模型: " + model + " (有 wm-pet、wm-text、wm-guest)", "model", "model_not_found");
    return;
  }
  std::string text = last_user_text(body["messages"]);
  if (text.empty()) {
    openai_error(conn, 400, "messages 里没有 user 消息", "messages");
    return;
  }
  bool pet = model == "wm-pet", stream = body["stream"].boolean();
  if (pet && hidden()) {
    openai_error(conn, 409, "桌宠现在隐藏着 (用户隐藏了, 或者有全屏程序), 用 wm-text, 或者先显示她", "model", "pet_hidden");
    return;
  }
  logf("本机接口: 聊天 %s%s%s, %zu 字节", model.c_str(), guest ? " (访客 token)" : "", stream ? " (流式)" : "", text.size());
  static std::atomic<int> n{0};
  std::string cid = "chatcmpl-wm" + std::to_string(kStarted) + "-" + std::to_string(++n);
  double created = double(time(nullptr));
  Json page = Json::object();
  page.set("kind", "chat").set("mode", pet ? "pet" : model == "wm-guest" ? "guest" : "text").set("text", text);
  // 调用方带来的工具 (AstrBot 人格里开着工具时会带): 这几个模型都不用 (wm-guest 根本没有工具, 回复里也不会有 tool_calls),
  // 记一笔; wm-guest 的计数给面板提示"去它那边关掉"
  const Json& choice = body["tool_choice"];
  size_t ntools = body["tools"].items().size() + body["functions"].items().size();
  bool tooled = ntools > 0 || (choice.is_string() ? choice.str() != "none" : choice.is_object());
  if (tooled) logf("本机接口: %s 请求里带了 %zu 个工具, 不用 (调用方那边开着函数调用)", model.c_str(), ntools);
  if (model == "wm-guest") {  // 调用方的用户 id (OpenAI 的 user 字段): 访客会话按它分; 截到 64 字节, 不切开 UTF-8 的一个字
    guest_requests_++;
    if (tooled) guest_tools_++;
    std::string user = body.get_str("user");
    if (user.empty()) {  // AstrBot 不带 user, 会话号放在这个头里 (它的 /new 会换一个, 我们这边也跟着换一个访客会话)
      std::string conv = trim(req.header("x-astrbot-conversation-id"));
      if (!conv.empty()) user = "astrbot:" + conv;
    }
    size_t cut = std::min<size_t>(user.size(), 64);
    while (cut > 0 && cut < user.size() && (static_cast<unsigned char>(user[cut]) & 0xC0) == 0x80) cut--;
    page.set("user", user.substr(0, cut));
  }

  // wm-guest 不回 (外发检查拦下、她选了不说) 时回复是空的: 结束原因标成 content_filter. AstrBot 收到 stop 加空内容会当成
  // 模型出错, 同一句连问三次 (每次都进访客会话); content_filter 它不重试, 只给对方发人格里的"出错时回复"
  auto finish_of = [&](const Json& r) { return model == "wm-guest" && r.get_str("reply").empty() ? "content_filter" : "stop"; };
  if (!stream) {
    Json r = call(page);
    if (!r["ok"].boolean()) {
      openai_error(conn, int(r["status"].number(500)), r.get_str("error", "出错了"));
      return;
    }
    Json msg = Json::object();
    msg.set("role", "assistant").set("content", r.get_str("reply"));
    Json choice = Json::object();
    choice.set("index", 0).set("message", msg).set("finish_reason", finish_of(r));
    Json choices = Json::array();
    choices.push(choice);
    Json out = Json::object();
    out.set("id", cid).set("object", "chat.completion").set("created", created).set("model", model).set("choices", choices);
    Json wm = Json::object();  // 扩展字段: 这一轮的情绪、动作 (OpenAI 的客户端会忽略)
    wm.set("emotion", r["emo"]).set("actions", r["acts"]).set("interrupted", r["interrupted"].boolean());
    out.set("wm", wm);
    conn.respond(200, "application/json; charset=utf-8", out.dump());
    return;
  }

  // 流式: 等到第一段回复 (或者结束) 才发 200 和事件流头, 前面出错时还能回正常的错误状态码
  bool started = false;
  auto chunk = [&](Json delta, Json finish) {
    Json choice = Json::object();
    choice.set("index", 0).set("delta", delta).set("finish_reason", finish);
    Json choices = Json::array();
    choices.push(choice);
    Json c = Json::object();
    c.set("id", cid).set("object", "chat.completion.chunk").set("created", created).set("model", model).set("choices", choices);
    return conn.sse(c.dump());
  };
  auto begin = [&] {
    if (started) return conn.alive();
    started = true;
    Json d = Json::object();
    d.set("role", "assistant").set("content", "");
    return conn.start_sse() && chunk(d, Json());
  };
  Json r = call(page, [&](const std::string& piece) {
    Json d = Json::object();
    d.set("content", piece);
    return begin() && chunk(d, Json());
  });
  if (!r["ok"].boolean() && !started) {
    openai_error(conn, int(r["status"].number(500)), r.get_str("error", "出错了"));
    return;
  }
  if (!begin()) return;
  if (r["ok"].boolean()) {
    chunk(Json::object(), finish_of(r));
  } else {
    Json e = Json::object();
    e.set("message", r.get_str("error", "出错了")).set("type", "server_error");
    Json wrap = Json::object();
    wrap.set("error", e);
    conn.sse(wrap.dump());
  }
  conn.sse("[DONE]");
}

Json PetApi::call(Json req, const std::function<bool(const std::string&)>& on_delta, int timeout_s) {
  {
    std::lock_guard<std::mutex> lock(app_.mu);
    if (!app_.page_ready) return done_error(503, "桌宠页面还没准备好 (角色还在加载, 或者没连上服务)");
  }
  auto c = std::make_shared<Call>();
  int id;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (stopping_) return done_error(503, "桌宠退出了");
    id = ++seq_;
    calls_[id] = c;
  }
  req.set("t", "api").set("id", id);
  std::wstring msg = widen(req.dump());
  app_.ui([&] { app_.web.post_json(msg); });
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_s);
  Json result;
  bool finished = false, gone = false;
  while (!finished && !gone) {
    std::deque<Json> events;
    {
      std::unique_lock<std::mutex> lock(c->mu);
      if (!c->cv.wait_until(lock, deadline, [&] { return !c->events.empty(); })) break;
      events.swap(c->events);
    }
    for (Json& e : events) {
      std::string ev = e.get_str("ev");
      if (ev == "delta" && on_delta && !gone) {
        gone = !on_delta(e.get_str("text"));
      } else if (ev == "done") {
        result = std::move(e);
        finished = true;
        break;
      }
    }
  }
  {
    std::lock_guard<std::mutex> lock(mu_);
    calls_.erase(id);
  }
  if (finished) return result;
  // 超时, 或者调用方断开了: 告诉页面不用再做 (正在说的这一轮会被打断)
  Json cancel = Json::object();
  cancel.set("t", "api").set("kind", "cancel").set("id", id);
  std::wstring cmsg = widen(cancel.dump());
  app_.ui([&] { app_.web.post_json(cmsg); });
  return gone ? done_error(499, "调用方断开了") : done_error(504, "等了 " + std::to_string(timeout_s) + " 秒没有结果");
}

void PetApi::on_page_event(const std::string& msg) {
  size_t sp = msg.find(' ');
  int id = atoi(msg.c_str());
  Json ev;
  if (sp == std::string::npos || !Json::parse(msg.substr(sp + 1), ev) || !ev.is_object()) return;
  std::shared_ptr<Call> c;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = calls_.find(id);
    if (it == calls_.end()) return;  // 已经超时或者断开了
    c = it->second;
  }
  std::lock_guard<std::mutex> lock(c->mu);
  c->events.push_back(std::move(ev));
  c->cv.notify_all();
}

bool PetApi::hidden() {
  bool h = false;
  app_.ui([&] { h = app_.user_hidden || app_.auto_hidden; });
  return h;
}

Json PetApi::guest_stats() const {
  Json g = Json::object();
  g.set("requests", double(guest_requests_.load())).set("with_tools", double(guest_tools_.load()));
  return g;
}

Json PetApi::host_state() {
  Json s = Json::object();
  s.set("guest", guest_stats());
  app_.ui([&] {
    App& a = app_;
    s.set("visible", !a.user_hidden && !a.auto_hidden).set("hidden_by_user", a.user_hidden).set("hidden_for_fullscreen", a.auto_hidden);
    s.set("mic_on", a.mic_on).set("muted", a.muted).set("paused", a.paused).set("idle_fps", a.fps);
    s.set("place", a.stand_on ? "window_top" : (a.dragging || a.falling) ? "moving" : "taskbar");
  });
  return s;
}
