// Untrusted browsing lives in a separate WebView2 environment. No application
// bridge, host objects, injected scripts, or /pet local-resource proxy are added.
#pragma once
#include "util.h"
#include "json.h"
#include <set>

class BrowserPane {
  friend struct BrowserPaneProbe; // Standalone native regression harness; no web-facing test API.
  friend struct WorkbenchIntegrationProbe;
 public:
  BrowserPane(HWND owner, std::wstring user_data, std::set<int> protected_ports);
  std::function<void(const Json&)> on_state;
  void command(const Json& message);
  void parent_moved();
  void close();
  static bool allowed_url(const std::wstring& url, const std::set<int>& protected_ports);
  static int url_port(const std::wstring& url);
 private:
  void start();
  HRESULT configure(ICoreWebView2Controller* controller);
  void fail(const std::string& error);
  void emit(const std::string& state = "", const std::string& error = "");
  void navigate(const std::wstring& url);
  void bounds(const Json& data, bool visible);
  void update_bounds();
  HWND owner_;
  std::wstring user_data_, pending_url_;
  std::set<int> protected_ports_;
  HMODULE loader_ = nullptr;
  Com<ICoreWebView2Environment> environment_;
  Com<ICoreWebView2Controller> controller_;
  Com<ICoreWebView2> view_;
  Com<ICoreWebView2DevToolsProtocolEventReceiver> network_receiver_;
  RECT bounds_{};
  bool started_ = false, closed_ = false, visible_ = false, network_ready_ = false;
  int http_status_ = 0;
  std::string state_ = "starting", error_;
};
