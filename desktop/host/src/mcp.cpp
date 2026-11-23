// MCP 服务端 (Streamable HTTP 的 POST /mcp, 只回单个 JSON, 不开事件流) 和 stdio 转发.
// 两代协议都认 (规范里叫 dual-era):
//   2026-07-28 (新): 没有握手和会话, 每个请求带 MCP-Protocol-Version / Mcp-Method / Mcp-Name 头和 _meta 里的版本, 头和请求体不一致回 400 + -32020;
//   2025-11-25 及更早 (旧): 先 initialize 再调用. 不发 Mcp-Session-Id (规范允许服务端不要会话), 所以宿主这边不用存任何状态.
#include "api.h"
#include "app.h"

#include <winhttp.h>  // 要在 windows.h 之后

#include <atomic>
#include <condition_variable>

namespace {

const char* kModern = "2026-07-28";
const char* kLegacy[] = {"2025-11-25", "2025-06-18", "2025-03-26"};
// 和 web/talk.js 的 DESKTOP_SKILLS 一致, 再加身体自己做的 squat (挂了 StayStill 的身体才有, 页面会再查一次)
const char* kSkills[] = {"wave", "raise", "point", "nod", "shake", "shrug", "think", "bow", "squat"};

const char* kInstructions =
    "这是用户桌面上的虚拟角色 (WorldModel 桌宠). pet_chat 让她说话并拿到回复 (voice=false 时只回文字、不出声), "
    "pet_act 让她做一个手势, pet_state 看她现在的状态, pet_show / pet_hide 让她出现或藏起来. "
    "她自己记对话上下文, 每次只传这一句话. 出声前想想用户此刻是否方便 (比如在开会).";

// 工具定义: 写成 JSON 文本, 用到时解析一次
const char* kTools = R"JSON([
 {"name": "pet_chat", "title": "和桌宠说话",
  "description": "对桌宠说一句话 (或问一个问题), 返回她的回复文字. voice=true (默认) 时她在桌面上开口说, 带表情和手势, 和用户在桌面上跟她的对话共用上下文; voice=false 时只回文字, 不出声也不动 (另一条对话, 上下文分开). 讲故事这类长回复要等她说完才返回.",
  "inputSchema": {"type": "object", "properties": {
    "text": {"type": "string", "description": "要对她说的话 (中文)"},
    "voice": {"type": "boolean", "default": true, "description": "是否在桌面上出声说"}},
   "required": ["text"], "additionalProperties": false},
  "annotations": {"readOnlyHint": false, "destructiveHint": false, "openWorldHint": false}},
 {"name": "pet_act", "title": "让桌宠做手势",
  "description": "让桌宠做一个手势 (不说话): wave 挥手, raise 举手, point 指向前方, nod 点头, shake 摇头, shrug 耸肩, think 托腮想, bow 鞠躬, squat 蹲下 (蹲一会儿自己站起来). 桌面上没有房间, 走动、坐下、拿东西做不了.",
  "inputSchema": {"type": "object", "properties": {
    "skill": {"type": "string", "enum": ["wave", "raise", "point", "nod", "shake", "shrug", "think", "bow", "squat"]},
    "side": {"type": "string", "enum": ["L", "R"], "default": "R", "description": "用哪只手 (L 左 / R 右)"}},
   "required": ["skill"], "additionalProperties": false},
  "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}},
 {"name": "pet_state", "title": "桌宠的状态",
  "description": "桌宠现在的状态: 是否显示着、在不在说话、麦克风和静音、情绪、站在任务栏还是窗口顶上、连没连上服务.",
  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
  "annotations": {"readOnlyHint": true, "openWorldHint": false}},
 {"name": "pet_show", "title": "让桌宠出现",
  "description": "让桌宠出现在桌面上 (用户隐藏了她的话). 有全屏程序时她仍然自动藏着, 全屏结束才出现.",
  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
  "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}},
 {"name": "pet_hide", "title": "让桌宠藏起来",
  "description": "让桌宠藏起来 (停止渲染, 麦克风暂停). 用户可以从托盘或者 pet_show 让她回来.",
  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
  "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}}
])JSON";

bool is_legacy(const std::string& v) {
  for (const char* l : kLegacy)
    if (v == l) return true;
  return false;
}

Json supported_versions() {
  Json a = Json::array();
  a.push(kModern);
  for (const char* l : kLegacy) a.push(l);
  return a;
}

Json server_info() {
  Json s = Json::object();
  s.set("name", "worldmodel-pet").set("title", "WorldModel 桌宠").set("version", "0.2.0");
  return s;
}

Json rpc_result(const Json& id, Json result) {
  Json r = Json::object();
  r.set("jsonrpc", "2.0").set("id", id).set("result", std::move(result));
  return r;
}

Json rpc_error(const Json& id, int code, const std::string& message, Json data = Json()) {
  Json e = Json::object();
  e.set("code", code).set("message", message);
  if (!data.is_null()) e.set("data", std::move(data));
  Json r = Json::object();
  r.set("jsonrpc", "2.0").set("id", id).set("error", e);
  return r;
}

Json tool_text(const std::string& text, bool error, Json structured = Json()) {
  Json part = Json::object();
  part.set("type", "text").set("text", text);
  Json content = Json::array();
  content.push(part);
  Json r = Json::object();
  r.set("content", content);
  if (!structured.is_null()) r.set("structuredContent", std::move(structured));
  r.set("isError", error);
  return r;
}

// Mcp-Name 的值不是纯 ASCII 时客户端会写成 =?base64?...?=, 比较前先解出来
std::string header_value(const std::string& v) {
  if (v.size() < 12 || v.rfind("=?base64?", 0) != 0 || v.compare(v.size() - 2, 2, "?=") != 0) return v;
  std::vector<uint8_t> out;
  if (!base64_decode(v.substr(9, v.size() - 11), out)) return v;
  return std::string(out.begin(), out.end());
}

const char* kJson = "application/json; charset=utf-8";

}  // namespace

void PetApi::mcp(const HttpRequest& req, HttpConn& conn) {
  if (req.method != "POST") {  // 不开 GET 的事件流 (新版也去掉了), 也没有会话可以 DELETE
    conn.respond(405, "text/plain; charset=utf-8", "只接受 POST", "Allow: POST\r\n");
    return;
  }
  Json msg;
  std::string err;
  if (!Json::parse(req.body, msg, &err)) {
    conn.respond(400, kJson, rpc_error(Json(), -32700, "Parse error: " + err).dump());
    return;
  }
  if (!msg.is_object()) {
    conn.respond(400, kJson, rpc_error(Json(), -32600, "Invalid Request: 不支持批量请求").dump());
    return;
  }
  if (!msg.has("method")) {  // 客户端对服务端请求的回应: 这里从不向客户端发请求, 收下就行
    conn.respond(202, "", "");
    return;
  }
  const Json& id = msg["id"];
  bool request = msg.has("id");
  std::string method = msg.get_str("method");
  const Json& params = msg["params"];
  std::string version = req.header("mcp-protocol-version");
  bool modern = method != "initialize" && version == kModern;
  if (method != "initialize" && !version.empty() && !modern && !is_legacy(version)) {
    Json data = Json::object();
    data.set("supported", supported_versions()).set("requested", version);
    conn.respond(400, kJson, rpc_error(id, -32022, "Unsupported protocol version", data).dump());
    return;
  }
  if (modern) {  // 新版: 头和请求体必须一致
    std::string why;
    std::string meta_version = params["_meta"].get_str("io.modelcontextprotocol/protocolVersion");
    if (req.header("mcp-method") != method) why = "Mcp-Method 头和 method 不一致";
    else if (request && meta_version != version) why = "MCP-Protocol-Version 头和 _meta 里的版本不一致";
    else if (method == "tools/call" && header_value(req.header("mcp-name")) != params.get_str("name"))
      why = "Mcp-Name 头和 params.name 不一致";
    if (!why.empty()) {
      conn.respond(400, kJson, rpc_error(id, -32020, "HeaderMismatch: " + why).dump());
      return;
    }
  }
  if (!request) {  // 通知 (notifications/initialized、notifications/cancelled 等): 不用回
    conn.respond(202, "", "");
    return;
  }

  Json result;
  if (method == "initialize" || method == "server/discover") {  // 记下是谁连上来的 (排查用; 只记名字和版本)
    const Json& who = method == "initialize" ? params["clientInfo"] : params["_meta"]["io.modelcontextprotocol/clientInfo"];
    logf("MCP: %s %s 连上来了 (%s, 协议 %s)", who.get_str("name", "?").c_str(), who.get_str("version").c_str(), method.c_str(),
         method == "initialize" ? params.get_str("protocolVersion").c_str() : version.c_str());
  }
  if (method == "initialize") {
    std::string want = params.get_str("protocolVersion");
    Json caps = Json::object();
    Json tools = Json::object();
    tools.set("listChanged", false);
    caps.set("tools", tools);
    result = Json::object();
    result.set("protocolVersion", is_legacy(want) ? want : kLegacy[0]).set("capabilities", caps).set("serverInfo", server_info());
    result.set("instructions", kInstructions);
  } else if (method == "server/discover") {
    Json caps = Json::object();
    caps.set("tools", Json::object());
    Json meta = Json::object();
    meta.set("io.modelcontextprotocol/serverInfo", server_info());
    result = Json::object();
    result.set("supportedVersions", supported_versions()).set("capabilities", caps).set("_meta", meta);
    result.set("instructions", kInstructions).set("ttlMs", 600000).set("cacheScope", "private");
  } else if (method == "ping") {
    result = Json::object();
  } else if (method == "tools/list") {
    static Json tools = [] {
      Json t;
      Json::parse(kTools, t);
      return t;
    }();
    result = Json::object();
    result.set("tools", tools);
    // 新版的列表结果必须带缓存提示 (SEP-2549): 工具表跟着桌宠版本走, 10 分钟内不会变; 不同用户之间不共用
    if (modern) result.set("ttlMs", 600000).set("cacheScope", "private");
  } else if (method == "tools/call") {
    bool unknown = false;
    std::string name = params.get_str("name");
    result = tool_call(name, params["arguments"], unknown);
    if (unknown) {
      conn.respond(200, kJson, rpc_error(id, -32602, "Unknown tool: " + name).dump());
      return;
    }
  } else {
    // 新版规定没实现的方法回 404 + -32601; 旧版照常 200 + JSON-RPC 错误
    conn.respond(modern ? 404 : 200, kJson, rpc_error(id, -32601, "Method not found: " + method).dump());
    return;
  }
  if (modern) result.set("resultType", "complete");
  conn.respond(200, kJson, rpc_result(id, std::move(result)).dump());
}

Json PetApi::tool_call(const std::string& name, const Json& args, bool& unknown) {
  if (name == "pet_chat") {
    std::string text = args.get_str("text");
    if (text.find_first_not_of(" \t\r\n") == std::string::npos) return tool_text("text 不能为空", true);
    bool voice = args["voice"].boolean(true);
    if (voice && hidden()) return tool_text("桌宠现在隐藏着 (用户隐藏了, 或者有全屏程序), 不出声. 可以用 voice=false, 或者先 pet_show.", true);
    logf("MCP: pet_chat%s, %zu 字节", voice ? "" : " (不出声)", text.size());
    Json page = Json::object();
    page.set("kind", "chat").set("mode", voice ? "pet" : "text").set("text", text);
    Json r = call(page);
    if (!r["ok"].boolean()) return tool_text(r.get_str("error", "出错了"), true);
    Json s = Json::object();
    s.set("reply", r.get_str("reply")).set("emotion", r["emo"]).set("actions", r["acts"]).set("interrupted", r["interrupted"].boolean());
    s.set("voice", voice);
    std::string reply = r.get_str("reply");
    if (r["interrupted"].boolean()) reply += " (被打断了)";
    return tool_text(reply.empty() ? "(她没有回答)" : reply, false, s);
  }
  if (name == "pet_act") {
    std::string skill = args.get_str("skill"), side = args.get_str("side", "R");
    bool known = false;
    for (const char* s : kSkills) known = known || skill == s;
    if (!known) return tool_text("没有这个手势: " + skill + " (有 wave raise point nod shake shrug think bow squat)", true);
    if (side != "L" && side != "R") return tool_text("side 只能是 L 或 R", true);
    if (hidden()) return tool_text("桌宠现在隐藏着, 做了也看不见. 先 pet_show.", true);
    Json page = Json::object();
    page.set("kind", "act").set("skill", skill).set("side", side);
    Json r = call(page, nullptr, 10);
    if (!r["ok"].boolean()) return tool_text(r.get_str("error", "出错了"), true);
    return tool_text("她做了 " + skill, false);
  }
  if (name == "pet_state") {
    Json s = host_state();
    Json page = Json::object();
    page.set("kind", "state");
    Json r = call(page, nullptr, 5);
    s.set("page", r["ok"].boolean() ? r["state"] : Json());
    if (!r["ok"].boolean()) s.set("page_error", r.get_str("error"));
    return tool_text(s.dump(), false, s);
  }
  if (name == "pet_show" || name == "pet_hide") {
    bool hide = name == "pet_hide", fullscreen = false;
    app_.ui([&] {
      app_.set_user_hidden(hide);
      fullscreen = app_.auto_hidden;
    });
    logf("MCP: %s", name.c_str());
    if (hide) return tool_text("她藏起来了", false);
    return tool_text(fullscreen ? "好的, 不过现在有全屏程序, 全屏结束她才会出现" : "她出来了", false);
  }
  unknown = true;
  return Json();
}

// ---------------------------------------------------------------------------
// stdio 转发: 一行一个 JSON-RPC 消息 (规范的 stdio 传输), 每行转成一个 POST /mcp. 新版消息按请求体补上三个头; 旧版消息原样转.
// 每个请求一个线程 (pet_chat 可能要等好久, 不挡住 ping 之类), 回应按完成先后写回 stdout.
namespace {

struct Relay {
  int port = 8793;
  std::string token;
  HINTERNET session = nullptr;
  HANDLE out = nullptr;
  std::mutex out_mu;
  std::mutex mu;
  std::condition_variable cv;
  int active = 0;

  void write(const std::string& line) {
    std::lock_guard<std::mutex> lock(out_mu);
    std::string data = line + "\n";
    DWORD n = 0;
    WriteFile(out, data.data(), DWORD(data.size()), &n, nullptr);
  }

  // 返回 HTTP 状态 (连不上时 0), body 是回应正文
  int post(const std::string& body, const std::wstring& headers, std::string& reply) {
    HINTERNET conn = WinHttpConnect(session, L"127.0.0.1", INTERNET_PORT(port), 0);
    HINTERNET req = conn ? WinHttpOpenRequest(conn, L"POST", L"/mcp", nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0) : nullptr;
    int status = 0;
    if (req && WinHttpSendRequest(req, headers.c_str(), DWORD(-1), const_cast<char*>(body.data()), DWORD(body.size()), DWORD(body.size()), 0) &&
        WinHttpReceiveResponse(req, nullptr)) {
      DWORD code = 0, size = sizeof code;
      WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &code, &size, WINHTTP_NO_HEADER_INDEX);
      status = int(code);
      char buf[8192];
      DWORD got = 0;
      while (WinHttpReadData(req, buf, sizeof buf, &got) && got) reply.append(buf, got);
    }
    if (req) WinHttpCloseHandle(req);
    if (conn) WinHttpCloseHandle(conn);
    return status;
  }

  void forward(const std::string& line) {
    Json msg;
    if (!Json::parse(line, msg) || !msg.is_object()) {
      write(rpc_error(Json(), -32700, "Parse error").dump());
      return;
    }
    std::wstring headers = L"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\nAuthorization: Bearer " +
                           widen(token) + L"\r\n";
    std::string method = msg.get_str("method");
    std::string version = msg["params"]["_meta"].get_str("io.modelcontextprotocol/protocolVersion");
    if (!version.empty() && !method.empty()) {
      headers += L"MCP-Protocol-Version: " + widen(version) + L"\r\nMcp-Method: " + widen(method) + L"\r\n";
      if (method == "tools/call") headers += L"Mcp-Name: " + widen(msg["params"].get_str("name")) + L"\r\n";
    }
    std::string reply;
    int status = post(line, headers, reply);
    bool request = msg.has("id") && msg.has("method");
    if (status == 0) {
      if (request) write(rpc_error(msg["id"], -32000, "连不上桌宠 (127.0.0.1:" + std::to_string(port) + "): 先启动 wmpet.exe").dump());
    } else if (status == 401 || status == 403) {
      if (request) write(rpc_error(msg["id"], -32000, "桌宠拒绝了请求 (HTTP " + std::to_string(status) + "): " + reply).dump());
    } else if (!reply.empty()) {
      write(reply);
    }
  }
};

}  // namespace

int run_mcp_stdio(int port) {
  Relay& r = *new Relay;  // 不释放: 退出时可能还有转发线程在用
  r.port = port;
  if (!load_or_create_token(r.token)) return 2;
  r.out = GetStdHandle(STD_OUTPUT_HANDLE);
  HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
  if (!in || in == INVALID_HANDLE_VALUE || !r.out || r.out == INVALID_HANDLE_VALUE) return 2;
  r.session = WinHttpOpen(L"wmpet-mcp-stdio", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
  if (!r.session) return 2;
  WinHttpSetTimeouts(r.session, 5000, 5000, 30000, 200000);  // 收回应最多等 200 秒 (宿主那边 180 秒就结束了)
  std::string buf;
  char chunk[8192];
  DWORD got = 0;
  auto dispatch = [&r](std::string line) {
    {
      std::lock_guard<std::mutex> lock(r.mu);
      r.active++;
    }
    std::thread([&r, line = std::move(line)] {
      r.forward(line);
      std::lock_guard<std::mutex> lock(r.mu);
      r.active--;
      r.cv.notify_all();
    }).detach();
  };
  while (ReadFile(in, chunk, sizeof chunk, &got, nullptr) && got) {
    buf.append(chunk, got);
    for (size_t nl; (nl = buf.find('\n')) != std::string::npos;) {
      std::string line = buf.substr(0, nl);
      buf.erase(0, nl + 1);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (line.find_first_not_of(" \t") != std::string::npos) dispatch(std::move(line));
    }
  }
  // stdin 关了 (客户端要退出): 等手上的请求做完再走
  std::unique_lock<std::mutex> lock(r.mu);
  r.cv.wait_for(lock, std::chrono::seconds(5), [&r] { return r.active == 0; });
  WinHttpCloseHandle(r.session);
  return 0;
}
