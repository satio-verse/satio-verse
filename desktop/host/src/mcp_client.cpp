#include "mcp_client.h"

#include <algorithm>
#include <cwctype>
#include <vector>

#include "util.h"

namespace {

const char* kModern = "2026-07-28";
const char* kLegacy = "2025-11-25";

Json client_info() {
  Json c = Json::object();
  c.set("name", "worldmodel-pet").set("title", "WorldModel 桌宠").set("version", "0.2.0");
  return c;
}

bool same_id(const Json& a, const Json& b) {
  if (a.type() != b.type()) return false;
  return a.is_string() ? a.str() == b.str() : a.number() == b.number();
}

// Windows 命令行的引号规则 (CommandLineToArgvW 能原样解回来)
std::wstring quote_arg(const std::wstring& a) {
  if (!a.empty() && a.find_first_of(L" \t\"") == std::wstring::npos) return a;
  std::wstring out = L"\"";
  size_t slashes = 0;
  for (wchar_t c : a) {
    if (c == L'\\') {
      slashes++;
    } else if (c == L'"') {
      out.append(slashes * 2 + 1, L'\\');
      slashes = 0;
    } else {
      out.append(slashes, L'\\');
      slashes = 0;
    }
    if (c != L'\\') out += c;
  }
  out.append(slashes * 2, L'\\');
  return out + L"\"";
}

// npx、uvx 这类命令在 Windows 上多半是 .cmd 批处理, CreateProcess 不能直接起, 要经 cmd.exe
std::wstring resolve(const std::wstring& cmd, bool& batch) {
  batch = false;
  wchar_t found[MAX_PATH];
  for (const wchar_t* ext : {L".exe", L".cmd", L".bat", L".com"}) {
    if (SearchPathW(nullptr, cmd.c_str(), ext, MAX_PATH, found, nullptr)) {
      std::wstring f = found, low = f;
      for (auto& c : low) c = wchar_t(towlower(c));
      batch = low.size() > 4 && (low.compare(low.size() - 4, 4, L".cmd") == 0 || low.compare(low.size() - 4, 4, L".bat") == 0);
      return f;
    }
  }
  return cmd;
}

// 当前环境加上配置里的变量 (变量名不分大小写)
std::wstring env_block(const Json& extra) {
  std::map<std::wstring, std::pair<std::wstring, std::wstring>> vars;  // 小写名 -> (原名, 值)
  if (wchar_t* env = GetEnvironmentStringsW()) {
    for (wchar_t* p = env; *p; p += wcslen(p) + 1) {
      std::wstring kv = p;
      size_t eq = kv.find(L'=', 1);  // "=C:=C:\" 这种以 = 开头的也留着
      if (eq == std::wstring::npos) continue;
      std::wstring name = kv.substr(0, eq), low = name;
      for (auto& c : low) c = wchar_t(towlower(c));
      vars[low] = {name, kv.substr(eq + 1)};
    }
    FreeEnvironmentStringsW(env);
  }
  for (const auto& kv : extra.members()) {
    std::wstring name = widen(kv.first), low = name;
    for (auto& c : low) c = wchar_t(towlower(c));
    vars[low] = {name, widen(kv.second.str())};
  }
  std::wstring block;
  for (const auto& kv : vars) block += kv.second.first + L"=" + kv.second.second + L'\0';
  return block + L'\0';
}

// SSE 正文里找 id 对得上的那条 JSON-RPC 回应 (data 可以分多行)
bool find_in_sse(const std::string& text, const Json& id, Json& out) {
  size_t pos = 0;
  while (pos < text.size()) {
    size_t end = text.find("\n\n", pos);
    size_t end_crlf = text.find("\r\n\r\n", pos);
    if (end_crlf != std::string::npos && (end == std::string::npos || end_crlf < end)) end = end_crlf;
    if (end == std::string::npos) break;
    std::string event = text.substr(pos, end - pos), data;
    pos = end + 2;
    for (size_t a = 0, b; a < event.size(); a = b + 1) {
      b = event.find('\n', a);
      if (b == std::string::npos) b = event.size();
      std::string line = event.substr(a, b - a);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (line.rfind("data:", 0) == 0) data += (data.empty() ? "" : "\n") + line.substr(line.size() > 5 && line[5] == ' ' ? 6 : 5);
    }
    Json msg;
    if (!data.empty() && Json::parse(data, msg) && msg.is_object() && msg.has("id") && !msg.has("method") && same_id(msg["id"], id)) {
      out = msg;
      return true;
    }
  }
  return false;
}

std::wstring header_safe(const std::string& v) {
  for (unsigned char c : v)
    if (c < 0x20 || c > 0x7e) {  // 不是纯 ASCII: 按规范写成 =?base64?...?=
      return L"=?base64?" + widen(base64_encode(reinterpret_cast<const uint8_t*>(v.data()), v.size())) + L"?=";
    }
  return widen(v);
}

}  // namespace

Json McpClient::with_meta(Json params) const {
  if (!params.is_object()) params = Json::object();
  if (!modern_) return params;
  Json& meta = params.at("_meta");
  if (!meta.is_object()) meta = Json::object();
  meta.set("io.modelcontextprotocol/protocolVersion", kModern).set("io.modelcontextprotocol/clientInfo", client_info());
  meta.set("io.modelcontextprotocol/clientCapabilities", Json::object());
  return params;
}

bool McpClient::connect(const Json& cfg, std::string& error, int timeout_ms, int discover_ms) {
  close();
  cfg_ = cfg;
  http_ = cfg.get_str("transport") == "http";
  if (http_) {
    std::wstring url = widen(cfg.get_str("url"));
    URL_COMPONENTS uc{sizeof uc};
    wchar_t host[256] = L"", path[2048] = L"";
    uc.lpszHostName = host;
    uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path;
    uc.dwUrlPathLength = 2048;
    if (url.empty() || !WinHttpCrackUrl(url.c_str(), 0, 0, &uc)) {
      error = "地址不对: " + cfg.get_str("url");
      return false;
    }
    https_ = uc.nScheme == INTERNET_SCHEME_HTTPS;
    host_ = host;
    path_ = path[0] ? path : L"/";
    port_ = uc.nPort;
    headers_.clear();
    for (const auto& kv : cfg["headers"].members()) headers_[kv.first] = kv.second.str();
    session_ = WinHttpOpen(L"wmpet-mcp-client", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session_) {
      error = "WinHTTP 打不开";
      return false;
    }
  } else if (!spawn(cfg, error)) {
    return false;
  }
  // 先按新版探: server/discover. 新版服务器回 DiscoverResult (或者 -32022 说版本不支持, 附上它支持的版本)
  modern_ = true;
  std::string err;
  Json r = request("server/discover", Json::object(), err, std::min(timeout_ms, discover_ms));
  const Json& res = r["result"];
  bool modern_ok = false;
  for (const Json& v : res["supportedVersions"].items()) modern_ok = modern_ok || v.str() == kModern;
  if (modern_ok) {
    version_ = kModern;
    server_info = res["_meta"]["io.modelcontextprotocol/serverInfo"];
    instructions = res.get_str("instructions");
    return true;
  }
  std::string want = kLegacy;
  if (r["error"]["code"].number() == -32022) {  // 新版服务器, 但不支持 2026-07-28: 挑它支持的旧版
    for (const Json& v : r["error"]["data"]["supported"].items())
      if (v.str() < kModern) {
        want = v.str();
        break;
      }
  }
  // 旧版: stdio 的进程可能因为不认识 server/discover 退出了, 重起一个干净的
  modern_ = false;
  if (!http_) {
    DWORD code = STILL_ACTIVE;
    if (proc_ && GetExitCodeProcess(proc_, &code) && code != STILL_ACTIVE) {
      close();
      if (!spawn(cfg, error)) return false;
    }
  }
  return handshake_legacy(want, error, timeout_ms);
}

bool McpClient::handshake_legacy(const std::string& want, std::string& error, int timeout_ms) {
  Json params = Json::object();
  params.set("protocolVersion", want).set("capabilities", Json::object()).set("clientInfo", client_info());
  Json r = request("initialize", params, error, timeout_ms);
  if (r.is_null()) {
    if (error.empty()) error = "服务器没有回应 initialize";
    return false;
  }
  if (r.has("error")) {
    error = "握手失败: " + r["error"].get_str("message");
    return false;
  }
  version_ = r["result"].get_str("protocolVersion", want);
  server_info = r["result"]["serverInfo"];
  instructions = r["result"].get_str("instructions");
  notify("notifications/initialized", Json::object());
  return true;
}

Json McpClient::list_tools(std::string& error, int timeout_ms) {
  Json tools = Json::array();
  std::string cursor;
  for (int page = 0; page < 50; page++) {  // 分页最多取 50 页, 免得游标绕圈
    Json params = Json::object();
    if (!cursor.empty()) params.set("cursor", cursor);
    Json r = request("tools/list", params, error, timeout_ms);
    if (r.is_null()) return Json();
    if (r.has("error")) {
      error = "tools/list 出错: " + r["error"].get_str("message");
      return Json();
    }
    for (const Json& t : r["result"]["tools"].items()) tools.push(t);
    cursor = r["result"].get_str("nextCursor");
    if (cursor.empty()) break;
  }
  return tools;
}

Json McpClient::call_tool(const std::string& name, const Json& args, std::string& error, int timeout_ms) {
  Json params = Json::object();
  params.set("name", name).set("arguments", args.is_object() ? args : Json::object());
  Json r = request("tools/call", params, error, timeout_ms);
  if (r.is_null()) return Json();
  if (r.has("error")) {
    error = "tools/call 出错: " + r["error"].get_str("message");
    return Json();
  }
  return r["result"];
}

Json McpClient::request(const std::string& method, Json params, std::string& error, int timeout_ms) {
  Json id(next_id_++);
  Json msg = Json::object();
  msg.set("jsonrpc", "2.0").set("id", id).set("method", method).set("params", with_meta(std::move(params)));
  // 停掉了: 告诉服务器别做了 (规范: 发起方用 notifications/cancelled 取消自己发的请求; 之后它的回应来了也不理)
  auto stopped = [&] {
    Json p = Json::object();
    p.set("requestId", id).set("reason", "用户或服务端不等了");
    notify("notifications/cancelled", p);
    error = "停掉了";
    return Json();
  };
  if (cancel_) {  // 还没发就停了: 不用告诉服务器
    error = "停掉了";
    return Json();
  }
  if (http_) {
    std::string reply;
    int status = post(msg.dump(), method, method == "tools/call" ? msg["params"].get_str("name") : "", id, reply, timeout_ms, error);
    if (cancel_) return stopped();
    if (status == 0) return Json();
    Json r;
    if (Json::parse(reply, r) && r.is_object() && (r.has("result") || r.has("error"))) return r;
    if (find_in_sse(reply, id, r)) return r;
    // 旧版服务器对不认识的请求常回 400/404/405 加一段文字: 当成一个错误, 让调用方决定退不退回旧版
    Json e = Json::object();
    e.set("code", -32000).set("message", "HTTP " + std::to_string(status) + ": " + reply.substr(0, 200));
    r = Json::object();
    r.set("jsonrpc", "2.0").set("id", id).set("error", e);
    return r;
  }
  if (!write_line(msg.dump())) {
    error = "写不进服务器的 stdin (进程可能退出了)";
    return Json();
  }
  ULONGLONG deadline = GetTickCount64() + ULONGLONG(timeout_ms);
  while (GetTickCount64() < deadline) {
    Json m;
    if (!read_message(m, int(deadline - GetTickCount64()))) break;
    if (m.has("method") && m.has("id")) {
      answer_server_request(m);
    } else if (m.has("id") && same_id(m["id"], id)) {
      return m;
    }
  }
  if (cancel_) return stopped();
  DWORD code = STILL_ACTIVE;
  if (proc_ && GetExitCodeProcess(proc_, &code) && code != STILL_ACTIVE) {
    error = "服务器进程退出了 (退出码 " + std::to_string(code) + ")";
  } else {  // 超时: 也告诉服务器别做了 (规范建议), 连接留着接着用
    Json p = Json::object();
    p.set("requestId", id).set("reason", "等太久了");
    notify("notifications/cancelled", p);
    error = "等了 " + std::to_string(timeout_ms / 1000) + " 秒没有回应 " + method;
  }
  return Json();
}

bool McpClient::alive() const {
  if (http_) return session_ != nullptr;
  DWORD code = 0;
  return proc_ && in_w_ && GetExitCodeProcess(proc_, &code) && code == STILL_ACTIVE;
}

void McpClient::notify(const std::string& method, Json params) {
  Json msg = Json::object();
  msg.set("jsonrpc", "2.0").set("method", method).set("params", with_meta(std::move(params)));
  if (http_) {
    std::string reply, error;
    post(msg.dump(), method, "", Json(), reply, 5000, error);
  } else {
    write_line(msg.dump());
  }
}

void McpClient::answer_server_request(const Json& msg) {
  Json r = Json::object();
  r.set("jsonrpc", "2.0").set("id", msg["id"]);
  if (msg.get_str("method") == "ping") {
    r.set("result", Json::object());
  } else {  // roots/list、sampling、elicitation 都没声明, 一律说不支持
    Json e = Json::object();
    e.set("code", -32601).set("message", "Method not found");
    r.set("error", e);
  }
  write_line(r.dump());
}

bool McpClient::spawn(const Json& cfg, std::string& error) {
  std::wstring cmd = widen(cfg.get_str("command"));
  if (cmd.empty()) {
    error = "没有填命令";
    return false;
  }
  bool batch = false;
  std::wstring exe = resolve(cmd, batch);
  std::wstring line = quote_arg(exe);
  for (const Json& a : cfg["args"].items()) line += L" " + quote_arg(widen(a.str()));
  if (batch) line = L"cmd.exe /d /s /c \"" + line + L"\"";
  SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
  HANDLE child_in = nullptr, child_out = nullptr;
  if (!CreatePipe(&child_in, &in_w_, &sa, 0) || !CreatePipe(&out_r_, &child_out, &sa, 1 << 16)) {
    error = "建管道失败";
    return false;
  }
  SetHandleInformation(in_w_, HANDLE_FLAG_INHERIT, 0);
  SetHandleInformation(out_r_, HANDLE_FLAG_INHERIT, 0);
  HANDLE nul = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);  // stderr 不收: 没人读会把它堵住
  STARTUPINFOEXW si{};
  si.StartupInfo.cb = sizeof si;
  si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  si.StartupInfo.hStdInput = child_in;
  si.StartupInfo.hStdOutput = child_out;
  si.StartupInfo.hStdError = nul;
  // 只把这几个句柄交给子进程. 不限定的话本进程里所有可继承的句柄都会漏过去 (比如本机接口的 socket:
  // 子进程握着监听 socket, 桌宠退出时 accept 不返回, 退不出去, 作业对象也就一直不结束子进程)
  HANDLE pass[3] = {child_in, child_out, nul};
  SIZE_T attr_size = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &attr_size);
  std::vector<char> attr_buf(attr_size);
  auto attrs = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attr_buf.data());
  bool listed = InitializeProcThreadAttributeList(attrs, 1, 0, &attr_size) &&
                UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, pass,
                                          (nul != INVALID_HANDLE_VALUE ? 3 : 2) * sizeof(HANDLE), nullptr, nullptr);
  if (listed) si.lpAttributeList = attrs;
  PROCESS_INFORMATION pi{};
  std::wstring env = env_block(cfg["env"]);
  std::wstring cwd = widen(cfg.get_str("cwd"));
  std::vector<wchar_t> mutable_line(line.begin(), line.end());
  mutable_line.push_back(0);
  BOOL ok = CreateProcessW(nullptr, mutable_line.data(), nullptr, nullptr, TRUE,
                           CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED | (listed ? EXTENDED_STARTUPINFO_PRESENT : 0),
                           env.data(), cwd.empty() ? nullptr : cwd.c_str(), &si.StartupInfo, &pi);
  if (listed) DeleteProcThreadAttributeList(attrs);
  CloseHandle(child_in);
  CloseHandle(child_out);
  if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
  if (!ok) {
    error = "起不来: " + utf8(line) + " (错误 " + std::to_string(GetLastError()) + ")";
    return false;
  }
  // 放进作业对象: 关连接时连它起的子孙进程 (npx 起的 node 等) 一起结束, 不留孤儿进程
  job_ = CreateJobObjectW(nullptr, nullptr);
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim{};
  lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if (job_) {
    SetInformationJobObject(job_, JobObjectExtendedLimitInformation, &lim, sizeof lim);
    AssignProcessToJobObject(job_, pi.hProcess);
  }
  ResumeThread(pi.hThread);
  CloseHandle(pi.hThread);
  proc_ = pi.hProcess;
  buf_.clear();
  return true;
}

bool McpClient::write_line(const std::string& line) {
  if (!in_w_) return false;
  std::string data = line + "\n";
  DWORD n = 0;
  return WriteFile(in_w_, data.data(), DWORD(data.size()), &n, nullptr) && n == data.size();
}

// 一行一个 JSON-RPC 消息; 不是 JSON 的行 (有的服务器往 stdout 打日志) 跳过
bool McpClient::read_message(Json& msg, int timeout_ms) {
  ULONGLONG deadline = GetTickCount64() + ULONGLONG(std::max(0, timeout_ms));
  for (;;) {
    size_t nl;
    while ((nl = buf_.find('\n')) != std::string::npos) {
      std::string line = buf_.substr(0, nl);
      buf_.erase(0, nl + 1);
      if (Json::parse(line, msg) && msg.is_object()) return true;
    }
    DWORD avail = 0;
    if (!PeekNamedPipe(out_r_, nullptr, 0, nullptr, &avail, nullptr)) return false;  // 进程退出, 管道断了
    if (avail) {
      std::vector<char> chunk(std::min<DWORD>(avail, 1 << 20));
      DWORD got = 0;
      if (!ReadFile(out_r_, chunk.data(), DWORD(chunk.size()), &got, nullptr)) return false;
      buf_.append(chunk.data(), got);
      continue;
    }
    if (GetTickCount64() >= deadline || cancel_) return false;
    Sleep(5);
  }
}

void McpClient::cancel() {
  cancel_ = true;
  std::lock_guard<std::mutex> lock(req_mu_);
  if (live_req_) {
    WinHttpCloseHandle(live_req_);  // 另一个线程里阻塞着的 WinHttpSendRequest / ReceiveResponse / ReadData 会失败返回
    live_req_ = nullptr;
  }
}

int McpClient::post(const std::string& body, const std::string& method, const std::string& name, const Json& id,
                    std::string& reply, int timeout_ms, std::string& error) {
  HINTERNET conn = WinHttpConnect(session_, host_.c_str(), port_, 0);
  HINTERNET req = conn ? WinHttpOpenRequest(conn, L"POST", path_.c_str(), nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                            https_ ? WINHTTP_FLAG_SECURE : 0)
                       : nullptr;
  int status = 0;
  if (req) {
    std::lock_guard<std::mutex> lock(req_mu_);
    live_req_ = req;
  }
  if (req) {
    WinHttpSetTimeouts(req, 5000, 10000, 30000, timeout_ms);
    std::wstring h = L"Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n";
    if (modern_) {
      h += L"MCP-Protocol-Version: " + widen(kModern) + L"\r\nMcp-Method: " + widen(method) + L"\r\n";
      if (!name.empty()) h += L"Mcp-Name: " + header_safe(name) + L"\r\n";
    } else if (method != "initialize") {
      h += L"MCP-Protocol-Version: " + widen(version_) + L"\r\n";
      if (!session_id_.empty()) h += L"Mcp-Session-Id: " + widen(session_id_) + L"\r\n";
    }
    for (const auto& kv : headers_) h += widen(kv.first) + L": " + widen(kv.second) + L"\r\n";
    if (WinHttpSendRequest(req, h.c_str(), DWORD(-1), const_cast<char*>(body.data()), DWORD(body.size()), DWORD(body.size()), 0) &&
        WinHttpReceiveResponse(req, nullptr)) {
      DWORD code = 0, size = sizeof code;
      WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &code, &size,
                          WINHTTP_NO_HEADER_INDEX);
      status = int(code);
      wchar_t sid[256];
      DWORD sid_size = sizeof sid;
      if (method == "initialize" &&
          WinHttpQueryHeaders(req, WINHTTP_QUERY_CUSTOM, L"Mcp-Session-Id", sid, &sid_size, WINHTTP_NO_HEADER_INDEX))
        session_id_ = utf8(sid);
      char buf[8192];
      DWORD got = 0;
      Json found;
      // 事件流可能在回应之后还开着: 找到自己的那条回应就不再读
      while (WinHttpReadData(req, buf, sizeof buf, &got) && got) {
        reply.append(buf, got);
        if (!id.is_null() && reply.find("data:") != std::string::npos && find_in_sse(reply, id, found)) break;
      }
    } else {
      error = "连不上 " + utf8(host_) + " (错误 " + std::to_string(GetLastError()) + ")";
    }
  } else {
    error = "连不上 " + utf8(host_);
  }
  {
    std::lock_guard<std::mutex> lock(req_mu_);
    if (req && live_req_ == req) WinHttpCloseHandle(req);  // cancel() 关过的不再关
    live_req_ = nullptr;
  }
  if (conn) WinHttpCloseHandle(conn);
  return status;
}

void McpClient::close() {
  if (in_w_) {
    CloseHandle(in_w_);  // 关掉 stdin: 规范的正常退出方式, 给它半秒自己退
    in_w_ = nullptr;
    if (proc_) WaitForSingleObject(proc_, 500);
  }
  if (job_) {
    CloseHandle(job_);  // 还没退的连同子孙进程一起结束
    job_ = nullptr;
  }
  if (proc_) {
    TerminateProcess(proc_, 1);
    CloseHandle(proc_);
    proc_ = nullptr;
  }
  if (out_r_) {
    CloseHandle(out_r_);
    out_r_ = nullptr;
  }
  if (session_) {
    WinHttpCloseHandle(session_);
    session_ = nullptr;
  }
  session_id_.clear();
  modern_ = false;
  version_.clear();
  buf_.clear();
}
