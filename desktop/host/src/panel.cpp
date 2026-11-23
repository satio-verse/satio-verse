#include "panel.h"

#include <dwmapi.h>
#include <shellapi.h>
#include <shobjidl.h>

#include <algorithm>
#include <cstdio>
#include <thread>

#include "api.h"
#include "app.h"
#include "builtins.h"
#include "extpack.h"
#include "guard.h"
#include "mcp_client.h"
#include "settings.h"

namespace {

constexpr UINT_PTR T_SHOT = 1;
constexpr UINT_PTR T_CLOSE = 2;  // 关窗时等页面存完的上限

bool system_dark() {
  DWORD light = 1, size = sizeof light;
  RegGetValueW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", L"AppsUseLightTheme",
               RRF_RT_REG_DWORD, nullptr, &light, &size);
  return light == 0;
}

COLORREF panel_background() { return system_dark() ? RGB(32, 33, 36) : RGB(247, 247, 248); }

// "另存为": 让用户自己选存在哪 (导出她的经历这类, 面板给内容, 宿主弹系统的保存对话框). GUID 自己写, 不依赖 uuid 库里有没有
const CLSID kClsidFileSaveDialog = {0xc0b4e2f3, 0xba21, 0x4773, {0x8d, 0xba, 0x33, 0x5e, 0xc9, 0x46, 0xeb, 0x8b}};
const IID kIidIFileSaveDialog = {0x84bccd23, 0x5fde, 0x4cdb, {0xae, 0xa4, 0xaf, 0x64, 0xb8, 0x3d, 0x78, 0xab}};
constexpr size_t kSaveMax = 64u << 20;  // 一次最多存多大 (面板经 postMessage 传 base64 过来)

// 选好了返回 true 和路径; 用户取消或对话框起不来返回 false (why 里写原因, 取消时为空)
bool save_dialog(HWND owner, const std::wstring& name, const std::wstring& ext, std::wstring& path, std::string& why) {
  IFileSaveDialog* d = nullptr;
  if (FAILED(CoCreateInstance(kClsidFileSaveDialog, nullptr, CLSCTX_INPROC_SERVER, kIidIFileSaveDialog, reinterpret_cast<void**>(&d)))) {
    why = "保存对话框打不开";
    return false;
  }
  std::wstring label = ext == L"zip" ? L"ZIP 压缩包" : L"文件", pattern = L"*." + ext;
  COMDLG_FILTERSPEC spec[] = {{label.c_str(), pattern.c_str()}};
  d->SetFileTypes(1, spec);
  d->SetDefaultExtension(ext.c_str());
  d->SetFileName(name.c_str());
  DWORD flags = 0;
  if (SUCCEEDED(d->GetOptions(&flags))) d->SetOptions(flags | FOS_OVERWRITEPROMPT | FOS_FORCEFILESYSTEM);
  bool ok = false;
  if (SUCCEEDED(d->Show(owner))) {
    IShellItem* item = nullptr;
    if (SUCCEEDED(d->GetResult(&item))) {
      PWSTR p = nullptr;
      if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &p))) {
        path = p;
        CoTaskMemFree(p);
        ok = true;
      }
      item->Release();
    }
  }
  d->Release();
  return ok;
}

// 云端提供方的模型列表: GET <base_url>/models, 带 Bearer key. 用户在面板上点"取模型列表"时才发
int http_get(const std::string& url, const std::string& bearer, std::string& body, std::string& error) {
  std::wstring u = widen(url);
  URL_COMPONENTS uc{sizeof uc};
  wchar_t host[256] = L"", path[2048] = L"";
  uc.lpszHostName = host;
  uc.dwHostNameLength = 256;
  uc.lpszUrlPath = path;
  uc.dwUrlPathLength = 2048;
  if (u.empty() || !WinHttpCrackUrl(u.c_str(), 0, 0, &uc)) {
    error = "地址不对: " + url;
    return 0;
  }
  HINTERNET s = WinHttpOpen(L"wmpet-panel", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
  HINTERNET c = s ? WinHttpConnect(s, host, uc.nPort, 0) : nullptr;
  HINTERNET r = c ? WinHttpOpenRequest(c, L"GET", path, nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                       uc.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0)
                  : nullptr;
  int status = 0;
  if (r) {
    WinHttpSetTimeouts(r, 5000, 10000, 15000, 20000);
    std::wstring h = L"Accept: application/json\r\n";
    if (!bearer.empty()) h += L"Authorization: Bearer " + widen(bearer) + L"\r\n";
    if (WinHttpSendRequest(r, h.c_str(), DWORD(-1), WINHTTP_NO_REQUEST_DATA, 0, 0, 0) && WinHttpReceiveResponse(r, nullptr)) {
      DWORD code = 0, size = sizeof code;
      WinHttpQueryHeaders(r, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &code, &size,
                          WINHTTP_NO_HEADER_INDEX);
      status = int(code);
      char buf[8192];
      DWORD got = 0;
      while (WinHttpReadData(r, buf, sizeof buf, &got) && got) body.append(buf, got);
    } else {
      error = "连不上 " + utf8(host) + " (错误 " + std::to_string(GetLastError()) + ")";
    }
  }
  if (r) WinHttpCloseHandle(r);
  if (c) WinHttpCloseHandle(c);
  if (s) WinHttpCloseHandle(s);
  return status;
}

// 测试连接的结果: 工具只留面板要显示的几项
Json tool_summary(const Json& tools, const std::string& kind) {
  Json out = Json::array();
  for (const Json& t : tools.items()) {
    Json one = Json::object();
    std::string desc = t.get_str("description");
    if (desc.size() > 400) desc = desc.substr(0, 400) + "…";
    one.set("name", t.get_str("name")).set("title", t["title"]).set("description", desc).set("annotations", t["annotations"]);
    if (!kind.empty()) one.set("tier", guard::tier_name(guard::tier(kind, t.get_str("name"))));  // 操控类: 四档 (面板据此限制权限)
    out.push(one);
  }
  return out;
}

Json probe(Json cfg, const std::string& tool, const Json& args) {
  McpClient c;
  std::string error;
  Json r = Json::object();
  ULONGLONG t0 = GetTickCount64();
  std::string kind = cfg.get_str("kind");
  Json forced = guard::env(kind, settings::load()["guard"]);  // 和真正调用时一样 (tools.cpp); 先存下来, 直接遍历临时对象的成员会悬空
  for (const auto& kv : forced.members()) cfg.at("env").set(kv.first, kv.second);
  if (!c.connect(cfg, error, guard::connect_timeout_ms(kind), guard::discover_timeout_ms(kind))) {
    r.set("ok", false).set("error", error);
    return r;
  }
  Json tools = c.list_tools(error);
  r.set("ok", !tools.is_null()).set("version", c.version()).set("modern", c.modern()).set("server_info", c.server_info);
  std::string ins = c.instructions;
  if (ins.size() > 600) ins = ins.substr(0, 600) + "…";
  r.set("instructions", ins).set("tools", tool_summary(tools, kind)).set("ms", double(GetTickCount64() - t0));
  if (tools.is_null()) r.set("error", error);
  if (!tool.empty() && !tools.is_null()) {
    Json res = c.call_tool(tool, args, error);
    r.set("call", res);
    if (res.is_null()) r.set("call_error", error);
  }
  c.close();
  return r;
}

void append_line(const std::wstring& path, const std::string& line) {
  if (path.empty()) return;
  FILE* f = _wfopen(path.c_str(), L"ab");
  if (!f) return;
  fwrite(line.data(), 1, line.size(), f);
  fputc('\n', f);
  fclose(f);
}

}  // namespace

void PanelWindow::open() {
  if (hwnd_) {
    ShowWindow(hwnd_, SW_RESTORE);
    SetForegroundWindow(hwnd_);
    return;
  }
  static bool registered = false;
  if (!registered) {
    WNDCLASSEXW wc{sizeof wc};
    wc.lpfnWndProc = proc;
    wc.hInstance = app_.inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon = app_icon(GetSystemMetrics(SM_CXICON));
    wc.hIconSm = app_icon(GetSystemMetrics(SM_CXSMICON));
    wc.lpszClassName = L"WmPetPanel";
    RegisterClassExW(&wc);
    registered = true;
  }
  UINT dpi = app_.hwnd ? GetDpiForWindow(app_.hwnd) : 96;
  if (!dpi) dpi = 96;
  int w = MulDiv(1000, dpi, 96), h = MulDiv(720, dpi, 96);
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromWindow(app_.hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
  w = std::min<int>(w, mi.rcWork.right - mi.rcWork.left);
  h = std::min<int>(h, mi.rcWork.bottom - mi.rcWork.top);
  int x = (mi.rcWork.left + mi.rcWork.right - w) / 2, y = (mi.rcWork.top + mi.rcWork.bottom - h) / 2;
  hwnd_ = CreateWindowExW(0, L"WmPetPanel", L"WorldModel 桌宠 · 设置", WS_OVERLAPPEDWINDOW, x, y, w, h, nullptr, nullptr, app_.inst, this);
  if (!hwnd_) {
    logf("建操作面板窗口失败 %lu", GetLastError());
    return;
  }
  BOOL dark = system_dark();
  DwmSetWindowAttribute(hwnd_, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof dark);
  ShowWindow(hwnd_, SW_SHOW);
  SetForegroundWindow(hwnd_);
  web_ = new WebViewHost();
  web_->on_message = [this](const std::string& m) {
    sink_ = web_;
    on_message(m);
  };
  WebViewHost::Config cfg;
  cfg.hwnd = hwnd_;
  cfg.devtools = app_.opt.devtools;
  cfg.server = app_.opt.server;
  cfg.web_dir = app_.opt.web_dir;
  cfg.page = L"panel.html";
  cfg.page_query = app_.opt.panel_query;
  cfg.user_data = full_path(exe_dir() + L"\\..\\webview2-data");
  cfg.transparent = false;
  cfg.background = panel_background();
  cfg.share = &app_.web;
  HRESULT hr = web_->start(cfg);
  logf("打开操作面板%s", FAILED(hr) ? " (WebView2 启动失败)" : "");
}

LRESULT CALLBACK PanelWindow::proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  if (m == WM_NCCREATE) SetWindowLongPtrW(h, GWLP_USERDATA, LONG_PTR(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams));
  auto* self = reinterpret_cast<PanelWindow*>(GetWindowLongPtrW(h, GWLP_USERDATA));
  switch (m) {
    case WM_SIZE:
      if (self && self->web_) {
        RECT rc;
        GetClientRect(h, &rc);
        self->web_->resize(rc);
      }
      return 0;
    case WM_MOVE:
      if (self && self->web_) self->web_->parent_moved();
      return 0;
    case WM_ERASEBKGND:
      return 1;
    case WM_PAINT: {
      PAINTSTRUCT ps;
      HDC dc = BeginPaint(h, &ps);
      HBRUSH b = CreateSolidBrush(panel_background());
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
      mm->ptMinTrackSize = {MulDiv(760, dpi ? dpi : 96, 96), MulDiv(520, dpi ? dpi : 96, 96)};
      return 0;
    }
    case WM_CLOSE:
      // 先让页面把还没存的改动存掉 (改了之后停手 0.6 秒才存, 改完马上关窗就丢了), 页面回 close-ok 再关; 1.5 秒没回也关
      if (self && self->web_ && !self->closing_) {
        self->closing_ = true;
        Json m = Json::object();
        m.set("op", "closing");
        self->reply(m);
        SetTimer(h, T_CLOSE, 1500, nullptr);
        return 0;
      }
      DestroyWindow(h);
      return 0;
    case WM_TIMER:
      if (wp == T_SHOT && self) {
        KillTimer(h, T_SHOT);
        self->shot_next();
      } else if (wp == T_CLOSE) {
        KillTimer(h, T_CLOSE);
        DestroyWindow(h);
      }
      return 0;
    case WM_DESTROY:
      if (self) {
        if (self->web_) self->web_->close();
        self->web_ = nullptr;  // 不释放: WebView2 关掉之后还可能有回调进来, 留着这个小对象比悬空指针安全
        self->hwnd_ = nullptr;
        self->closing_ = false;
        logf("操作面板关了");
      }
      return 0;
  }
  return DefWindowProcW(h, m, wp, lp);
}

void PanelWindow::reply(Json r) {
  r.set("t", "panel");
  if (WebViewHost* to = sink_ ? sink_ : web_) to->post_json(widen(r.dump()));
}

void PanelWindow::notify_changed() {
  std::wstring m = L"{\"t\":\"panel\",\"op\":\"changed\"}";
  if (web_) web_->post_json(m);  // 没开时 web_ 是空的
  if (embed_) embed_->post_json(m);
}

void PanelWindow::reply_later(Json r) {
  WebViewHost* to = sink_;  // 工作线程做完时回给发请求的那边
  app_.ui([this, r, to] {
    sink_ = to;
    reply(r);
  });
}

void PanelWindow::handle(const std::string& text, WebViewHost* from) {
  sink_ = embed_ = from;
  on_message(text);
}

void PanelWindow::close() {
  if (hwnd_) PostMessageW(hwnd_, WM_CLOSE, 0, 0);
}

void PanelWindow::on_message(const std::string& text) {
  if (text.rfind("panel ", 0) != 0) return;
  Json msg;
  if (!Json::parse(text.substr(6), msg) || !msg.is_object()) return;
  std::string op = msg.get_str("op");
  Json out = Json::object();
  out.set("id", msg["id"]).set("op", op);
  if (op == "close-ok") {  // 关窗前没存的改动存完了 (WM_CLOSE 发的 closing)
    if (closing_ && hwnd_) {
      KillTimer(hwnd_, T_CLOSE);
      DestroyWindow(hwnd_);
    }
    return;
  } else if (op == "ready") {
    logf("操作面板就绪%s", sink_ != web_ ? " (工作台里)" : "");
    if (!app_.opt.panel_shot.empty() && sink_ == web_ && web_) {
      shot_tabs_ = msg["tabs"];
      shot_index_ = 0;
      shot_next();
    }
  } else if (op == "get") {
    out.set("ok", true).set("settings", settings::for_panel(settings::load())).set("skills", settings::scan_skills());
    out.set("skills_dir", utf8(settings::skills_dir()));
    // 门面: 3D 总有; 2D 要先跑 desktop/scripts/prepare_live2d.py 把 SDK 和模型取出来
    std::wstring l2d = app_.opt.web_dir + L"\\..\\third_party\\live2d-models\\Eustia\\pet-model.json";
    Json faces = Json::array(), vrm = Json::object(), live2d = Json::object();
    vrm.set("id", "vrm").set("name", "3D (VRM)").set("ok", true);
    live2d.set("id", "live2d").set("name", "2D (Live2D): 尤斯蒂娅").set("ok", GetFileAttributesW(l2d.c_str()) != INVALID_FILE_ATTRIBUTES);
    faces.push(vrm);
    faces.push(live2d);
    out.set("faces", faces);
    reply(out);
  } else if (op == "save") {
    Json merged = settings::merge(settings::load(), msg["settings"], msg["keys"], msg["secrets"]);
    bool ok = settings::save(merged);
    out.set("ok", ok).set("settings", settings::for_panel(merged));
    if (!ok) out.set("error", "设置文件写不进去: " + utf8(settings::path()));
    reply(out);
    logf("操作面板: 设置%s", ok ? "已保存" : "保存失败");
    app_.send_profile();  // 当前人设、模型可能变了: 告诉桌宠页面
  } else if (op == "computer-preset") {  // "添加: 操控电脑": Windows-MCP 装在哪、怎么起 (锁定的工具在启动参数里就去掉)
    std::wstring exe = full_path(app_.opt.web_dir + L"\\..\\third_party\\windows-mcp\\.venv\\Scripts\\windows-mcp.exe");
    Json args = Json::array();
    args.push("serve").push("--tools").push("ControlStatus,DisplayInventory,Wait,Screenshot,Snapshot,WaitFor,Click,Type,Scroll,Move,Shortcut,App");
    out.set("ok", GetFileAttributesW(exe.c_str()) != INVALID_FILE_ATTRIBUTES).set("command", utf8(exe)).set("args", args);
    reply(out);
  } else if (op == "mcp-test") {
    std::string id = msg.get_str("server");
    std::thread([this, id, out]() mutable {
      std::string error;
      Json cfg = settings::mcp_server(settings::load(), id, &error);
      Json r = cfg.is_null() ? Json::object() : probe(cfg, "", Json());
      if (cfg.is_null()) r.set("ok", false).set("error", error);
      for (const auto& kv : r.members()) out.set(kv.first, kv.second);
      logf("操作面板: 测试 MCP 服务器 %s: %s", cfg.get_str("name").c_str(),
           r["ok"].boolean() ? ("连上了, 协议 " + r.get_str("version") + ", " + std::to_string(r["tools"].items().size()) + " 个工具").c_str()
                             : r.get_str("error").c_str());
      reply_later(out);
    }).detach();
  } else if (op == "models") {
    std::string provider = msg.get_str("provider");
    std::thread([this, provider, out]() mutable {
      Json s = settings::load();
      const Json& p = s["providers"][provider];
      std::string key, body, error;
      if (!p.get_str("key").empty()) settings::unprotect(p.get_str("key"), key);
      std::string base = p.get_str("base_url");
      while (!base.empty() && base.back() == '/') base.pop_back();
      int status = base.empty() ? 0 : http_get(base + "/models", key, body, error);
      Json parsed, models = Json::array();
      if (status == 200 && Json::parse(body, parsed))
        for (const Json& m : parsed["data"].items()) models.push(m.get_str("id"));
      bool ok = status == 200;
      if (base.empty()) error = "先填地址";
      else if (status == 401 || status == 403) error = "key 不对或没有权限 (HTTP " + std::to_string(status) + ")";
      else if (status && !ok) error = "HTTP " + std::to_string(status) + ": " + body.substr(0, 200);
      out.set("ok", ok).set("models", models).set("error", error);
      logf("操作面板: 取 %s 的模型列表: %s", provider.c_str(), ok ? (std::to_string(models.items().size()) + " 个").c_str() : error.c_str());
      reply_later(out);
    }).detach();
  } else if (op == "skills") {
    out.set("ok", true).set("skills", settings::scan_skills()).set("skills_dir", utf8(settings::skills_dir()));
    reply(out);
  } else if (op == "skill-read") {  // Skill 页: 打开一个来改 (随附的也能读, 存的时候存到用户文件夹)
    Json sk = settings::read_skill(msg.get_str("name"));
    out.set("ok", sk.is_object()).set("skill", sk);
    if (!sk.is_object()) out.set("error", "没有这个 skill");
    reply(out);
  } else if (op == "skill-save" || op == "skill-delete") {
    std::string error, name = msg.get_str("name");
    bool ok = op == "skill-save" ? settings::save_skill(name, msg.get_str("description"), msg.get_str("body"), error)
                                 : settings::delete_skill(name, error);
    logf("操作面板: %s skill %s: %s", op == "skill-save" ? "保存" : "删除", name.c_str(), ok ? "好了" : error.c_str());
    out.set("ok", ok).set("error", error).set("skills", settings::scan_skills());
    reply(out);
  } else if (op == "qq-status") {  // QQ 页: 桌宠页面报回的最后一份 channel_status (没有就是 null)
    Json st;
    out.set("ok", true).set("status", Json::parse(app_.qq_status, st) ? st : Json());
    if (app_.api) out.set("guest", app_.api->guest_stats());  // 本机接口开过才有: wm-guest 的请求数、带了工具的次数
    // 外部渠道页的"怎么接": 本机接口开没开、端口、访客 token 文件在哪 (不给 token 本身)
    Json api = Json::object();
    std::wstring guest_token = api_guest_token_path();
    api.set("on", app_.api_on).set("port", app_.opt.api_port).set("token_path", utf8(guest_token));
    api.set("token_exists", GetFileAttributesW(guest_token.c_str()) != INVALID_FILE_ATTRIBUTES);
    out.set("api", api);
    reply(out);
  } else if (op == "save-as") {  // 存一个文件, 位置由用户在保存对话框里选: {name, ext, data: base64}. 存好在资源管理器里选中它
    std::vector<uint8_t> bytes;
    std::string ext = msg.get_str("ext", "zip"), why;
    std::wstring path;
    bool safe_ext = !ext.empty() && ext.size() <= 8 && std::all_of(ext.begin(), ext.end(), [](char c) { return isalnum(static_cast<unsigned char>(c)); });
    if (!safe_ext || !base64_decode(msg.get_str("data"), bytes) || bytes.empty() || bytes.size() > kSaveMax) {
      out.set("ok", false).set("error", "要存的内容不对 (空的、太大或不是 base64)");
    } else if (!save_dialog(hwnd_, widen(msg.get_str("name", "export")), widen(ext), path, why)) {
      out.set("ok", false).set("cancelled", why.empty()).set("error", why);
    } else {
      HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
      DWORD wrote = 0;
      bool ok = f != INVALID_HANDLE_VALUE && WriteFile(f, bytes.data(), DWORD(bytes.size()), &wrote, nullptr) && wrote == bytes.size();
      if (f != INVALID_HANDLE_VALUE) CloseHandle(f);
      out.set("ok", ok).set("path", utf8(path)).set("bytes", double(bytes.size()));
      if (!ok) out.set("error", "写不进去: " + utf8(path));
      else ShellExecuteW(nullptr, L"open", L"explorer.exe", (L"/select,\"" + path + L"\"").c_str(), nullptr, SW_SHOWNORMAL);
      logf("操作面板: 另存为 %s (%zu 字节)%s", utf8(path).c_str(), bytes.size(), ok ? "" : " 没写成");
    }
    reply(out);
  } else if (op == "show-guest-token") {  // 在资源管理器里选中访客 token 文件 (面板不经手 token)
    std::wstring arg = L"/select,\"" + api_guest_token_path() + L"\"";
    ShellExecuteW(nullptr, L"open", L"explorer.exe", arg.c_str(), nullptr, SW_SHOWNORMAL);
    out.set("ok", true);
    reply(out);
  } else if (op == "ext-state") {  // 扩展页: 装过的内容包、提醒、导出放哪
    Json s = settings::load();
    out.set("ok", true).set("extensions", s["extensions"].is_array() ? s["extensions"] : Json::array());
    out.set("reminders", builtins::reminders().list()).set("exports_dir", utf8(extpack::exports_dir()));
    reply(out);
  } else if (op == "ext-inspect") {  // 导入之前先看: 校验格式和 SHA-256, 全文、可疑的话、冲突
    std::string error;
    Json r = extpack::inspect(msg.get_str("text"), error);
    out.set("ok", !r.is_null()).set("preview", r).set("error", error);
    reply(out);
  } else if (op == "ext-install" || op == "ext-uninstall") {  // 卸载的包 id 放在 pack 里 (id 是面板请求的配对号)
    std::string error;
    bool ok = op == "ext-install" ? !extpack::install(msg.get_str("text"), error).is_null() : extpack::uninstall(msg.get_str("pack"), error);
    out.set("ok", ok).set("error", error).set("settings", settings::for_panel(settings::load()));
    reply(out);
    if (ok) app_.send_profile();  // 人设列表变了: 侧菜单、工具表跟着变
  } else if (op == "ext-export") {
    std::string error;
    Json r = extpack::export_pack(msg["pack"], error);
    out.set("ok", !r.is_null()).set("result", r).set("error", error);
    reply(out);
  } else if (op == "ext-open-exports") {
    CreateDirectoryW(extpack::exports_dir().c_str(), nullptr);
    ShellExecuteW(nullptr, L"open", extpack::exports_dir().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
  } else if (op == "audit") {  // 调用记录: 审计日志最新的几条
    out.set("ok", true).set("lines", extpack::audit_tail(std::clamp(int(msg["limit"].number(200)), 1, 1000)));
    reply(out);
  } else if (op == "reminder-cancel") {
    out.set("ok", builtins::reminders().cancel(msg.get_str("reminder"))).set("reminders", builtins::reminders().list());
    reply(out);
  } else if (op == "open-skills") {
    settings::scan_skills();  // 顺便把文件夹建好
    ShellExecuteW(nullptr, L"open", settings::skills_dir().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
  } else if (op == "selftest") {  // 结果写进 --out; 不退出, 测试脚本接着从外面查 (pet_state、设置文件), 查完自己关
    append_line(app_.opt.out, msg["result"].dump());
    logf("操作面板自检结束");
  } else if (op == "log") {
    logf("[面板] %s", msg.get_str("text").c_str());
  } else if (op == "pet-get") {  // "桌宠与工作台"页: 托盘里那些开关
    out.set("ok", true).set("pet", app_.pet_prefs());
    reply(out);
  } else if (op == "pet-set") {
    app_.set_pet_pref(msg.get_str("key"), msg["value"]);
    out.set("ok", true).set("pet", app_.pet_prefs());
    reply(out);
  } else if (op == "open-workbench") {  // 左边导航的"工作台"
    app_.open_workbench();
  }
}

// 自检截图: 让页面切到下一页, 等它画好再用 WebView2 自己的截图 (不受窗口遮挡影响), 全截完就退出
void PanelWindow::shot_next() {
  if (!web_) return;
  auto show_next = [this] {
    const auto& tabs = shot_tabs_.items();
    if (shot_index_ >= tabs.size()) {
      PostMessageW(app_.hwnd, WM_CLOSE, 0, 0);
      return;
    }
    Json show = Json::object();
    show.set("op", "show").set("tab", tabs[shot_index_].str());
    reply(show);
    shot_index_++;
    SetTimer(hwnd_, T_SHOT, 1500, nullptr);
  };
  if (shot_index_ == 0) {
    show_next();
    return;
  }
  // 先截好这一页, 截完再切下一页 (截图是异步的, 先切就会截到下一页)
  std::wstring path = app_.opt.panel_shot + L"-" + widen(shot_tabs_.items()[shot_index_ - 1].str()) + L".png";
  web_->capture_png(path, [show_next, path](bool ok) {
    logf("操作面板截图 %s: %s", utf8(path).c_str(), ok ? "好了" : "失败");
    show_next();
  });
}

int run_mcp_probe(const std::wstring& cfg_path, const std::string& tool, const std::wstring& args_path) {
  std::vector<char> data;
  Json cfg, args = Json::object();
  HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
  auto write = [out](const std::string& s) {
    DWORD n = 0;
    std::string line = s + "\n";
    if (out && out != INVALID_HANDLE_VALUE) WriteFile(out, line.data(), DWORD(line.size()), &n, nullptr);
  };
  if (!read_file(cfg_path, data) || !Json::parse(std::string(data.begin(), data.end()), cfg)) {
    write("{\"ok\":false,\"error\":\"配置文件读不了\"}");
    return 2;
  }
  if (!args_path.empty() && read_file(args_path, data)) Json::parse(std::string(data.begin(), data.end()), args);
  Json r = probe(cfg, tool, args);
  write(r.dump());
  return r["ok"].boolean() ? 0 : 1;
}
