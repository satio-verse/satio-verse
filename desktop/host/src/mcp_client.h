// 桌宠当 MCP 客户端: 连操作面板里配的外部 MCP 服务器 (stdio 起子进程, 或 Streamable HTTP), 列工具、调工具.
// 新旧两版协议都会 (规范里叫 dual-era 客户端): 先按新版 2026-07-28 发 server/discover;
// 对方回的不是新版的结果 (旧版服务器多半报"没初始化"或"没有这个方法"), 就退回旧版的 initialize 握手 (2025-11-25).
// 不是线程安全的: 一个连接只在一个线程里用. 只有 cancel() 可以从别的线程调 (停掉正在等的那个请求).
#pragma once
#include <windows.h>
#include <winhttp.h>

#include <atomic>
#include <map>
#include <mutex>
#include <string>

#include "json.h"

class McpClient {
 public:
  ~McpClient() { close(); }
  // cfg: {"transport":"stdio","command","args":[],"cwd","env":{}} 或 {"transport":"http","url","headers":{}}
  // discover_ms: 新版探测 (server/discover) 最多等多久. 起得慢的服务器 (Windows-MCP 起一次要 20 秒) 要等够:
  // 探测超时退回旧版时, 它其实已经按新版回了, 不再认 initialize ("connection is serving the 2026-07-28 protocol")
  bool connect(const Json& cfg, std::string& error, int timeout_ms = 20000, int discover_ms = 8000);
  Json list_tools(std::string& error, int timeout_ms = 20000);  // 分页取完的 tools 数组
  // 返回 tools/call 的 result ({content, structuredContent, isError}); 出错时返回 null, error 里是原因
  Json call_tool(const std::string& name, const Json& args, std::string& error, int timeout_ms = 60000);
  // 别的线程: 停掉正在等的请求 (它马上返回 null, error 是"停掉了"), 并告诉服务器 (notifications/cancelled).
  // 停之后连接照常能用; 下一次调用前 clear_cancel().
  void cancel();
  void clear_cancel() { cancel_ = false; }
  bool cancelled() const { return cancel_; }
  // 连接还在吗: stdio 看进程退没退, HTTP 连上过就算在. 调用超时但还在的, 不用重连
  bool alive() const;
  void close();
  const std::string& version() const { return version_; }  // 谈成的协议版本
  bool modern() const { return modern_; }
  Json server_info;
  std::string instructions;

 private:
  // 发一个请求, 等它的回应 (整个 JSON-RPC 对象: 有 result 或 error). 传输出错、超时时返回 null.
  Json request(const std::string& method, Json params, std::string& error, int timeout_ms);
  void notify(const std::string& method, Json params);
  Json with_meta(Json params) const;
  bool handshake_legacy(const std::string& want, std::string& error, int timeout_ms);

  // stdio
  bool spawn(const Json& cfg, std::string& error);
  bool write_line(const std::string& line);
  bool read_message(Json& msg, int timeout_ms);
  void answer_server_request(const Json& msg);  // 服务器反过来发的请求 (ping、roots/list 等)
  HANDLE job_ = nullptr, proc_ = nullptr, in_w_ = nullptr, out_r_ = nullptr;
  std::string buf_;

  // Streamable HTTP
  int post(const std::string& body, const std::string& method, const std::string& name, const Json& id, std::string& reply,
           int timeout_ms, std::string& error);
  bool http_ = false, https_ = false;
  std::wstring host_, path_;
  INTERNET_PORT port_ = 0;
  std::map<std::string, std::string> headers_;
  std::string session_id_;  // 旧版服务器在 initialize 时可能给一个会话号, 之后每个请求都要带
  HINTERNET session_ = nullptr;
  std::mutex req_mu_;
  HINTERNET live_req_ = nullptr;  // 正在等的 HTTP 请求: cancel() 关掉它, 阻塞着的 WinHTTP 调用就返回

  std::atomic<bool> cancel_{false};

  bool modern_ = false;
  std::string version_;
  int next_id_ = 1;
  Json cfg_;
};
