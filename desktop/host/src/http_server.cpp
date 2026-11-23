#include "http_server.h"

#include <algorithm>
#include <cctype>
#include <cstdlib>

#include "util.h"

namespace {

constexpr size_t kMaxHeader = 64 * 1024;
constexpr size_t kMaxBody = 4 * 1024 * 1024;

const char* reason(int status) {
  switch (status) {
    case 200: return "OK";
    case 202: return "Accepted";
    case 400: return "Bad Request";
    case 401: return "Unauthorized";
    case 403: return "Forbidden";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 409: return "Conflict";
    case 413: return "Payload Too Large";
    case 500: return "Internal Server Error";
    case 503: return "Service Unavailable";
    case 504: return "Gateway Timeout";
    default: return "Status";
  }
}

std::string lower(std::string s) {
  for (auto& c : s) c = char(tolower(static_cast<unsigned char>(c)));
  return s;
}

std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t"), b = s.find_last_not_of(" \t\r");
  return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

}  // namespace

bool HttpConn::send_all(const std::string& data) {
  size_t sent = 0;
  while (alive_ && sent < data.size()) {
    int n = send(s_, data.data() + sent, int(std::min<size_t>(data.size() - sent, 1 << 20)), 0);
    if (n <= 0) alive_ = false;
    else sent += size_t(n);
  }
  return alive_;
}

bool HttpConn::respond(int status, const std::string& content_type, const std::string& body, const std::string& extra) {
  std::string head = "HTTP/1.1 " + std::to_string(status) + " " + reason(status) + "\r\n";
  if (!content_type.empty()) head += "Content-Type: " + content_type + "\r\n";
  head += "Content-Length: " + std::to_string(body.size()) + "\r\nCache-Control: no-store\r\nConnection: close\r\n" + extra + "\r\n";
  return send_all(head + body);
}

bool HttpConn::start_sse(const std::string& extra) {
  return send_all("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\n"
                  "X-Accel-Buffering: no\r\nConnection: close\r\n" + extra + "\r\n");
}

bool HttpConn::sse(const std::string& data) { return send_all("data: " + data + "\n\n"); }

bool HttpServer::start(int port, Handler handler) {
  WSADATA wsa;
  if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;
  listen_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (listen_ == INVALID_SOCKET) return false;
  // socket 默认可以被子进程继承: 桌宠起的 MCP 服务器握着它的话, 这边 closesocket 之后 accept 不返回, 桌宠退不出去
  SetHandleInformation(reinterpret_cast<HANDLE>(listen_), HANDLE_FLAG_INHERIT, 0);
  // 不设 SO_REUSEADDR: 端口被别的程序占着时就失败, 免得和它抢
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(u_short(port));
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // 只听本机
  if (bind(listen_, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 || listen(listen_, 16) != 0) {
    logf("API 端口 %d 打不开 (错误 %d), 可能被别的程序占着", port, WSAGetLastError());
    closesocket(listen_);
    listen_ = INVALID_SOCKET;
    return false;
  }
  port_ = port;
  handler_ = std::move(handler);
  running_ = true;
  accept_ = std::thread([this] {
    while (running_) {
      SOCKET c = accept(listen_, nullptr, nullptr);
      if (c == INVALID_SOCKET) break;
      SetHandleInformation(reinterpret_cast<HANDLE>(c), HANDLE_FLAG_INHERIT, 0);
      std::thread([this, c] { serve(c); }).detach();
    }
  });
  return true;
}

void HttpServer::stop() {
  if (!running_) return;
  running_ = false;
  closesocket(listen_);  // accept 随之返回
  listen_ = INVALID_SOCKET;
  if (accept_.joinable()) accept_.join();
}

void HttpServer::serve(SOCKET c) {
  DWORD timeout = 15000;
  setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof timeout);
  HttpConn conn(c);
  std::string buf;
  char chunk[8192];
  size_t head_end = std::string::npos;
  while ((head_end = buf.find("\r\n\r\n")) == std::string::npos) {
    int n = recv(c, chunk, sizeof chunk, 0);
    if (n <= 0 || buf.size() > kMaxHeader) {
      closesocket(c);
      return;
    }
    buf.append(chunk, size_t(n));
  }
  HttpRequest req;
  size_t line_end = buf.find("\r\n");
  std::string line = buf.substr(0, line_end);
  size_t sp1 = line.find(' '), sp2 = line.find(' ', sp1 + 1);
  if (sp1 == std::string::npos || sp2 == std::string::npos) {
    conn.respond(400, "text/plain", "bad request line");
    closesocket(c);
    return;
  }
  req.method = line.substr(0, sp1);
  std::string target = line.substr(sp1 + 1, sp2 - sp1 - 1);
  size_t q = target.find('?');
  req.path = target.substr(0, q);
  if (q != std::string::npos) req.query = target.substr(q + 1);
  for (size_t pos = line_end + 2; pos < head_end;) {
    size_t end = buf.find("\r\n", pos);
    std::string h = buf.substr(pos, end - pos);
    size_t colon = h.find(':');
    if (colon != std::string::npos) req.headers[lower(trim(h.substr(0, colon)))] = trim(h.substr(colon + 1));
    pos = end + 2;
  }
  size_t length = size_t(strtoull(req.header("content-length").c_str(), nullptr, 10));
  if (length > kMaxBody) {
    conn.respond(413, "text/plain", "body too large");
    closesocket(c);
    return;
  }
  req.body = buf.substr(head_end + 4);
  while (req.body.size() < length) {
    int n = recv(c, chunk, sizeof chunk, 0);
    if (n <= 0) break;
    req.body.append(chunk, size_t(n));
  }
  req.body.resize(std::min(req.body.size(), length));
  try {
    handler_(req, conn);
  } catch (...) {
    conn.respond(500, "text/plain", "internal error");
  }
  shutdown(c, SD_SEND);
  closesocket(c);
}
