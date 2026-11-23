// wmpet: 桌宠宿主.
//   wmpet.exe [--server http://127.0.0.1:8790] [--host hwnd|comp] [--ct layered|toggle|plain] [--devtools]
//             [--query "页面参数"] [--memory-level low|normal]
//             [--api [--api-port 8793]]   打开本机接口 (OpenAI 兼容 API 和 MCP, 默认关; 托盘里也能开关)
//             [--lean-after 15]   放着不动多少秒后趴在任务栏 (或窗口顶边) 上; 0 不趴, 自检默认不趴
//             [--selftest --out results.jsonl [--measure hidden,0,15,30,60 --measure-s 10 [--measure-film 4 --measure-film-ms 50]] [--ledge-test]]
//   wmpet.exe --mcp-stdio [--api-port 8793]   MCP 的 stdio 转发 (给只会起子进程的 MCP 客户端用), 不开窗口
#include <dwmapi.h>
#include <shellapi.h>
#include <windowsx.h>

#include <algorithm>
#include <cstdlib>
#include <cwchar>

#include "activity.h"
#include "api.h"
#include "app.h"
#include "ledge.h"
#include "panel.h"
#include "settings.h"
#include "tools.h"
#include "workbench.h"

namespace {

constexpr UINT WM_APP_TRAY = WM_APP + 1;
constexpr UINT WM_APP_CALL = WM_APP + 2;
constexpr UINT WM_APP_POST = WM_APP + 3;  // post_ui: 投递过来的 std::function (执行完删掉)
constexpr int HK_STOP = 1;                 // 一键停操控电脑的全局快捷键
constexpr UINT_PTR T_HIT = 1, T_DRAG = 2, T_FULL = 3, T_STAND = 4, T_ACT = 5;
enum { ID_TOGGLE = 100, ID_INPUT, ID_SETTINGS, ID_MIC, ID_MUTE, ID_PAUSE, ID_FPS10, ID_FPS15, ID_FPS30, ID_FPS60, ID_PIN, ID_FOLLOW,
       ID_LEAN, ID_ROOM, ID_ACTIVITY, ID_API, ID_RELOAD, ID_DEVTOOLS, ID_EXIT, ID_GRAVITY, ID_UNPLUG, ID_QQ_PAUSE, ID_WORKBENCH,
       ID_SVC_UP, ID_SVC_ALL, ID_SVC_STOP, ID_SVC_LOGS, ID_EXIT_STOP, ID_SVC_ITEM = 400 };  // ID_SVC_ITEM + i: 重启第 i 样服务

App g_app;

// 操控电脑期间看真人动没动鼠标键盘 (程序注入的输入带 INJECTED 标记, 不算): 点了鼠标、按了键就一键停.
// 点桌宠自己的窗口 (确认卡片、停)、在桌宠的输入框里打字不算. 钩子要尽快返回, 停的事投递到 UI 线程去做
HHOOK g_mouse_hook = nullptr, g_key_hook = nullptr;
bool g_input_fired = false;  // 这一次操控里已经喊过停了 (按住键会一直来)

bool own_window(HWND w) {
  DWORD pid = 0;
  if (w) GetWindowThreadProcessId(GetAncestor(w, GA_ROOT), &pid);
  return pid == GetCurrentProcessId();
}

void real_input(const char* what) {
  if (g_input_fired) return;
  g_input_fired = true;
  std::string why = what;
  g_app.post_ui([why] {
    if (g_app.tools) g_app.tools->emergency_stop(why);
  });
}

LRESULT CALLBACK mouse_hook(int code, WPARAM wp, LPARAM lp) {
  if (code == HC_ACTION && (wp == WM_LBUTTONDOWN || wp == WM_RBUTTONDOWN || wp == WM_MBUTTONDOWN || wp == WM_XBUTTONDOWN)) {
    auto* m = reinterpret_cast<MSLLHOOKSTRUCT*>(lp);
    if (!(m->flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)) && !own_window(WindowFromPoint(m->pt))) real_input("你点了鼠标");
  }
  return CallNextHookEx(nullptr, code, wp, lp);
}

LRESULT CALLBACK key_hook(int code, WPARAM wp, LPARAM lp) {
  if (code == HC_ACTION && (wp == WM_KEYDOWN || wp == WM_SYSKEYDOWN)) {
    auto* k = reinterpret_cast<KBDLLHOOKSTRUCT*>(lp);
    if (!(k->flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) && !own_window(GetForegroundWindow())) real_input("你按了键盘");
  }
  return CallNextHookEx(nullptr, code, wp, lp);
}

UINT dpi_of(HWND h) {
  UINT d = h ? GetDpiForWindow(h) : 0;
  return d ? d : 96;
}

LRESULT CALLBACK pet_proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  App& a = g_app;
  if (m == a.taskbar_created && m) {  // 资源管理器重启后托盘图标要重新加
    a.tray_add();
    return 0;
  }
  switch (m) {
    case WM_SIZE: {
      RECT rc;
      GetClientRect(h, &rc);
      a.web.resize(rc);
      return 0;
    }
    case WM_MOVE:
      a.web.parent_moved();
      return 0;
    case WM_DPICHANGED: {
      auto* r = reinterpret_cast<RECT*>(lp);
      SetWindowPos(h, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
      return 0;
    }
    case WM_ERASEBKGND:
      return 1;
    case WM_PAINT: {
      // hwnd 宿主: 黑色在"模糊背后 + 空区域"下是全透明, 只有 WebView2 画出来的像素可见
      PAINTSTRUCT ps;
      HDC dc = BeginPaint(h, &ps);
      if (a.opt.host != L"comp") FillRect(dc, &ps.rcPaint, static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
      EndPaint(h, &ps);
      return 0;
    }
    case WM_MOUSEACTIVATE:
      return MA_NOACTIVATE;  // 点桌宠不抢走用户当前窗口的焦点
    case WM_SETCURSOR:
      if (a.web.set_cursor()) return TRUE;
      break;
    case WM_MOUSEMOVE: case WM_LBUTTONDOWN: case WM_LBUTTONUP: case WM_RBUTTONDOWN: case WM_RBUTTONUP:
    case WM_MBUTTONDOWN: case WM_MBUTTONUP: case WM_MOUSEWHEEL: case WM_MOUSELEAVE:
      if (a.web.forward_mouse(m, wp, lp)) return 0;
      break;
    case WM_TIMER:
      if (wp == T_HIT) a.update_click_through(), a.send_cursor();
      else if (wp == T_DRAG) a.drag_tick();
      else if (wp == T_FULL) a.check_fullscreen();
      else if (wp == T_STAND) a.stand_tick();
      else if (wp == T_ACT && a.activity) a.activity->tick();
      return 0;
    case WM_APP_TRAY:
      if (LOWORD(lp) == WM_RBUTTONUP || LOWORD(lp) == WM_CONTEXTMENU) a.tray_menu();
      else if (LOWORD(lp) == WM_LBUTTONDBLCLK) a.on_command(ID_TOGGLE);
      return 0;
    case WM_COMMAND:
      a.on_command(LOWORD(wp));
      return 0;
    case WM_APP_CALL:
      (*reinterpret_cast<std::function<void()>*>(lp))();
      return 0;
    case WM_APP_POST: {
      auto* fn = reinterpret_cast<std::function<void()>*>(lp);
      (*fn)();
      delete fn;
      return 0;
    }
    case WM_HOTKEY:
      if (wp == HK_STOP && a.tools) a.tools->emergency_stop("你按了 Ctrl+Alt+Shift+Backspace");
      return 0;
    case WM_DESTROY:
      logf("关闭窗口");
      if (a.nid.hWnd) Shell_NotifyIconW(NIM_DELETE, &a.nid);
      a.web.close();
      logf("页面已关");
      PostQuitMessage(0);
      return 0;
  }
  return DefWindowProcW(h, m, wp, lp);
}

void parse_args(Options& o) {
  int argc = 0;
  LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  for (int i = 1; i < argc; i++) {
    std::wstring k = argv[i];
    auto next = [&]() -> std::wstring { return i + 1 < argc ? argv[++i] : L""; };
    if (k == L"--server") o.server = next();
    else if (k == L"--web") o.web_dir = next();
    else if (k == L"--host") o.host = next();
    else if (k == L"--ct") o.ct = next();
    else if (k == L"--out") o.out = next();
    else if (k == L"--measure") o.measure = next();
    else if (k == L"--query") o.query = next();
    else if (k == L"--memory-level") o.memory_low = next() == L"low";
    else if (k == L"--ledge-test") o.ledge_test = true;
    else if (k == L"--fake-mic") o.fake_mic = full_path(next());
    else if (k == L"--film") o.film_s = _wtoi(next().c_str());
    else if (k == L"--measure-s") o.measure_s = _wtoi(next().c_str());
    else if (k == L"--measure-film") o.measure_film_s = _wtoi(next().c_str());
    else if (k == L"--measure-film-ms") o.measure_film_ms = (std::max)(10, _wtoi(next().c_str()));
    else if (k == L"--selftest") o.selftest = true;
    else if (k == L"--devtools") o.devtools = true;
    else if (k == L"--size") swscanf(next().c_str(), L"%dx%d", &o.width, &o.height);
    else if (k == L"--api") o.api = true;
    else if (k == L"--api-port") o.api_port = _wtoi(next().c_str());
    else if (k == L"--mcp-stdio") o.mcp_stdio = true;
    else if (k == L"--panel") o.panel_at_start = true;
    else if (k == L"--panel-query") o.panel_query = next();
    else if (k == L"--panel-shot") o.panel_shot = full_path(next()), o.panel_at_start = true;
    else if (k == L"--workbench") o.workbench_at_start = true;
    else if (k == L"--workbench-query") o.workbench_query = next();
    else if (k == L"--workbench-shot") o.workbench_shot = full_path(next()), o.workbench_at_start = true;
    else if (k == L"--settings-dir") o.settings_dir = full_path(next());
    else if (k == L"--mcp-probe") o.mcp_probe = full_path(next());
    else if (k == L"--mcp-call") o.mcp_call = utf8(next()), o.mcp_call_args = full_path(next());
    else if (k == L"--ask-shot") o.ask_shot = full_path(next());
    else if (k == L"--dry-open") o.dry_open = true;
    else if (k == L"--task-shot-any") o.task_shot_any = true;
    else if (k == L"--no-autohide") o.no_autohide = true;
    else if (k == L"--lean-after") o.lean_after_s = _wtoi(next().c_str());
    else if (k == L"--activity-test") o.activity_test = full_path(next());
    else if (k == L"--services") o.services = true, o.services_given = true;
    else if (k == L"--no-services") o.services = false, o.services_given = true;
    else if (k == L"--python") o.python = full_path(next());
  }
  LocalFree(argv);
  if (o.selftest && !o.services_given) o.services = false;  // 自检测资源, 别让看守在后台抢 CPU
  while (!o.server.empty() && o.server.back() == L'/') o.server.pop_back();
  // 页面目录默认是 desktop/web: 发布版在 build/bin (往上两级), 开发版在 build/dev/bin (往上三级)
  if (o.web_dir.empty()) {
    for (const wchar_t* up : {L"\\..\\..\\web", L"\\..\\..\\..\\web"}) {
      std::wstring dir = full_path(exe_dir() + up);
      if (GetFileAttributesW((dir + L"\\pet.html").c_str()) != INVALID_FILE_ATTRIBUTES) {
        o.web_dir = dir;
        break;
      }
    }
  }
  o.web_dir = full_path(o.web_dir);
}

// 不置顶时: 光标这一点上有没有别的窗口盖着桌宠 (从桌宠往上数 Z 序; 透明穿透的、隐藏的、被系统藏起来的不算)
bool covered_at(HWND self, POINT p) {
  for (HWND h = GetWindow(self, GW_HWNDPREV); h; h = GetWindow(h, GW_HWNDPREV)) {
    if (!IsWindowVisible(h) || IsIconic(h) || (GetWindowLongPtrW(h, GWL_EXSTYLE) & WS_EX_TRANSPARENT)) continue;
    BOOL cloaked = FALSE;
    DwmGetWindowAttribute(h, DWMWA_CLOAKED, &cloaked, sizeof cloaked);
    if (cloaked) continue;
    RECT r = visible_frame(h);
    if (PtInRect(&r, p)) return true;
  }
  return false;
}

void enable_per_monitor_dpi() {
  using Fn = BOOL(WINAPI*)(HANDLE);
  if (auto f = reinterpret_cast<Fn>(GetProcAddress(GetModuleHandleW(L"user32.dll"), "SetProcessDpiAwarenessContext")))
    f(reinterpret_cast<HANDLE>(-4));  // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
}

}  // namespace

void App::ui(std::function<void()> fn) {
  if (hwnd) SendMessageW(hwnd, WM_APP_CALL, 0, reinterpret_cast<LPARAM>(&fn));
}

void App::post_ui(std::function<void()> fn) {
  if (!hwnd) return;
  auto* p = new std::function<void()>(std::move(fn));
  if (!PostMessageW(hwnd, WM_APP_POST, 0, reinterpret_cast<LPARAM>(p))) delete p;
}

void App::set_stop_hotkey(bool on) {
  if (on == stop_hotkey) return;
  stop_hotkey = on;
  if (!on) {
    UnregisterHotKey(hwnd, HK_STOP);
    return;
  }
  bool ok = RegisterHotKey(hwnd, HK_STOP, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_BACK);
  logf("一键停的快捷键 Ctrl+Alt+Shift+Backspace %s", ok ? "注册好了" : "注册不上 (被别的程序占了), 只能点停");
}

void App::set_controlling(bool on) {
  controlling = on;
  if (!on) control_grace = GetTickCount64() + 5000;
  if (on && auto_hidden) {
    auto_hidden = false;
    logf("开始操作电脑: 不因为全屏程序隐藏");
    apply_visibility();
  }
}

void App::set_input_watch(bool on) {
  if (on == (g_mouse_hook != nullptr)) return;
  if (on) {
    g_input_fired = false;
    g_mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, mouse_hook, inst, 0);
    g_key_hook = SetWindowsHookExW(WH_KEYBOARD_LL, key_hook, inst, 0);
    logf("操控期间看真人输入: %s", g_mouse_hook && g_key_hook ? "开了" : "钩子装不上");
  } else {
    if (g_mouse_hook) UnhookWindowsHookEx(g_mouse_hook);
    if (g_key_hook) UnhookWindowsHookEx(g_key_hook);
    g_mouse_hook = g_key_hook = nullptr;
  }
}

void App::send_fps() {
  web.post_json(L"{\"t\":\"fps\",\"v\":" + std::to_wstring(effective_fps()) + L"}");
}

void App::apply_visibility() {
  bool show = !user_hidden && (!auto_hidden || peeking) && !wb_hidden;
  if (show) {
    ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    web.set_visible(true);
    // 全屏时露出对话栏: 要盖在全屏程序上面, 没置顶也临时置顶 (不抢焦点); 收回、正常显示时按原来的置顶设置
    if (peek_shown() || !topmost) SetWindowPos(hwnd, peek_shown() || topmost ? HWND_TOPMOST : HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  } else {
    // WebView2 不可见时会降低页面的调度; 工作台开着、麦克风开着时页面照常跑 (她在后台听、说), 只是窗口收起、不画
    web.set_visible(wb_hidden && mic_on && !user_hidden && !auto_hidden);
    ShowWindow(hwnd, SW_HIDE);
  }
  web.post_json(std::wstring(L"{\"t\":\"peek\",\"on\":") + (peek_shown() ? L"true" : L"false") + L"}");  // 页面据此只留对话栏、挪到右下角
  send_fps();
  if (mic_on) send_mic();
}

// 麦克风只在用户从托盘打开、而且桌宠显示着的时候采音: 隐藏或全屏程序 (游戏、演示) 时先停, 显示回来再开
void App::send_mic() {
  bool live = mic_on && !user_hidden && !auto_hidden;
  web.post_json(std::wstring(L"{\"t\":\"mic\",\"v\":") + (live ? L"true" : L"false") + L"}");
}

void App::update_tray_tip() {
  if (!nid.hWnd) return;
  lstrcpynW(nid.szTip, mic_on ? L"WorldModel 桌宠 · 麦克风开着" : L"WorldModel 桌宠", ARRAYSIZE(nid.szTip));
  nid.uFlags = NIF_TIP;
  Shell_NotifyIconW(NIM_MODIFY, &nid);
}

void App::set_click_through(bool on) {
  if (on == ct_on) return;
  ct_on = on;
  LONG_PTR ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
  LONG_PTR bits = opt.ct == L"toggle" ? (WS_EX_LAYERED | WS_EX_TRANSPARENT) : WS_EX_TRANSPARENT;
  ex = on ? (ex | bits) : (ex & ~bits);
  SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex);
}

// 光标在角色身上 (命中掩码里) 就接收鼠标, 否则让点击落到下面的窗口. 掩码太旧时默认穿透: 宁可点不中, 不挡住用户.
void App::update_click_through() {
  if (dragging || ct_frozen) return;
  // 按在角色身上还没松手 (趴着时移动 5 像素才开始拖): 光标滑出剪影也不中途变成点穿, 不然页面收不到移动, 拖不起来
  int button = GetSystemMetrics(SM_SWAPBUTTON) ? VK_RBUTTON : VK_LBUTTON;
  if (!ct_on && (GetAsyncKeyState(button) & 0x8000)) return;
  POINT p;
  RECT r;
  GetCursorPos(&p);
  GetWindowRect(hwnd, &r);
  bool over = false;
  if (PtInRect(&r, p) && IsWindowVisible(hwnd)) {
    std::lock_guard<std::mutex> lock(mu);
    bool fresh = effective_fps() == 0 || GetTickCount64() - mask.received < 3000;
    if (mask.valid() && fresh)
      over = mask.hit((p.x - r.left) / double(r.right - r.left), (p.y - r.top) / double(r.bottom - r.top));
  }
  if (over && !topmost && covered_at(hwnd, p)) over = false;  // 被别的窗口盖着的地方不算 (侧菜单不在别人的窗口后面滑出)
  set_click_through(!over);
  if (over != hovered) {  // 光标进出角色 (和对话栏、菜单): 页面据此滑出/收起侧菜单
    hovered = over;
    web.post_json(std::wstring(L"{\"t\":\"hover\",\"v\":") + (over ? L"true" : L"false") + L"}");
  }
}

void App::send_ui() {
  bool top = (GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0;  // 报窗口真实的样子 (自检据此核对)
  web.post_json(std::wstring(L"{\"t\":\"ui\",\"mic\":") + (mic_on ? L"true" : L"false") + L",\"muted\":" +
                (muted ? L"true" : L"false") + L",\"topmost\":" + (top ? L"true" : L"false") + L",\"follow\":" +
                (follow_mouse ? L"true" : L"false") + L"}");
  send_dock();
}

// ---- 工作台和桌宠合在一起 ----
// 工作台显示着: 桌宠 (连小屋) 收起来、不画 (麦克风照常: 说话她照样听、照样回, 声音从后台的桌宠页面出); 开着的设置窗口关掉 (设置在工作台里).
// 工作台关了或最小化: 桌宠回到桌面, 原来是小屋就还是小屋.
void App::on_workbench(bool shown) {
  bool hide = shown && wb_hides;
  if (shown && panel && panel->is_open()) panel->close();
  if (hide == wb_hidden) return;
  wb_hidden = hide;
  logf(hide ? "工作台开着: 桌宠收起" : "桌宠回到桌面");
  apply_visibility();
  if (!hide) set_leaning(false, false);
}

void App::send_dock() {
  if (!workbench || !workbench->is_open()) return;
  Json s = settings::load(), list = Json::array();
  for (const Json& p : s["personas"].items()) {
    Json x = Json::object();
    x.set("id", p.get_str("id")).set("name", p.get_str("name"));
    list.push(x);
  }
  Json m = Json::object();
  m.set("t", "dock").set("mic", mic_on).set("muted", muted).set("top", workbench->topmost()).set("room", room_mode)
      .set("live2d", face == "live2d").set("personas", list).set("active", s.get_str("active")).set("hides", wb_hides);
  workbench->post(widen(m.dump()));
}

void App::dock_action(const std::string& act, const std::string& arg) {
  if (act == "mic") on_command(ID_MIC);
  else if (act == "mute") on_command(ID_MUTE);
  else if (act == "persona") set_persona(arg);
  else if (act == "pin" && workbench) { workbench->set_topmost(!workbench->topmost()); send_dock(); }
  else if (act == "desktop" || act == "room") {  // 回到桌面 (关掉工作台); 小屋: 回到桌面并进小屋
    if (act == "room" && !room_mode) set_room_mode(true);
    if (workbench) workbench->close();
  } else if (act == "settings") {
    if (workbench) workbench->show_settings(arg);
  }
}

void App::set_persona(const std::string& id) {
  Json s = settings::load();
  std::string name;
  for (const Json& x : s["personas"].items())
    if (x.get_str("id") == id) name = x.get_str("name");
  if (name.empty() || s.get_str("active") == id) return;
  s.set("active", id);
  settings::save(s);
  logf("换成人设预设 %s", name.c_str());
  send_profile();
  if (panel) panel->notify_changed();
  send_dock();
}

void App::panel_message(const std::string& text, WebViewHost* from) {
  if (!panel) panel = new PanelWindow(*this);  // 跟着程序走, 不释放 (窗口不一定开)
  panel->handle(text, from);
}

Json App::pet_prefs() const {
  Json p = Json::object();
  p.set("topmost", topmost).set("follow_mouse", follow_mouse).set("lean", lean_on).set("lean_ok", !lean_unsupported)
      .set("gravity", gravity).set("activity", activity_on).set("room", room_mode).set("fps", fps).set("api", api_on)
      .set("api_port", opt.api_port).set("workbench_hides", wb_hides).set("live2d", face == "live2d");
  return p;
}

void App::set_pet_pref(const std::string& key, const Json& v) {
  bool on = v.boolean();
  if (key == "topmost" && on != topmost) on_command(ID_PIN);
  else if (key == "follow_mouse" && on != follow_mouse) on_command(ID_FOLLOW);
  else if (key == "lean" && on != lean_on) on_command(ID_LEAN);
  else if (key == "gravity" && on != gravity) on_command(ID_GRAVITY);
  else if (key == "activity" && on != activity_on) on_command(ID_ACTIVITY);
  else if (key == "room" && on != room_mode) set_room_mode(on);
  else if (key == "api" && on != api_on) set_api(on);
  else if (key == "fps") {
    int f = int(v.number());
    if (f == 10 || f == 15 || f == 30 || f == 60) { fps = f; send_fps(); save_prefs(); }
  } else if (key == "workbench_hides" && on != wb_hides) {
    wb_hides = on;
    save_prefs();
    on_workbench(workbench && workbench->shown());
    send_dock();
  }
}

// 置顶: 关掉后和普通窗口一样, 点别的窗口就会盖住桌宠; 拖动、打开输入框时提到普通窗口的最上面
void App::set_topmost(bool on) {
  topmost = on;
  SetWindowPos(hwnd, on ? HWND_TOPMOST : HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  logf(on ? "置顶" : "取消置顶 (别的窗口可以盖住桌宠)");
  send_ui();
}

// 眼睛跟着鼠标: 光标相对桌宠窗口的位置 (比例, 可以在窗口外面) 变了才发. 拖动时不发 (光标就在角色身上)
void App::send_cursor() {
  if (!follow_mouse || dragging || effective_fps() == 0) return;
  POINT p;
  RECT r;
  GetCursorPos(&p);
  GetWindowRect(hwnd, &r);
  double x = (p.x - r.left) / double(std::max(1L, r.right - r.left)), y = (p.y - r.top) / double(std::max(1L, r.bottom - r.top));
  if (std::abs(x - cursor_x) < 0.002 && std::abs(y - cursor_y) < 0.002) return;
  cursor_x = x;
  cursor_y = y;
  wchar_t b[96];
  swprintf(b, 96, L"{\"t\":\"cursor\",\"x\":%.4f,\"y\":%.4f}", x, y);
  web.post_json(b);
}

void App::save_prefs() {
  if (opt.selftest) return;
  Json s = settings::load();
  Json& pet = s.at("pet");
  if (!pet.is_object()) pet = Json::object();
  pet.set("topmost", topmost).set("follow_mouse", follow_mouse).set("lean", lean_on).set("activity", activity_on).set("room", room_mode);
  pet.set("gravity", gravity).set("fps", fps).set("workbench_hides", wb_hides);
  RECT r;
  if (hwnd && GetWindowRect(hwnd, &r) && !dragging && !falling) pet.set("pos", Json::array().push(int(r.left)).push(int(r.top)).push(grounded));
  settings::save(s);
}

// 脚底在窗口里的位置 (物理像素): 页面按地面原点投影报的比例最准也最稳; 没报时退回掩码最下面一行 (一格约 7 像素)
int App::foot_px(int window_h) {
  std::lock_guard<std::mutex> lock(mu);
  if (leaning && lean_frac > 0 && lean_frac <= 1) return int(lean_frac * window_h + 0.5);  // 趴着: 栏杆线当脚底
  if (foot_frac > 0 && foot_frac <= 1) return int(foot_frac * window_h + 0.5);
  if (!mask.valid() || mask.foot < 0) return window_h;
  return (mask.foot + 1) * window_h / mask.rows;
}

void App::place_initial() {
  if (!grounded) {  // 上次停在半空: 窗口已经放回原处 (建窗口时), 不站到地面
    settled_at = 0;
    return;
  }
  RECT r;
  GetWindowRect(hwnd, &r);
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
  int y = mi.rcWork.bottom - foot_px(r.bottom - r.top);
  SetWindowPos(hwnd, nullptr, r.left, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
  logf("站到工作区底边: 窗口 (%ld, %d), 脚在窗口内 %d 像素处, 工作区底 %ld", r.left, y, foot_px(r.bottom - r.top),
       mi.rcWork.bottom);
  if (!settled_at) settled_at = GetTickCount64();
}

void App::place_on_ground() {
  if (opt.selftest || dragging || falling || !grounded) return;  // 自检不挪窗口 (截图按原来的位置截); 停在半空时不动
  if (stand_on) {
    stand_tick();
    return;
  }
  RECT r;
  GetWindowRect(hwnd, &r);
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
  int y = mi.rcWork.bottom - foot_px(r.bottom - r.top);
  if (y != r.top) SetWindowPos(hwnd, nullptr, r.left, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
}

// 放着不动够久了就趴下 (50 ms 一次, 跟 stand_tick 同一个定时器)
void App::lean_tick() {
  int after = opt.lean_after_s >= 0 ? opt.lean_after_s : (opt.selftest ? 0 : 15);
  if (leaning || !lean_on || lean_unsupported || after <= 0 || !settled_at || !grounded || dragging || falling || user_hidden || auto_hidden) return;
  {
    std::lock_guard<std::mutex> lock(mu);
    if (!page_ready) return;
  }
  if (GetTickCount64() - settled_at >= ULONGLONG(after) * 1000) set_leaning(true);
}

void App::set_leaning(bool on, bool replace) {
  if (on == leaning) return;
  {
    std::lock_guard<std::mutex> lock(mu);
    leaning = on;
    lean_frac = -1;
  }
  logf(on ? "放着不动够久了, 趴下" : "站起来");
  web.post_json(std::wstring(L"{\"t\":\"lean\",\"v\":") + (on ? L"true" : L"false") + L"}");
  if (!on && replace) place_on_ground();  // 不是拖起来、掉下去的 (托盘里关掉): 按站着的脚底站回去
}

void App::start_drag() {
  int button = GetSystemMetrics(SM_SWAPBUTTON) ? VK_RBUTTON : VK_LBUTTON;
  if (dragging || !(GetAsyncKeyState(button) & 0x8000)) return;
  POINT p;
  RECT r;
  GetCursorPos(&p);
  GetWindowRect(hwnd, &r);
  drag_off = {p.x - r.left, p.y - r.top};
  dragging = true;
  settled_at = 0;
  set_leaning(false, false);  // 拖起来: 站起来, 落地之后重新计时
  if (!topmost) SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  falling = false;
  stand_on = nullptr;
  set_click_through(false);
  SetTimer(hwnd, T_DRAG, 10, nullptr);
}

// 松手 (或脚下的窗口没了) 后往下掉, 落到最近的地面: 露在外面的窗口顶边, 都没有就是工作区底边 (任务栏顶边). 见 ledge.cpp.
void App::begin_fall() {
  settled_at = 0;
  set_leaning(false, false);  // 脚下的窗口没了: 站起来再往下掉
  RECT r;
  GetWindowRect(hwnd, &r);
  int w = r.right - r.left, h = r.bottom - r.top, foot = foot_px(h);
  POINT probe{(r.left + r.right) / 2, r.top + foot};
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromPoint(probe, MONITOR_DEFAULTTONEAREST), &mi);
  fall_x = std::clamp<int>(r.left, mi.rcWork.left - w / 3, mi.rcWork.right - 2 * w / 3);
  Ledge ledge = find_ledge(hwnd, fall_x + w / 2, r.top + foot, foot, mi.rcMonitor, mi.rcWork);
  fall_onto = ledge.hwnd;
  fall_target = ledge.top - foot;
  fall_y = std::min<double>(r.top, fall_target);  // 拖到地面下面: 直接站回去
  fall_v = 0;
  fall_last = GetTickCount64();
  falling = true;
  stand_on = nullptr;
}

// 站在窗口顶边上: 那个窗口移动就跟着走; 关掉、最小化、最大化、或者缩到脚下没有了, 就往下掉
void App::stand_tick() {
  lean_tick();
  if (!stand_on || dragging || falling) return;
  RECT r;
  GetWindowRect(hwnd, &r);
  int w = r.right - r.left, foot = foot_px(r.bottom - r.top);
  bool ok = can_stand_on(stand_on, hwnd);
  RECT f = ok ? visible_frame(stand_on) : RECT{};
  int x = f.left + stand_dx, center = x + w / 2;
  if (!ok || center < f.left || center > f.right) {
    logf("脚下的窗口没了, 往下掉");
    stand_on = nullptr;
    begin_fall();
    SetTimer(hwnd, T_DRAG, 10, nullptr);
    return;
  }
  if (x != r.left || f.top - foot != r.top)
    SetWindowPos(hwnd, nullptr, x, f.top - foot, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
}

// 松手就停在那里 (默认): 脚离下面最近的地面不到 28 像素 (按 96 DPI), 或者拖到了地面下面 (任务栏里), 才站上去.
// 窗口左右至少留三分之一在屏幕里, 脚不低于工作区底边, 头不出屏幕顶 (窗口顶上的空白可以出去一点)
void App::settle_free() {
  RECT r;
  GetWindowRect(hwnd, &r);
  int w = r.right - r.left, h = r.bottom - r.top, foot = foot_px(h);
  POINT probe{(r.left + r.right) / 2, r.top + foot};
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromPoint(probe, MONITOR_DEFAULTTONEAREST), &mi);
  int x = std::clamp<int>(r.left, mi.rcWork.left - w / 3, mi.rcWork.right - 2 * w / 3);
  Ledge ledge = find_ledge(hwnd, x + w / 2, r.top + foot, foot, mi.rcMonitor, mi.rcWork);
  int gap = ledge.top - (r.top + foot);
  if (gap <= MulDiv(28, dpi_of(hwnd), 96)) {
    begin_fall();
    SetTimer(hwnd, T_DRAG, 10, nullptr);
    return;
  }
  int y = std::clamp<int>(r.top, mi.rcWork.top - foot / 4, mi.rcWork.bottom - foot);
  if (x != r.left || y != r.top) SetWindowPos(hwnd, nullptr, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
  grounded = false;
  stand_on = nullptr;
  falling = false;
  settled_at = 0;
  logf("松手停在 (%d, %d), 离下面的地面 %d 像素", x, y, gap);
  save_prefs();
}

void App::drop_from(int left, int top) {
  dragging = false;
  SetWindowPos(hwnd, nullptr, left, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
  begin_fall();
  SetTimer(hwnd, T_DRAG, 10, nullptr);
}

void App::drag_tick() {
  if (dragging) {
    int button = GetSystemMetrics(SM_SWAPBUTTON) ? VK_RBUTTON : VK_LBUTTON;
    if (GetAsyncKeyState(button) & 0x8000) {
      POINT p;
      GetCursorPos(&p);
      SetWindowPos(hwnd, nullptr, p.x - drag_off.x, p.y - drag_off.y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
      return;
    }
    dragging = false;
    if (gravity) begin_fall();
    else settle_free();
  }
  if (falling) {
    ULONGLONG now = GetTickCount64();
    double dt = std::min(0.05, (now - fall_last) / 1000.0);
    fall_last = now;
    fall_v += 5000.0 * dpi_of(hwnd) / 96.0 * dt;  // 重力, 物理像素/秒^2
    fall_y += fall_v * dt;
    if (fall_y >= fall_target) {
      fall_y = fall_target;
      falling = false;
      grounded = true;
      settled_at = GetTickCount64();  // 落地了: 开始计时, 放着不动够久就趴下
    }
    SetWindowPos(hwnd, nullptr, fall_x, int(fall_y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    if (!falling && fall_onto && IsWindow(fall_onto)) {  // 落到了窗口上: 记下相对位置, 之后跟着它走
      stand_on = fall_onto;
      stand_dx = fall_x - visible_frame(fall_onto).left;
      wchar_t title[64] = L"";
      GetWindowTextW(fall_onto, title, 64);
      logf("站到窗口顶边上: %s", utf8(title).c_str());
    }
    if (!falling) save_prefs();
  }
  if (!dragging && !falling) KillTimer(hwnd, T_DRAG);
}

void App::tray_add() {
  nid = {};
  nid.cbSize = sizeof nid;
  nid.hWnd = hwnd;
  nid.uID = 1;
  nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
  nid.uCallbackMessage = WM_APP_TRAY;
  nid.hIcon = app_icon(GetSystemMetrics(SM_CXSMICON));
  lstrcpynW(nid.szTip, L"WorldModel 桌宠 (M0)", ARRAYSIZE(nid.szTip));
  Shell_NotifyIconW(NIM_ADD, &nid);
}

void App::tray_menu() {
  HMENU m = CreatePopupMenu();
  AppendMenuW(m, MF_STRING | (user_hidden ? MF_GRAYED : 0), ID_INPUT, L"说话… (也可以双击角色)");
  AppendMenuW(m, MF_STRING, ID_WORKBENCH, L"工作台… (她替你干活: 读写文件、搜、跑命令)");
  AppendMenuW(m, MF_STRING, ID_SETTINGS, L"设置… (人设预设、模型、MCP 服务器、skill)");
  AppendMenuW(m, MF_STRING | (room_mode ? MF_CHECKED : 0) | (face == "live2d" ? MF_GRAYED : 0), ID_ROOM,
              face == "live2d" ? L"小屋模式 (2D 门面没有小屋)" : L"小屋模式「抹茶樱花」小筑居");
  AppendMenuW(m, MF_STRING | (activity_on ? MF_CHECKED : 0), ID_ACTIVITY, L"看你在做什么 (只在本机, 不截图; 聊天软件只看是哪个)");
  AppendMenuW(m, MF_STRING, ID_TOGGLE, user_hidden ? L"显示" : L"隐藏");
  AppendMenuW(m, MF_STRING | (mic_on ? MF_CHECKED : 0), ID_MIC, L"麦克风 (说话就能对话)");
  AppendMenuW(m, MF_STRING | (muted ? MF_CHECKED : 0), ID_MUTE, L"静音");
  AppendMenuW(m, MF_STRING | (paused ? MF_CHECKED : 0), ID_PAUSE, L"暂停渲染");
  AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(m, MF_STRING | (fps == 10 ? MF_CHECKED : 0), ID_FPS10, L"空闲 10 帧 (省电)");
  AppendMenuW(m, MF_STRING | (fps == 15 ? MF_CHECKED : 0), ID_FPS15, L"空闲 15 帧");
  AppendMenuW(m, MF_STRING | (fps == 30 ? MF_CHECKED : 0), ID_FPS30, L"空闲 30 帧");
  AppendMenuW(m, MF_STRING | (fps == 60 ? MF_CHECKED : 0), ID_FPS60, L"空闲 60 帧");
  AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(m, MF_STRING | (topmost ? MF_CHECKED : 0), ID_PIN, L"置顶 (关掉后会被别的窗口盖住)");
  AppendMenuW(m, MF_STRING | (follow_mouse ? MF_CHECKED : 0), ID_FOLLOW, L"眼睛跟着鼠标");
  AppendMenuW(m, MF_STRING | (lean_on ? MF_CHECKED : 0) | (lean_unsupported ? MF_GRAYED : 0), ID_LEAN,
              lean_unsupported ? L"放着不动时趴在任务栏上 (这个角色做不了)" : L"站在任务栏、窗口顶边上放着不动 15 秒后趴下");
  AppendMenuW(m, MF_STRING | (gravity ? MF_CHECKED : 0), ID_GRAVITY, L"松手落到地面 (关着时停在松手的地方)");
  AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
  std::wstring api = L"本机接口 (OpenAI 兼容 API 和 MCP, 端口 " + std::to_wstring(opt.api_port) + L")";
  AppendMenuW(m, MF_STRING | (api_on ? MF_CHECKED : 0), ID_API, api.c_str());
  AppendMenuW(m, MF_STRING | (tools && tools->has_control() ? 0 : MF_GRAYED), ID_UNPLUG, L"断开电脑操控 (停下来, 并在设置里停用)");
  {
    const Json qq = settings::load()["qq"];
    AppendMenuW(m, MF_STRING | (qq["paused"].boolean() ? MF_CHECKED : 0) | (qq["enabled"].boolean() ? 0 : MF_GRAYED), ID_QQ_PAUSE,
                L"暂停 QQ (她在 QQ 上不收不发)");
  }
  {
    // 本地服务 (services.h): 看守写的状态, 每样一行, 点一下请看守重启那一样
    svc_items = services::read();
    HMENU sm = CreatePopupMenu();
    int up = 0;
    for (size_t i = 0; i < svc_items.items.size() && i < 20; i++) {
      const auto& it = svc_items.items[i];
      up += it.state == "up" || it.state == "external";
      AppendMenuW(sm, MF_STRING | (it.state == "external" || !svc_items.running ? MF_GRAYED : 0), ID_SVC_ITEM + int(i),
                  services::mark(it).c_str());
    }
    if (!svc_items.running) {
      if (!svc_items.items.empty()) AppendMenuW(sm, MF_SEPARATOR, 0, nullptr);
      AppendMenuW(sm, MF_STRING | (services::found_python() ? 0 : MF_GRAYED), ID_SVC_UP,
                  services::found_python() ? L"启动本地服务 (服务端、本机模型、语音; 已经开着的不重复开)" : L"找不到 Python (用 --python 指定 pythonw.exe)");
    } else {
      AppendMenuW(sm, MF_SEPARATOR, 0, nullptr);
      AppendMenuW(sm, MF_STRING, ID_SVC_ALL, L"全部重启");
      AppendMenuW(sm, MF_STRING, ID_SVC_STOP, L"全部停掉");
    }
    AppendMenuW(sm, MF_STRING, ID_SVC_LOGS, L"打开服务日志");
    std::wstring title = svc_items.running ? L"本地服务 (" + std::to_wstring(up) + L"/" + std::to_wstring(svc_items.items.size()) + L" 在跑)"
                                           : L"本地服务 (没人看着)";
    AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(m, MF_POPUP, reinterpret_cast<UINT_PTR>(sm), title.c_str());
  }
  AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(m, MF_STRING, ID_RELOAD, L"重新加载页面");
  if (opt.devtools) AppendMenuW(m, MF_STRING, ID_DEVTOOLS, L"开发者工具");
  AppendMenuW(m, MF_SEPARATOR, 0, nullptr);
  AppendMenuW(m, MF_STRING, ID_EXIT, svc_items.running ? L"退出 (本地服务接着跑, QQ、微信照常收)" : L"退出");
  if (svc_items.running) AppendMenuW(m, MF_STRING, ID_EXIT_STOP, L"退出并停掉本地服务");
  POINT p;
  GetCursorPos(&p);
  SetForegroundWindow(hwnd);  // 不先设前台的话, 点菜单外面菜单不会消失
  TrackPopupMenu(m, TPM_RIGHTBUTTON, p.x, p.y, 0, hwnd, nullptr);
  PostMessageW(hwnd, WM_NULL, 0, 0);
  DestroyMenu(m);
}

void App::on_command(int id) {
  switch (id) {
    case ID_TOGGLE: user_hidden = !user_hidden; apply_visibility(); break;
    case ID_INPUT: web.post_json(L"{\"t\":\"input\"}"); break;
    case ID_SETTINGS: open_panel(); break;
    case ID_WORKBENCH: open_workbench(); break;
    case ID_UNPLUG:
      if (!tools) break;
      tools->disconnect();
      send_profile();  // 顺带刷新工具表 (停用的服务器断开, 进程跟着结束)
      if (panel) panel->notify_changed();
      break;
    case ID_QQ_PAUSE: set_qq_paused(!settings::load()["qq"]["paused"].boolean()); break;
    case ID_ROOM: set_room_mode(!room_mode); break;
    case ID_ACTIVITY: set_activity(!activity_on); save_prefs(); break;
    case ID_MIC:
      mic_on = !mic_on;
      if (wb_hidden) apply_visibility();
      send_mic();
      send_ui();
      update_tray_tip();
      break;
    case ID_MUTE:
      muted = !muted;
      web.post_json(std::wstring(L"{\"t\":\"mute\",\"v\":") + (muted ? L"true" : L"false") + L"}");
      send_ui();
      break;
    case ID_PAUSE: paused = !paused; send_fps(); break;
    case ID_FPS10: fps = 10; send_fps(); break;
    case ID_FPS15: fps = 15; send_fps(); break;
    case ID_FPS30: fps = 30; send_fps(); break;
    case ID_FPS60: fps = 60; send_fps(); break;
    case ID_PIN: set_topmost(!topmost); save_prefs(); break;
    case ID_LEAN:
      lean_on = !lean_on;
      if (!lean_on) set_leaning(false);
      else if (settled_at) settled_at = GetTickCount64();
      save_prefs();
      break;
    case ID_FOLLOW:
      follow_mouse = !follow_mouse;
      cursor_x = cursor_y = -1e9;  // 再打开时马上发一次
      send_ui();
      save_prefs();
      break;
    case ID_API: set_api(!api_on); break;
    case ID_RELOAD: web.reload(); break;
    case ID_DEVTOOLS: web.open_devtools(); break;
    case ID_EXIT: DestroyWindow(hwnd); break;
    case ID_EXIT_STOP: services::command("stop"); DestroyWindow(hwnd); break;
    case ID_SVC_UP: services::start(); break;
    case ID_SVC_ALL: services::command("restart"); break;
    case ID_SVC_STOP: services::command("stop"); break;
    default:
      if (id >= ID_SVC_ITEM && id < ID_SVC_ITEM + int(svc_items.items.size())) services::command("restart", svc_items.items[id - ID_SVC_ITEM].name);
      break;
    case ID_SVC_LOGS:
      CreateDirectoryW(services::logs_dir().c_str(), nullptr);
      ShellExecuteW(nullptr, L"open", services::logs_dir().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
      break;
    case ID_GRAVITY:
      gravity = !gravity;
      if (gravity && !grounded && !dragging && !falling) {  // 打开时停在半空的话马上落下去
        begin_fall();
        SetTimer(hwnd, T_DRAG, 10, nullptr);
      }
      save_prefs();
      break;
  }
}

// 本机接口: 只听 127.0.0.1, 要 token (见 api.h). 端口被占着时打不开, 托盘里的勾不打上
void App::set_api(bool on) {
  if (on == api_on) return;
  if (!api) api = new PetApi(*this);  // 不释放: 关掉之后连接线程可能还在用
  if (on) {
    api_on = api->start(opt.api_port);
    if (!api_on) {
      lstrcpynW(nid.szInfoTitle, L"本机接口没打开", ARRAYSIZE(nid.szInfoTitle));
      lstrcpynW(nid.szInfo, (L"端口 " + std::to_wstring(opt.api_port) + L" 可能被别的程序占着, 详见 wmpet.log").c_str(), ARRAYSIZE(nid.szInfo));
      nid.uFlags = NIF_INFO;
      if (nid.hWnd) Shell_NotifyIconW(NIM_MODIFY, &nid);
    }
  } else {
    api->stop();
    api_on = false;
    logf("本机接口已关");
  }
}

void App::open_panel() {
  if (workbench && workbench->shown()) {  // 工作台开着: 设置就在工作台里 (工作台 + 桌宠一份设置)
    workbench->show_settings("");
    return;
  }
  if (!panel) panel = new PanelWindow(*this);  // 跟着程序走, 不释放
  panel->open();
}

void App::open_workbench() {
  if (!workbench) workbench = new WorkbenchWindow(*this);  // 跟着程序走, 不释放
  if (!workbench->open()) {  // 窗口没建出来: 这次的对象别留着, 不然 !workbench 永远为假, 自动打开就再也不重试了
    delete workbench;
    workbench = nullptr;
  }
}

// 看你在做什么: 开着时 3 秒看一次前台窗口 (activity.h). 打开时页面先说一句她会看什么、不看什么
void App::set_activity(bool on) {
  activity_on = on;
  if (on) {
    if (!activity) activity = new ActivityWatch(*this);  // 跟着程序走, 不释放
    activity->reset();
    SetTimer(hwnd, T_ACT, 3000, nullptr);
    web.post_json(L"{\"t\":\"activity-mode\",\"on\":true}");
    activity->tick();
  } else {
    KillTimer(hwnd, T_ACT);
    web.post_json(L"{\"t\":\"activity\",\"on\":false}");
  }
  logf(on ? "看你在做什么: 打开" : "看你在做什么: 关了");
}

// 小屋模式开关: 页面带 room=1 重新打开 (就绪后照常发人设); 脚底位置换了 (小屋的脚底是卡片底边), 重新落地
void App::set_room_mode(bool on) {
  if (on == room_mode || (on && face == "live2d")) return;
  room_mode = on;
  save_prefs();
  set_leaning(false, false);
  logf(on ? "小屋模式: 打开" : "小屋模式: 关了, 回到桌面");
  {
    std::lock_guard<std::mutex> lock(mu);
    page_ready = false;
    mask = HitMask();
    foot_frac = -1;
  }
  web.navigate_query(page_query());
}

// 云端模型的 key 也在里面 (明文): 只在宿主和本进程的页面之间传, 页面再经本机的 /ws 交给服务端; 日志里不记
std::wstring App::page_query() const {
  std::wstring q = opt.query;
  auto add = [&q](const std::wstring& p) { q += std::wstring(q.empty() ? L"" : L"&") + p; };
  if (face == "live2d") {
    add(L"face=live2d");
    return q;
  }
  // 3D 门面选了某个模型 (面板里的"3D · 名字", 存成 vrm:文件名): 页面带 avatar=文件名. 只认 models/vrm 里那种文件名
  // (字母数字和 _ - .), 自检自己带了 avatar= 时不加
  if (face.rfind("vrm:", 0) == 0 && opt.query.find(L"avatar=") == std::wstring::npos) {
    std::string file = face.substr(4);
    bool ok = file.size() > 4 && file.size() < 80 && file.compare(file.size() - 4, 4, ".vrm") == 0 &&
              std::all_of(file.begin(), file.end(), [](char c) { return isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-' || c == '.'; });
    if (ok) add(L"avatar=" + std::wstring(file.begin(), file.end()));
  }
  if (room_mode) add(L"room=1");
  return q;
}

void App::send_profile() {
  Json s = settings::load();
  Json p = settings::active_profile(s);
  // 当前预设换了门面 (3D <-> 2D): 页面带新参数重新打开, 就绪后再来一次 send_profile
  if (p.get_str("face") != face) {
    face = p.get_str("face");
    logf("门面换成 %s, 重新打开页面", face.c_str());
    {
      std::lock_guard<std::mutex> lock(mu);
      page_ready = false;
      mask = HitMask();
    }
    web.navigate_query(page_query());
    return;
  }
  // 侧菜单的人设列表: 只要名字和用哪类模型 (不带人设文字和 key)
  Json list = Json::array();
  for (const Json& x : s["personas"].items()) {
    Json one = Json::object();
    one.set("id", x.get_str("id")).set("name", x.get_str("name")).set("provider", x.get_str("provider", "local"));
    one.set("face", x.get_str("face") == "live2d" ? "live2d" : "vrm");
    list.push(one);
  }
  Json msg = Json::object();
  msg.set("t", "profile").set("profile", p).set("personas", list).set("active", s.get_str("active"));
  web.post_json(widen(msg.dump()));
  if (!tools) {
    tools = new ToolHub(*this);  // 跟着程序走, 不释放 (连着的 MCP 服务器进程在作业对象里, 程序退出时一起结束)
    tools->start();
  }
  tools->refresh();
  send_channel();
}

// QQ 渠道的设置 (含解密的 token 和 key, 不记日志): 页面连上服务端时和改了时发 channel_config. 没配过 qq 段就不发
void App::send_channel() {
  Json s = settings::load();
  Json cfg = settings::channel_config(s);
  if (cfg.is_null()) return;
  Json msg = Json::object();
  msg.set("t", "qq").set("config", cfg).set("paused", s["qq"]["paused"].boolean());
  web.post_json(widen(msg.dump()));
}

void App::set_qq_paused(bool on) {
  Json s = settings::load();
  if (!s["qq"].is_object()) return;
  s.at("qq").set("paused", on);
  settings::save(s);
  Json msg = Json::object();
  msg.set("t", "qq-pause").set("v", on);
  web.post_json(widen(msg.dump()));
  if (panel) panel->notify_changed();
  logf("QQ %s", on ? "暂停了" : "恢复了");
}

// 平时点桌宠不抢焦点 (WM_MOUSEACTIVATE 返回不激活); 打开输入框时才把窗口设成前台.
// 别的程序在前台时 SetForegroundWindow 可能被系统拒绝, 先把输入队列挂到前台线程上再设.
void App::take_focus() {
  if (!topmost) SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  HWND fg = GetForegroundWindow();
  DWORD fg_thread = fg ? GetWindowThreadProcessId(fg, nullptr) : 0, me = GetCurrentThreadId();
  bool attach = fg_thread && fg_thread != me && AttachThreadInput(fg_thread, me, TRUE);
  BOOL ok = SetForegroundWindow(hwnd);
  if (attach) AttachThreadInput(fg_thread, me, FALSE);
  web.move_focus();
  logf("输入框要焦点: %s", ok ? "已设为前台" : "系统没允许设为前台");
}

// 全屏程序、D3D 全屏游戏、演示模式时自动隐身 (页面停到 0 帧)
void App::check_fullscreen() {
  if (opt.no_autohide) return;
  QUERY_USER_NOTIFICATION_STATE s;
  if (FAILED(SHQueryUserNotificationState(&s))) return;
  bool busy = s == QUNS_BUSY || s == QUNS_RUNNING_D3D_FULL_SCREEN || s == QUNS_PRESENTATION_MODE;
  if (busy && (controlling || GetTickCount64() < control_grace)) busy = false;  // 操作电脑时的发光边框不算 (见 set_controlling)
  if (busy == auto_hidden) return;
  auto_hidden = busy;
  logf(busy ? "检测到全屏程序, 隐藏 (状态 %d)" : "全屏结束, 恢复显示 (状态 %d)", int(s));
  apply_visibility();
}

void App::on_page(const std::string& m) {
  if (m.rfind("mask ", 0) == 0) {
    HitMask hm;
    if (!HitMask::parse(m, hm)) return;
    bool first;
    {
      std::lock_guard<std::mutex> lock(mu);
      first = !mask.valid();
      mask = std::move(hm);
    }
    if (first && !opt.selftest) place_initial();
  } else if (m == "drag") {
    start_drag();
  } else if (m == "focus") {
    take_focus();
  } else if (m == "mic-toggle") {  // 侧菜单的按钮, 和托盘同一个开关
    on_command(ID_MIC);
  } else if (m == "mute-toggle") {
    on_command(ID_MUTE);
  } else if (m == "pin-toggle") {  // 侧菜单的图钉
    on_command(ID_PIN);
  } else if (m.rfind("lean ", 0) == 0) {  // 趴着时栏杆线在窗口里的位置 (姿势过渡时每帧报), 或者 "lean none": 这个角色趴不了
    if (m == "lean none") {
      lean_unsupported = true;
      logf("这个角色趴不了 (没有行动层)");
      set_leaning(false);
    } else if (leaning) {
      {
        std::lock_guard<std::mutex> lock(mu);
        lean_frac = atof(m.c_str() + 5);
      }
      place_on_ground();
    }
  } else if (m == "panel") {  // 侧菜单的齿轮
    open_panel();
  } else if (m == "workbench") {  // 侧菜单的工作台按钮
    open_workbench();
  } else if (m == "room") {  // 侧菜单的小房子: 进小屋 / 回到桌面
    set_room_mode(!room_mode);
  } else if (m.rfind("persona ", 0) == 0) {  // 侧菜单里换人设预设: 存下来, 告诉页面 (再告诉服务端), 开着的面板跟着刷新
    set_persona(m.substr(8));
  } else if (m == "tools-refresh") {  // 她给自己装了 MCP (服务端说的): 重新连一遍工具
    if (tools) tools->refresh();
  } else if (m == "hide") {
    if (!user_hidden) on_command(ID_TOGGLE);
  } else if (m == "peek 1" || m == "peek 0") {  // 她陪看说的那句: 全屏自动隐藏时临时露出对话栏, 说完收回 (静音时只有字, 没静音时字 + 声音)
    bool on = m == "peek 1";
    if (on != peeking) {
      peeking = on;
      if (auto_hidden && !user_hidden && !wb_hidden) logf(on ? "全屏时她陪看说话: 露出对话栏" : "陪看那句说完: 收回");
      apply_visibility();
    }
  } else if (m.rfind("mic ", 0) == 0) {
    logf("麦克风: %s", m.c_str() + 4);
    if (m.rfind("mic error", 0) == 0 && mic_on) {  // 没权限、没设备、服务端没开识别: 托盘里的勾去掉
      mic_on = false;
      update_tray_tip();
      send_ui();
    }
  } else if (m.rfind("foot ", 0) == 0) {
    bool first;
    {
      std::lock_guard<std::mutex> lock(mu);
      first = foot_frac < 0;
      foot_frac = atof(m.c_str() + 5);
    }
    if (first && !opt.selftest && !stand_on && !dragging && !falling) place_initial();  // 精确的脚底位置到了, 重新站一次
  } else if (m.rfind("talk ", 0) == 0) {
    std::lock_guard<std::mutex> lock(mu);
    talk_result = m.substr(5);
  } else if (m.rfind("ready", 0) == 0) {
    {
      std::lock_guard<std::mutex> lock(mu);
      page_ready = true;
      if (m.size() > 6) page_info = m.substr(6);
    }
    logf("页面就绪 %s", m.c_str() + std::min<size_t>(m.size(), 6));
    cursor_x = cursor_y = -1e9;  // 页面加载好之前发的光标位置收不到: 下一拍重发
    lean_unsupported = false;    // 换了门面 (页面重新打开): 新角色重新算, 趴着的话先站起来重新计时
    if (leaning) {
      std::lock_guard<std::mutex> lock(mu);
      leaning = false;
      lean_frac = -1;
    }
    if (settled_at || opt.selftest) settled_at = GetTickCount64();  // 自检不站到地面上: 就绪时开始计时
    if (opt.memory_low) web.set_memory_low(true);
    send_fps();
    send_ui();
    send_profile();
    if (opt.panel_at_start && !panel) open_panel();  // 等桌宠的 WebView2 环境好了再开, 面板和它共用
    if (opt.workbench_at_start && !workbench) open_workbench();
    if (activity_on) {  // 记着是开的: 重新开始看 (页面重新加载过, 之前告诉它的都没了)
      if (!activity) activity = new ActivityWatch(*this);
      activity->reset();
      SetTimer(hwnd, T_ACT, 3000, nullptr);
    }
  } else if (m.rfind("stats ", 0) == 0) {
    std::lock_guard<std::mutex> lock(mu);
    last_stats = m.substr(6);
  } else if (m.rfind("tool ", 0) == 0) {  // 服务端要调工具 (页面转来的 tool_call, 见 tools.h)
    Json call;
    if (tools && Json::parse(m.substr(5), call) && call.is_object()) tools->on_call(call);
  } else if (m.rfind("tool-answer ", 0) == 0) {  // 用户在对话栏里的回答: "tool-answer <call_id> <once|always|deny|timeout>"
    size_t sp = m.find(' ', 12);
    if (tools && sp != std::string::npos) tools->on_answer(m.substr(12, sp - 12), m.substr(sp + 1));
  } else if (m.rfind("tool-cancel ", 0) == 0) {
    if (tools) tools->on_cancel(m.substr(12));
  } else if (m.rfind("builtin-done ", 0) == 0) {  // 页面做完了自带工具: "builtin-done <call_id> <1|0> <给模型的一句话>"
    size_t a = m.find(' ', 13), b = a == std::string::npos ? a : m.find(' ', a + 1);
    if (tools && b != std::string::npos) tools->on_builtin_done(m.substr(13, a - 13), m.substr(a + 1, b - a - 1) == "1", m.substr(b + 1));
  } else if (m.rfind("tool-stop ", 0) == 0) {  // 对话栏里的"停"
    if (tools) tools->on_stop(m.substr(10));
  } else if (m == "control-stop") {  // "在操作你的电脑"提示条上的"停"
    if (tools) tools->emergency_stop("你点了提示条上的停");
  } else if (m == "turn-done") {  // 这一轮说完了 (或被打断了): "这一轮都允许"作废
    if (tools) tools->on_turn_done();
  } else if (m.rfind("task-begin ", 0) == 0) {  // 渠道任务单 (QQ 上主人说的事): "task-begin <id> <owner|self>", 紧接着页面开这一轮
    size_t a = m.find(' ', 11);
    if (tools) tools->task_begin(m.substr(11, a == std::string::npos ? a : a - 11), a == std::string::npos ? "self" : m.substr(a + 1));
  } else if (m.rfind("task-end ", 0) == 0) {  // 这件事的一轮说完了 (或被打断了): 授权作废, 暂存的最后一张截图经 task-shot 回页面
    if (tools) tools->task_end(m.substr(9));
  } else if (m.rfind("apiev ", 0) == 0) {  // 本机接口的请求做到哪了 (见 api.h)
    if (api) api->on_page_event(m.substr(6));
  } else if (m == "qq-shot") {  // 自检 (--ask-shot): 第一次弹 QQ 草稿卡片时截一张页面
    if (!opt.ask_shot.empty()) web.capture_png(opt.ask_shot, [](bool ok) { logf("QQ 草稿卡片截图%s", ok ? "好了" : "失败"); });
  } else if (m.rfind("qq-status ", 0) == 0) {  // QQ 渠道的状态 (服务端的 channel_status, 不带 token): 面板的 QQ 页取
    std::string prev = qq_status;
    qq_status = m.substr(10);
    Json st;
    if (Json::parse(qq_status, st) && (prev.empty() || prev.find("\"state\":\"" + st.get_str("state") + "\"") == std::string::npos))
      logf("QQ 渠道: %s%s", st.get_str("state").c_str(), st.get_str("error").empty() ? "" : (" (" + st.get_str("error") + ")").c_str());
  } else if (m.rfind("log ", 0) == 0) {
    logf("[页面] %s", m.c_str() + 4);
  }
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, PWSTR, int) {
  App& a = g_app;
  a.inst = inst;
  parse_args(a.opt);
  if (a.opt.mcp_stdio) return run_mcp_stdio(a.opt.api_port);
  if (!a.opt.settings_dir.empty()) settings::set_dir(a.opt.settings_dir);
  settings::set_bundled_skills_dir(full_path(a.opt.web_dir + L"\\..\\skills"));  // 随附 skill: desktop/skills
  if (!a.opt.mcp_probe.empty()) return run_mcp_probe(a.opt.mcp_probe, a.opt.mcp_call, a.opt.mcp_call_args);
  if (!a.opt.activity_test.empty()) return run_activity_test(a.opt.activity_test);
  enable_per_monitor_dpi();
  CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
  log_open(exe_dir() + L"\\..\\wmpet.log");
  logf("启动: 服务 %s, 页面目录 %s, 宿主 %s, 点穿 %s%s", utf8(a.opt.server).c_str(), utf8(a.opt.web_dir).c_str(),
       utf8(a.opt.host).c_str(), utf8(a.opt.ct).c_str(), a.opt.selftest ? ", 自检" : "");

  WNDCLASSEXW wc{sizeof wc};
  wc.lpfnWndProc = pet_proc;
  wc.hInstance = inst;
  wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
  wc.hIcon = app_icon(GetSystemMetrics(SM_CXICON));
  wc.hIconSm = app_icon(GetSystemMetrics(SM_CXSMICON));
  wc.lpszClassName = L"WmPetWindow";
  RegisterClassExW(&wc);

  bool comp = a.opt.host == L"comp";
  Json prefs = settings::load()["pet"];
  a.topmost = a.opt.selftest || prefs["topmost"].boolean(true);  // 自检总是置顶 (截图时不能被盖住)
  a.follow_mouse = prefs["follow_mouse"].boolean(true);
  a.lean_on = prefs["lean"].boolean(true);
  a.gravity = prefs["gravity"].boolean(false);
  a.wb_hides = prefs["workbench_hides"].boolean(true);
  if (int f = int(prefs["fps"].number()); !a.opt.selftest && (f == 10 || f == 15 || f == 30 || f == 60)) a.fps = f;
  a.activity_on = !a.opt.selftest && prefs["activity"].boolean(false);  // 页面就绪后才开始看 (见 on_page 的 ready)
  a.room_mode = !a.opt.selftest && prefs["room"].boolean(false);  // 自检要小屋时页面参数带 room=1
  DWORD ex = (a.topmost ? WS_EX_TOPMOST : 0) | WS_EX_TOOLWINDOW;  // 置顶 (可以关), 不进任务栏和 Alt+Tab
  if (comp) ex |= WS_EX_NOREDIRECTIONBITMAP;
  if (a.opt.ct == L"layered") ex |= WS_EX_LAYERED;

  UINT dpi = 96;
  using DpiFn = UINT(WINAPI*)();
  if (auto f = reinterpret_cast<DpiFn>(GetProcAddress(GetModuleHandleW(L"user32.dll"), "GetDpiForSystem"))) dpi = f();
  int w = MulDiv(a.opt.width, dpi, 96), h = MulDiv(a.opt.height, dpi, 96);
  MONITORINFO mi{sizeof mi};
  GetMonitorInfoW(MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY), &mi);
  int x, y;
  if (a.opt.selftest) {  // 自检: 放在主显示器中间, 下面铺自己的棋盘格
    x = (mi.rcWork.left + mi.rcWork.right - w) / 2;
    y = (mi.rcWork.top + mi.rcWork.bottom - h) / 2;
  } else {
    x = mi.rcWork.right - w - MulDiv(24, dpi, 96);
    y = mi.rcWork.bottom - h;
    // 上次放在哪 (中心还在某个显示器上才用): 站在地面上的只取左右, 就绪后照常站到地面; 停在半空的原样放回
    const Json& pos = prefs["pos"];
    if (pos.is_array() && pos.items().size() >= 3) {
      int px = int(pos.items()[0].number()), py = int(pos.items()[1].number());
      if (MonitorFromPoint(POINT{px + w / 2, py + h / 2}, MONITOR_DEFAULTTONULL)) {
        x = px;
        a.grounded = pos.items()[2].boolean(true);
        if (!a.grounded) y = py;
      }
    }
  }
  a.hwnd = CreateWindowExW(ex, wc.lpszClassName, L"WorldModel 桌宠", WS_POPUP, x, y, w, h, nullptr, nullptr, inst, nullptr);
  if (!a.hwnd) {
    logf("建窗口失败 %lu", GetLastError());
    return 2;
  }
  if (a.opt.ct == L"layered") SetLayeredWindowAttributes(a.hwnd, 0, 255, LWA_ALPHA);
  if (!comp) {
    // 让 DWM 按像素透明度合成这个窗口 (GDI 画的黑色 alpha 为 0)
    DWM_BLURBEHIND bb{};
    bb.dwFlags = DWM_BB_ENABLE | DWM_BB_BLURREGION;
    bb.fEnable = TRUE;
    bb.hRgnBlur = CreateRectRgn(0, 0, -1, -1);
    DwmEnableBlurBehindWindow(a.hwnd, &bb);
    DeleteObject(bb.hRgnBlur);
  }
  ShowWindow(a.hwnd, SW_SHOWNOACTIVATE);
  if (!a.opt.selftest) {
    a.taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");
    a.tray_add();
    SetTimer(a.hwnd, T_FULL, 2000, nullptr);
    services::configure(a.opt.web_dir + L"\\..\\..", a.opt.python);
    if (a.opt.services) services::start();  // 服务端、本机模型、语音没开就由看守开起来 (开着的不重复开)
  }
  SetTimer(a.hwnd, T_HIT, 33, nullptr);
  SetTimer(a.hwnd, T_STAND, 50, nullptr);

  a.web.on_message = [&a](const std::string& m) { a.on_page(m); };
  a.web.on_navigated = [&a](bool ok, int) {
    if (!ok) {
      std::lock_guard<std::mutex> lock(a.mu);
      a.nav_failed = true;
      return;
    }
    // Explicit window launches need the WebView2 environment, not the pet's
    // first rendered frame. Hidden/fullscreen room views may never emit ready.
    // The ready handler remains a fallback; existing instances prevent repeats.
    if (a.opt.panel_at_start && !a.panel) a.open_panel();
    if (a.opt.workbench_at_start && !a.workbench) a.open_workbench();
  };
  WebViewHost::Config cfg;
  cfg.hwnd = a.hwnd;
  cfg.composition = comp;
  cfg.devtools = a.opt.devtools;
  cfg.server = a.opt.server;
  cfg.web_dir = a.opt.web_dir;
  a.face = settings::active_profile(settings::load()).get_str("face");
  cfg.page_query = a.page_query();
  if (!a.opt.fake_mic.empty())  // 自检: Chromium 的假麦克风播放这段 WAV (放一遍, 不循环)
    cfg.extra_args = L"--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=" + a.opt.fake_mic + L"%noloop";
  a.web.allow_permission = [&a](COREWEBVIEW2_PERMISSION_KIND kind, const std::wstring& uri) {
    return kind == COREWEBVIEW2_PERMISSION_KIND_MICROPHONE && uri.rfind(a.opt.server + L"/", 0) == 0 &&
           (a.mic_on || !a.opt.fake_mic.empty());
  };
  cfg.user_data = full_path(exe_dir() + L"\\..\\webview2-data");
  HRESULT hr = a.web.start(cfg);
  if (FAILED(hr)) logf("WebView2 启动失败 0x%08lx", hr);
  if (a.opt.api) a.set_api(true);
  if (a.opt.selftest) a.selftest = std::thread(run_selftest, std::ref(a));

  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
  if (a.selftest.joinable()) a.selftest.join();
  logf("消息循环结束");
  if (a.api_on) a.api->stop();  // 还在等的请求一律结束
  logf("本机接口已停");
  CoUninitialize();
  logf("退出");
  return a.opt.selftest ? (a.selftest_ok ? 0 : 1) : 0;
}
