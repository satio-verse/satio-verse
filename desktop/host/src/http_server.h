// 本机 HTTP/1.1 小服务 (OpenAI 兼容 API 和 MCP 用): 只听 127.0.0.1, 每个连接一个线程, 一个连接一个请求 (Connection: close).
// 回应可以是一次性的 JSON, 也可以是 SSE 流 (流式聊天).
#pragma once
#include <winsock2.h>
#include <ws2tcpip.h>

#include <atomic>
#include <functional>
#include <map>
#include <string>
#include <thread>

struct HttpRequest {
  std::string method, path, query, body;
  std::map<std::string, std::string> headers;  // 键一律小写
  std::string header(const std::string& lower_name) const {
    auto it = headers.find(lower_name);
    return it == headers.end() ? std::string() : it->second;
  }
};

class HttpConn {
 public:
  explicit HttpConn(SOCKET s) : s_(s) {}
  bool respond(int status, const std::string& content_type, const std::string& body, const std::string& extra_headers = "");
  bool start_sse(const std::string& extra_headers = "");
  bool sse(const std::string& data);  // 发一个 "data: ...\n\n" 事件
  bool alive() const { return alive_; }

 private:
  bool send_all(const std::string& data);
  SOCKET s_;
  bool alive_ = true;
};

class HttpServer {
 public:
  using Handler = std::function<void(const HttpRequest&, HttpConn&)>;
  ~HttpServer() { stop(); }
  bool start(int port, Handler handler);
  void stop();
  int port() const { return port_; }

 private:
  void serve(SOCKET c);
  SOCKET listen_ = INVALID_SOCKET;
  std::thread accept_;
  std::atomic<bool> running_{false};
  Handler handler_;
  int port_ = 0;
};
