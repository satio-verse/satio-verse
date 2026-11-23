// 宿主对外的本机接口 (M2): OpenAI 兼容的 /v1 (别的程序把桌宠当成一个聊天模型用) 和 MCP 的 /mcp (给 AI 助手当工具用).
// 只听 127.0.0.1, 要 Bearer token (首次启动时生成, 存在 %APPDATA%\WorldModel\pet-api-token.txt), 校验 Host 和 Origin (防 DNS 重绑定).
// 请求经宿主转给页面 ({"t":"api","id":N,"kind":...}), 页面做完用 "apiev <id> <json>" 回事件 (delta 一段回复 / done 结束), 见 web/api-bridge.js.
//
// 两个 token, 按 token 定权限 (不按请求里写的 model):
//   - 主人的 (pet-api-token.txt): wm-pet、wm-text、wm-guest 和 /mcp 都能用. 给你自己的程序 (Claude Code、dsh 等);
//   - 访客的 (pet-api-guest-token.txt): 只有 wm-guest, 写哪个 model 都按 wm-guest 走; /mcp 回 403. 给 AstrBot 这类第三方机器人框架,
//     它们转来的是别人的话: wm-guest 走服务端的访客连接 (/guest), 没有工具、读不到主人的记忆、不写记忆, 按 OpenAI 的 user 字段分会话.
#pragma once
#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>

#include "http_server.h"
#include "json.h"

class App;

class PetApi {
 public:
  explicit PetApi(App& app) : app_(app) {}
  bool start(int port);
  void stop();                                 // 退出时: 还在等的请求一律结束
  void on_page_event(const std::string& msg);  // 页面消息 "apiev " 后面的部分

  // 转给页面并等它做完. on_delta 每收到一段回复调一次, 返回 false 表示调用方不要了 (连接断了), 这一轮会被取消.
  // 返回页面的 done 事件: {"ok":true,...} 或 {"ok":false,"status":HTTP 状态,"error":"..."}
  Json call(Json req, const std::function<bool(const std::string&)>& on_delta = nullptr, int timeout_s = 180);
  Json host_state();  // 宿主这边的状态 (显示、麦克风、静音、站在哪), 合进 pet_state
  bool hidden();      // 用户隐藏了, 或者全屏程序时自动隐藏了
  Json guest_stats() const;  // wm-guest 这次启动以来的请求数、其中带了工具的次数 (面板的 QQ 页提示用)

 private:
  void handle(const HttpRequest& req, HttpConn& conn);
  void models(HttpConn& conn, bool guest);
  void chat(const HttpRequest& req, HttpConn& conn, bool guest);
  void mcp(const HttpRequest& req, HttpConn& conn);  // mcp.cpp
  Json tool_call(const std::string& name, const Json& args, bool& unknown);

  struct Call {
    std::mutex mu;
    std::condition_variable cv;
    std::deque<Json> events;
  };
  App& app_;
  HttpServer http_;
  std::string token_;
  std::string guest_token_;
  std::atomic<int> guest_requests_{0};
  std::atomic<int> guest_tools_{0};  // 调用方 (AstrBot 这类) 在请求里带了工具的次数: 不用, 只提示用户去它那边关掉
  std::mutex mu_;
  int seq_ = 0;
  std::map<int, std::shared_ptr<Call>> calls_;
  bool stopping_ = false;
};

// 本机接口的 token: 有就读, 没有就生成一个 (32 字节随机数, 十六进制). 访客 token 另存一个文件
std::wstring api_token_path();
std::wstring api_guest_token_path();
bool load_or_create_token(std::string& token);
bool load_or_create_token(std::string& token, const std::wstring& path);

// wmpet.exe --mcp-stdio [--api-port 8793]: 给只会起 stdio 子进程的 MCP 客户端用, 把 stdin/stdout 上的 JSON-RPC 转发到正在运行的桌宠的 /mcp
int run_mcp_stdio(int port);
