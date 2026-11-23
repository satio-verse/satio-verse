// 操作面板 (设置窗口): 人设预设、模型、MCP 服务器、skill. 一个普通窗口里的第二个 WebView2, 页面 /pet/panel.html,
// 和桌宠共用 WebView2 环境 (同一组浏览器进程). 设置的读写和密钥都在宿主这边 (settings.h), 页面拿不到密钥的明文.
// 页面发 "panel <json>", op 有 get / save / mcp-test / models / skills / skill-read / skill-save / skill-delete / open-skills /
// ready / selftest / log / close-ok;
// 宿主还会主动发 {op: show / changed / closing} (closing: 点了关闭, 页面存完没存的改动回 close-ok);
// 宿主回 {"t":"panel","id":请求号,"ok":...}.
#pragma once
#include <windows.h>

#include <string>

#include "json.h"
#include "webview_host.h"

class App;

class PanelWindow {
 public:
  explicit PanelWindow(App& app) : app_(app) {}
  void open();  // 没开就建窗口, 开着就提到前面
  bool is_open() const { return hwnd_ != nullptr; }
  void notify_changed();  // 设置在别处改了 (侧菜单换了人设): 开着的面板重新读一遍
  // 工作台里的设置页 (panel.html?embed=1 放在 iframe 里): 消息经工作台页面转来, 回话也发给工作台, 由它转给 iframe
  void handle(const std::string& text, WebViewHost* from);
  void close();  // 有没存的改动先存 (和点关闭一样)

 private:
  static LRESULT CALLBACK proc(HWND h, UINT m, WPARAM wp, LPARAM lp);
  void on_message(const std::string& m);
  void reply(Json r);        // UI 线程上
  void reply_later(Json r);  // 工作线程上: 转到 UI 线程再发
  void shot_next();          // 自检 (--panel-shot): 依次截每一页, 截完退出
  App& app_;
  HWND hwnd_ = nullptr;
  WebViewHost* web_ = nullptr;
  WebViewHost* sink_ = nullptr;   // 这条消息从哪来, 回话发回哪 (自己的窗口或工作台)
  WebViewHost* embed_ = nullptr;  // 工作台 (嵌着设置页): 设置在别处改了也告诉它
  Json shot_tabs_;
  size_t shot_index_ = 0;
  bool closing_ = false;  // 点了关闭, 等页面把没存的改动存完 (op close-ok)
};

// wmpet.exe --mcp-probe <配置.json> [--mcp-call 工具名 参数.json]: 用宿主的 MCP 客户端连一次, 把结果 (JSON) 写到 stdout. 自检用.
int run_mcp_probe(const std::wstring& cfg_path, const std::string& tool, const std::wstring& args_path);
