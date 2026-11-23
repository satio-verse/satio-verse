// 工作台窗口 (2026-10-26, 用户: "能不能做成基于桌宠软件的工作台呢? 网页这种太不方便了"): 桌宠里的第三个 WebView2 窗口,
// 页面 /pet/workbench.html (desktop/web/workbench.*), 和桌宠、设置窗口共用 WebView2 环境. 工作台的活在服务端 (wm/agent,
// 接口 /api/agent 和 /agent/ws), 页面和服务端同源直接连; 宿主只管窗口.
// 托盘"工作台…"、侧菜单的工作台按钮、设置窗口左边"工作台"都打开它. 页面发 "workbench <json>", op: ready / desk (桌前的她加载完) / log.
// 自检: --workbench-shot <png> 打开窗口, 桌前的她加载完 1.5 秒后截一张 (最多等 20 秒), 截完退出.
#pragma once
#include <windows.h>

#include <string>
#include <map>
#include <memory>
#include <vector>

#include "json.h"
#include "webview_host.h"
#include "browser_pane.h"

class App;

class WorkbenchWindow {
  friend struct WorkbenchIntegrationProbe;
 public:
  explicit WorkbenchWindow(App& app) : app_(app) {}
  bool open();  // 没开就建窗口, 开着就提到前面; 窗口没建出来返回 false
  bool is_open() const { return hwnd_ != nullptr; }
  bool shown() const { return hwnd_ && !IsIconic(hwnd_); }  // 开着而且没最小化
  void post(const std::wstring& json) { if (web_) web_->post_json(json); }
  void show_settings(const std::string& tab);  // 设置在工作台里打开 (托盘"设置…"、桌宠的齿轮在工作台开着时)
  void close() { if (hwnd_) PostMessageW(hwnd_, WM_CLOSE, 0, 0); }
  bool topmost() const { return hwnd_ && (GetWindowLongPtrW(hwnd_, GWL_EXSTYLE) & WS_EX_TOPMOST); }
  void set_topmost(bool on);

 private:
  static LRESULT CALLBACK proc(HWND h, UINT m, WPARAM wp, LPARAM lp);
  void on_message(const std::string& m);
  void choose_workspace_folder();
  App& app_;
  HWND hwnd_ = nullptr;
  WebViewHost* web_ = nullptr;
  BrowserPane* browser_ = nullptr;
  std::map<std::string,std::unique_ptr<BrowserPane>> browsers_;
  // Closed panes have released their resources, but asynchronous creation
  // callbacks can still arrive. Keep the small objects alive with the window.
  std::vector<std::unique_ptr<BrowserPane>> retired_browsers_;
  void activate_browser(const std::string& id);
  void close_browsers();
  bool shot_done_ = false;
  bool iconic_ = false;  // 上次是最小化 (还原时再收起桌宠)
  bool folder_picker_open_ = false;
  std::string folder_picker_request_;
};
