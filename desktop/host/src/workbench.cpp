#include "workbench.h"
#include "tools.h"

#include <dwmapi.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <shlobj.h>
#include <shlwapi.h>

#include <algorithm>

#include "app.h"

namespace {

constexpr UINT_PTR T_SHOT = 1;
constexpr UINT WM_CHOOSE_WORKSPACE = WM_APP + 33;

// Use the normal Windows folder picker, without exposing an arbitrary path or
// navigation URL supplied by an untrusted web page to the native shell.
const CLSID kClsidFileOpenDialog = {0xdc1c5a9c, 0xe88a, 0x4dde, {0xa5, 0xa1, 0x60, 0xf8, 0x2a, 0x20, 0xae, 0xf7}};
const IID kIidIFileOpenDialog = {0xd57c7288, 0xd4ad, 0x4768, {0xbe, 0x02, 0x9d, 0x96, 0x95, 0x32, 0xd9, 0x60}};

bool request_id(const std::string& value) {
  return !value.empty() && value.size() <= 100 && std::all_of(value.begin(), value.end(), [](unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
  });
}

std::wstring existing_final_path(const std::wstring& path) {
  if(path.empty() || path.size()>32700 || path.find(L'\0')!=std::wstring::npos || PathIsRelativeW(path.c_str())) return {};
  HANDLE handle=CreateFileW(path.c_str(),FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS,nullptr);
  if(handle==INVALID_HANDLE_VALUE) return {};
  std::wstring value(32768,L'\0');DWORD size=GetFinalPathNameByHandleW(handle,value.data(),DWORD(value.size()),FILE_NAME_NORMALIZED);
  CloseHandle(handle);if(!size || size>=value.size())return {};value.resize(size);
  if(value.rfind(L"\\\\?\\UNC\\",0)==0)value=L"\\\\"+value.substr(8);
  else if(value.rfind(L"\\\\?\\",0)==0)value=value.substr(4);
  return value;
}

bool reveal_workspace_path(const std::wstring& workspace,const std::wstring& requested) {
  const auto root=existing_final_path(workspace), path=existing_final_path(requested);
  if(root.empty() || path.empty())return false;
  const auto prefix=root.back()==L'\\'?root:root+L"\\";
  if(_wcsicmp(path.c_str(),root.c_str())!=0 && (path.size()<=prefix.size() || _wcsnicmp(path.c_str(),prefix.c_str(),prefix.size())!=0))return false;
  PIDLIST_ABSOLUTE full=nullptr;
  if(FAILED(SHParseDisplayName(path.c_str(),nullptr,&full,0,nullptr)) || !full)return false;
  PIDLIST_ABSOLUTE parent=ILCloneFull(full);
  PCUITEMID_CHILD child=ILFindLastID(full);
  HRESULT result=E_FAIL;
  if(parent && ILRemoveLastID(parent))result=SHOpenFolderAndSelectItems(parent,1,&child,0);
  CoTaskMemFree(parent);CoTaskMemFree(full);return SUCCEEDED(result);
}

Json choose_workspace(HWND owner, const std::string& rid) {
  Json result = Json::object();
  result.set("t", "workbench-folder").set("rid", rid).set("cancelled", false);
  Com<IFileOpenDialog> dialog;
  HRESULT hr = CoCreateInstance(kClsidFileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, kIidIFileOpenDialog,
                                reinterpret_cast<void**>(dialog.put()));
  if (FAILED(hr)) return result.set("error", "系统文件夹选择器打不开，请稍后重试");
  DWORD flags = 0;
  hr = dialog->GetOptions(&flags);
  if (SUCCEEDED(hr)) hr = dialog->SetOptions(flags | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR);
  if (FAILED(hr)) return result.set("error", "系统文件夹选择器初始化失败");
  dialog->SetTitle(L"选择工作区文件夹");
  dialog->SetOkButtonLabel(L"选择文件夹");
  hr = dialog->Show(owner);
  if (hr == HRESULT_FROM_WIN32(ERROR_CANCELLED)) return result.set("cancelled", true);
  if (FAILED(hr)) return result.set("error", "未能选择文件夹，请重试");
  Com<IShellItem> item;
  hr = dialog->GetResult(item.put());
  if (FAILED(hr)) return result.set("error", "未能读取选中的文件夹");
  PWSTR path = nullptr;
  hr = item->GetDisplayName(SIGDN_FILESYSPATH, &path);
  if (FAILED(hr) || !path) {
    if (path) CoTaskMemFree(path);
    return result.set("error", "请选择此电脑上的文件夹");
  }
  const std::wstring selected(path);
  CoTaskMemFree(path);
  DWORD attributes = GetFileAttributesW(selected.c_str());
  if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY))
    return result.set("error", "选中的文件夹不存在或已不可用");
  PWSTR label = nullptr;
  std::string name;
  if (SUCCEEDED(item->GetDisplayName(SIGDN_NORMALDISPLAY, &label)) && label) name = utf8(label);
  if (label) CoTaskMemFree(label);
  return result.set("path", utf8(selected)).set("name", name);
}

bool system_dark() {
  DWORD light = 1, size = sizeof light;
  RegGetValueW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", L"AppsUseLightTheme",
               RRF_RT_REG_DWORD, nullptr, &light, &size);
  return light == 0;
}

// 和页面的背景一样 (workbench.css 的 --bg), 页面还没画出来时窗口不闪白
COLORREF background() { return system_dark() ? RGB(23, 24, 28) : RGB(247, 246, 250); }

}  // namespace

bool WorkbenchWindow::open() {
  if (hwnd_) {
    if (IsIconic(hwnd_)) ShowWindow(hwnd_, SW_RESTORE);
    SetForegroundWindow(hwnd_);
    return true;
  }
  static bool registered = false;
  if (!registered) {
    WNDCLASSEXW wc{sizeof wc};
    wc.lpfnWndProc = proc;
    wc.hInstance = app_.inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon = app_icon(GetSystemMetrics(SM_CXICON));
    wc.hIconSm = app_icon(GetSystemMetrics(SM_CXSMICON));
    wc.lpszClassName = L"WmPetWorkbench";
    RegisterClassExW(&wc);
    registered = true;
  }
  UINT dpi = app_.hwnd ? GetDpiForWindow(app_.hwnd) : 96;
  if (!dpi) dpi = 96;
  int w = MulDiv(1240, dpi, 96), h = MulDiv(820, dpi, 96);
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromWindow(app_.hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
  w = std::min<int>(w, mi.rcWork.right - mi.rcWork.left);
  h = std::min<int>(h, mi.rcWork.bottom - mi.rcWork.top);
  int x = (mi.rcWork.left + mi.rcWork.right - w) / 2, y = (mi.rcWork.top + mi.rcWork.bottom - h) / 2;
  hwnd_ = CreateWindowExW(0, L"WmPetWorkbench", L"工作台 · WorldModel 桌宠", WS_OVERLAPPEDWINDOW, x, y, w, h, nullptr, nullptr,
                          app_.inst, this);
  if (!hwnd_) {
    logf("建工作台窗口失败 %lu", GetLastError());
    return false;
  }
  BOOL dark = system_dark();
  DwmSetWindowAttribute(hwnd_, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof dark);
  ShowWindow(hwnd_, SW_SHOW);
  SetForegroundWindow(hwnd_);
  web_ = new WebViewHost();
  const std::wstring trusted_page = app_.opt.server + L"/pet/workbench.html";
  web_->allow_message = [trusted_page](const std::wstring& source) {
    return source == trusted_page || (source.size() > trusted_page.size() &&
        source.compare(0, trusted_page.size(), trusted_page) == 0 &&
        (source[trusted_page.size()] == L'?' || source[trusted_page.size()] == L'#'));
  };
  web_->allow_navigation = web_->allow_message;
  web_->on_message = [this](const std::string& m) { on_message(m); };
  WebViewHost::Config cfg;
  cfg.hwnd = hwnd_;
  cfg.devtools = app_.opt.devtools;
  cfg.server = app_.opt.server;
  cfg.web_dir = app_.opt.web_dir;
  cfg.page = L"workbench.html";
  cfg.page_query = app_.opt.workbench_query;
  cfg.user_data = full_path(exe_dir() + L"\\..\\webview2-data");
  cfg.transparent = false;
  cfg.background = background();
  cfg.share = &app_.web;
  HRESULT hr = web_->start(cfg);
  logf("打开工作台%s", FAILED(hr) ? " (WebView2 启动失败)" : "");
  iconic_ = false;
  app_.on_workbench(true);
  return true;
}

void WorkbenchWindow::show_settings(const std::string& tab) {
  open();
  Json m = Json::object();
  m.set("t", "wb").set("op", "settings").set("tab", tab);
  post(widen(m.dump()));
}

void WorkbenchWindow::set_topmost(bool on) {
  if (!hwnd_) return;
  SetWindowPos(hwnd_, on ? HWND_TOPMOST : HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  logf(on ? "工作台置顶" : "工作台取消置顶");
}

void WorkbenchWindow::choose_workspace_folder() {
  const std::string rid = folder_picker_request_;
  folder_picker_request_.clear();
  if (rid.empty() || !hwnd_ || !web_) {
    folder_picker_open_ = false;
    return;
  }
  auto* original_web = web_;
  const Json result = choose_workspace(hwnd_, rid);
  if (web_ == original_web) {
    folder_picker_open_ = false;
    post(widen(result.dump()));
  }
}

void WorkbenchWindow::activate_browser(const std::string& id) {
  browser_=nullptr;
  for(auto& pair:browsers_) {
    if(pair.first==id) browser_=pair.second.get();
    else pair.second->command(Json::object().set("action","hide"));
  }
}

void WorkbenchWindow::close_browsers() {
  for(auto& pair:browsers_) {pair.second->close();retired_browsers_.push_back(std::move(pair.second));}
  browsers_.clear();browser_=nullptr;
}

LRESULT CALLBACK WorkbenchWindow::proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  if (m == WM_NCCREATE) SetWindowLongPtrW(h, GWLP_USERDATA, LONG_PTR(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams));
  auto* self = reinterpret_cast<WorkbenchWindow*>(GetWindowLongPtrW(h, GWLP_USERDATA));
  switch (m) {
    case WM_CHOOSE_WORKSPACE:
      if (self) self->choose_workspace_folder();
      return 0;
    case WM_SIZE:
      if (self && self->web_) {
        RECT rc;
        GetClientRect(h, &rc);
        self->web_->resize(rc);
      }
      if (self && self->browser_) self->browser_->parent_moved();
      // 最小化了: 桌宠回到桌面; 还原: 再收起 (她在工作台里)
      if (self && (wp == SIZE_MINIMIZED) != self->iconic_) {
        self->iconic_ = wp == SIZE_MINIMIZED;
        self->app_.on_workbench(!self->iconic_);
      }
      return 0;
    case WM_MOVE:
      if (self && self->web_) self->web_->parent_moved();
      if (self && self->browser_) self->browser_->parent_moved();
      return 0;
    case WM_ERASEBKGND:
      return 1;
    case WM_PAINT: {
      PAINTSTRUCT ps;
      HDC dc = BeginPaint(h, &ps);
      HBRUSH b = CreateSolidBrush(background());
      FillRect(dc, &ps.rcPaint, b);
      DeleteObject(b);
      EndPaint(h, &ps);
      return 0;
    }
    case WM_DPICHANGED: {
      auto* r = reinterpret_cast<RECT*>(lp);
      SetWindowPos(h, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
      return 0;
    }
    case WM_GETMINMAXINFO: {
      UINT dpi = GetDpiForWindow(h);
      auto* mm = reinterpret_cast<MINMAXINFO*>(lp);
      mm->ptMinTrackSize = {MulDiv(860, dpi ? dpi : 96, 96), MulDiv(560, dpi ? dpi : 96, 96)};
      return 0;
    }
    case WM_TIMER:
      if (wp == T_SHOT && self) {
        KillTimer(h, T_SHOT);
        if (self->web_) {
          std::wstring path = self->app_.opt.workbench_shot;
          HWND app = self->app_.hwnd;
          self->web_->capture_png(path, [path, app](bool ok) {
            logf("工作台截图 %s: %s", utf8(path).c_str(), ok ? "好了" : "失败");
            PostMessageW(app, WM_CLOSE, 0, 0);
          });
        }
      }
      return 0;
    case WM_DESTROY:
      if (self) {
        self->close_browsers();
        if (self->web_) self->web_->close();
        self->web_ = nullptr;  // 不释放: WebView2 关掉之后还可能有回调进来, 留着这个小对象比悬空指针安全
        self->hwnd_ = nullptr;
        self->folder_picker_open_ = false;
        self->folder_picker_request_.clear();
        logf("工作台关了");
        self->app_.on_workbench(false);
      }
      return 0;
  }
  return DefWindowProcW(h, m, wp, lp);
}

void WorkbenchWindow::on_message(const std::string& text) {
  if (text.rfind("panel ", 0) == 0) {  // 工作台里的设置页 (iframe) 经页面转来的
    app_.panel_message(text, web_);
    return;
  }
  if (text.rfind("workbench ", 0) != 0) return;
  Json msg;
  if (!Json::parse(text.substr(10), msg) || !msg.is_object()) return;
  std::string op = msg.get_str("op");
  if(op=="reveal_file") {
    const auto rid=msg.get_str("rid");if(!request_id(rid))return;
    const bool ok=reveal_workspace_path(widen(msg.get_str("workspace")),widen(msg.get_str("path")));
    Json result=Json::object();result.set("t","workbench-file-revealed").set("rid",rid).set("ok",ok);
    if(!ok)result.set("error","无法显示此文件；请确认它存在于当前工作区内");
    post(widen(result.dump()));return;
  }
  if (op == "browser") {
    const auto action = msg.get_str("action");
    const auto id=msg.get_str("tab_id","main");
    if(!request_id(id)) return;
    if (action == "external") {
      const auto url = widen(msg.get_str("url"));
      const bool allowed = BrowserPane::allowed_url(url, {BrowserPane::url_port(app_.opt.server), app_.opt.api_port});
      if (allowed) {
        const auto result = reinterpret_cast<INT_PTR>(ShellExecuteW(hwnd_, L"open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL));
        if (result > 32) return;
      }
      Json status = Json::object();
      status.set("t", "workbench-browser").set("tab_id",id).set("state", "error").set("url", utf8(url))
          .set("error", allowed ? "系统浏览器未能打开，请检查默认浏览器设置" : "仅支持普通 HTTP / HTTPS 网页");
      post(widen(status.dump()));
      return;
    }
    if(action=="hide" && !msg.has("tab_id")) {
      for(auto& pair:browsers_) pair.second->command(msg);
      return;
    }
    if (!browsers_.count(id) && action == "navigate" && hwnd_) {
      browsers_[id]=std::make_unique<BrowserPane>(hwnd_, full_path(exe_dir() + L"\\..\\browser-data"),
          std::set<int>{BrowserPane::url_port(app_.opt.server), app_.opt.api_port});
      browsers_[id]->on_state = [this,id](const Json& status) {Json reply=status;reply.set("tab_id",id);post(widen(reply.dump()));};
    }
    const auto found=browsers_.find(id);
    if(found!=browsers_.end()) {
      auto* target=found->second.get();
      if(action=="navigate" || action=="activate" || (action=="bounds" && msg["visible"].boolean(true))) activate_browser(id);
      target->command(msg);
      if(action=="close") {
        if(browser_==target) browser_=nullptr;
        retired_browsers_.push_back(std::move(found->second));browsers_.erase(found);
      }
    }
    return;
  }
  if (op == "choose_workspace_folder") {
    const std::string rid = msg.get_str("rid");
    if (!request_id(rid) || !hwnd_ || !web_) return;
    if (folder_picker_open_) {
      Json result = Json::object();
      result.set("t", "workbench-folder").set("rid", rid).set("cancelled", false).set("error", "请先完成已打开的文件夹选择");
      post(widen(result.dump()));
      return;
    }
    folder_picker_open_ = true;
    folder_picker_request_ = rid;
    // Leave the WebView callback before entering the native modal message loop;
    // WebView2 does not support reentrant modal dialogs from its event handlers.
    if (!PostMessageW(hwnd_, WM_CHOOSE_WORKSPACE, 0, 0)) {
      folder_picker_open_ = false;
      folder_picker_request_.clear();
      Json result = Json::object();
      result.set("t", "workbench-folder").set("rid", rid).set("cancelled", false).set("error", "文件夹选择器未能打开，请重试");
      post(widen(result.dump()));
    }
    return;
  }
  if (op == "open_session_browser") {
    const std::string rid = msg.get_str("rid"), id = msg.get_str("id");
    if (!request_id(rid)) return;
    Json result = Json::object();
    result.set("t", "workbench-opened").set("rid", rid).set("ok", false);
    if (!request_id(id)) result.set("error", "会话编号无效");
    else {
      const std::wstring url = app_.opt.server + L"/agent?s=" + widen(id);
      const auto code = reinterpret_cast<INT_PTR>(ShellExecuteW(hwnd_, L"open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL));
      if (code > 32) result.set("ok", true);
      else result.set("error", "系统浏览器未能打开，请检查默认浏览器设置");
    }
    post(widen(result.dump()));
    return;
  }
  if (op == "autonomy_changed") {
    if (app_.tools) app_.tools->refresh();
    return;
  }
  if (op == "dock") {  // 右边的功能键
    app_.dock_action(msg.get_str("act"), msg.get_str("id"));
    return;
  }
  if (op == "ready") {
    Json capabilities = Json::object();
    capabilities.set("t", "workbench-capabilities").set("version", 3).set("folder_picker", true).set("open_session_browser", true)
        .set("native_browser", true).set("browser_tabs", true).set("browser_zoom", true).set("browser_selection", true).set("reveal_file", true);
    web_->post_json(widen(capabilities.dump()));
    Json status = Json::object();
    status.set("t", "autonomy-host").set("version", 1).set("scope", "native-tools");
    web_->post_json(widen(status.dump()));
    logf("工作台页面就绪");
    app_.send_dock();
    // 自检截图: 等桌前的她加载完 (下面的 desk) 再拍; 她一直没消息就 20 秒后照拍
    if (!app_.opt.workbench_shot.empty() && !shot_done_ && hwnd_) SetTimer(hwnd_, T_SHOT, 20000, nullptr);
  } else if (op == "desk") {
    logf("工作台: 桌前的她%s", msg["ok"].boolean() ? "坐好了" : "没加载 (收起了或加载失败)");
    if (!app_.opt.workbench_shot.empty() && !shot_done_ && hwnd_) {
      shot_done_ = true;
      SetTimer(hwnd_, T_SHOT, 1500, nullptr);
    }
  } else if (op == "log") {
    logf("[工作台] %s", msg.get_str("text").c_str());
  }
}
