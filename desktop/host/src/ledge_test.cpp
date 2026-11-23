// 自检: 站在窗口顶边. 只用自检自己建的窗口 (一个带标题栏的"平台"和一块挡住它左半段顶边的遮挡物), 不碰用户的窗口:
//   1. 在被挡住的那段上方松手 -> 不能站到平台上;
//   2. 在露出来的那段上方松手 -> 站到平台顶边上, 脚底和顶边差不超过 1 像素;
//   3. 平台挪开 -> 桌宠跟着走;
//   4. 平台最小化 -> 桌宠往下掉, 不再站在它上面.
// 再测"松手就停在那里" (默认, 托盘里没开"松手落到地面"时): 平台恢复以后
//   5. 脚在平台露出来那段上方 150 像素松手 -> 停在原处, 不站到任何地方;
//   6. 脚在它上方 15 像素松手 -> 吸到平台顶边上;
//   7. 脚拖到工作区底边下面 20 像素 (任务栏里) 松手 -> 站回工作区底边.
#include <cstdio>

#include "app.h"
#include "ledge.h"

namespace {

LRESULT CALLBACK test_proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  if (m == WM_PAINT) {
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(h, &ps);
    FillRect(dc, &ps.rcPaint, reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1));
    EndPaint(h, &ps);
    return 0;
  }
  if (m == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
  return DefWindowProcW(h, m, wp, lp);
}

HWND make_window(HINSTANCE inst, const wchar_t* title, DWORD style, DWORD ex, const RECT& r) {
  static bool registered = false;
  if (!registered) {
    WNDCLASSEXW wc{sizeof wc};
    wc.lpfnWndProc = test_proc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.lpszClassName = L"WmPetLedgeTest";
    RegisterClassExW(&wc);
    registered = true;
  }
  HWND h = CreateWindowExW(ex | WS_EX_TOPMOST | WS_EX_NOACTIVATE, L"WmPetLedgeTest", title, style, r.left, r.top,
                           r.right - r.left, r.bottom - r.top, nullptr, nullptr, inst, nullptr);
  ShowWindow(h, SW_SHOWNOACTIVATE);
  return h;
}

struct Snap {
  bool falling = false;
  HWND stand = nullptr;
  RECT pet{};
  int foot = 0;
};

Snap snap(App& app) {
  Snap s;
  app.ui([&] {
    s.falling = app.falling || app.dragging;
    s.stand = app.stand_on;
    GetWindowRect(app.hwnd, &s.pet);
    s.foot = app.foot_px(s.pet.bottom - s.pet.top);
  });
  return s;
}

Snap settle(App& app, int ms = 3000) {
  Snap s;
  for (int t = 0; t < ms; t += 50) {
    Sleep(50);
    s = snap(app);
    if (!s.falling && t >= 300) break;
  }
  return s;
}

const char* where(const Snap& s, HWND platform) {
  return s.stand == platform ? "platform" : s.stand ? "other-window" : "taskbar";
}

}  // namespace

std::string run_ledge_test(App& app) {
  HWND platform = nullptr, cover = nullptr;
  RECT pf{}, work{};
  int w = 0, s96 = 96;
  Snap start = snap(app);
  w = start.pet.right - start.pet.left;
  app.ui([&] {
    s96 = GetDpiForWindow(app.hwnd);
    MONITORINFO mi{sizeof mi};
    GetMonitorInfoW(MonitorFromWindow(app.hwnd, MONITOR_DEFAULTTOPRIMARY), &mi);
    work = mi.rcWork;
    int pw = MulDiv(700, s96, 96), ph = MulDiv(260, s96, 96);
    int top = std::max<int>(work.top + start.foot + MulDiv(60, s96, 96), work.bottom - ph - MulDiv(40, s96, 96));
    RECT r{(work.left + work.right - pw) / 2, top, (work.left + work.right + pw) / 2, top + ph};
    platform = make_window(app.inst, L"桌宠平台自检", WS_OVERLAPPEDWINDOW, 0, r);
  });
  Sleep(400);
  app.ui([&] {
    pf = visible_frame(platform);
    RECT c{pf.left - 10, pf.top - MulDiv(40, s96, 96), pf.left + (pf.right - pf.left) * 45 / 100, pf.top + MulDiv(60, s96, 96)};
    cover = make_window(app.inst, L"", WS_POPUP, WS_EX_TOOLWINDOW, c);
    SetWindowPos(cover, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  });
  Sleep(400);
  int pw = pf.right - pf.left, lift = MulDiv(150, s96, 96);
  auto drop_over = [&](int foot_x) {
    app.ui([&] { app.drop_from(foot_x - w / 2, pf.top - lift - start.foot); });
    return settle(app);
  };

  // 1. 被挡住的那段
  Snap a = drop_over(pf.left + pw / 5);
  bool a_ok = a.stand != platform;
  // 2. 露出来的那段
  Snap b = drop_over(pf.left + pw * 7 / 10);
  int b_err = b.pet.top + b.foot - pf.top;
  bool b_ok = b.stand == platform && std::abs(b_err) <= 1;
  // 3. 平台挪开 (往右下一点, 往上一点), 桌宠跟着走
  int mx = MulDiv(80, s96, 96), my = -MulDiv(40, s96, 96);
  RECT pf2{};
  app.ui([&] {
    RECT wr;
    GetWindowRect(platform, &wr);
    SetWindowPos(platform, nullptr, wr.left + mx, wr.top + my, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
  });
  Sleep(400);
  Snap c = snap(app);
  app.ui([&] { pf2 = visible_frame(platform); });
  int c_err = c.pet.top + c.foot - pf2.top, c_dx = (c.pet.left - b.pet.left) - mx;
  bool c_ok = c.stand == platform && std::abs(c_err) <= 1 && std::abs(c_dx) <= 1;
  // 4. 平台最小化, 桌宠掉下去
  app.ui([&] { ShowWindow(platform, SW_MINIMIZE); });
  Snap d = settle(app);
  bool d_ok = d.stand != platform && !d.falling;
  int d_err = d.stand ? 0 : d.pet.top + d.foot - int(work.bottom);
  d_ok = d_ok && (d.stand || std::abs(d_err) <= 1);

  // 5-7. 松手就停在那里
  app.ui([&] { ShowWindow(platform, SW_SHOWNOACTIVATE); });
  Sleep(500);
  RECT pf3{};
  app.ui([&] { pf3 = visible_frame(platform); });
  int ex = pf3.left + (pf3.right - pf3.left) * 7 / 10;
  auto free_drop = [&](int left, int top) {
    app.ui([&] { SetWindowPos(app.hwnd, nullptr, left, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE); app.settle_free(); });
    Snap s = settle(app);
    bool grounded = true;
    app.ui([&] { grounded = app.grounded; });
    return std::make_pair(s, grounded);
  };
  int high_top = pf3.top - lift - start.foot;
  auto [e, e_ground] = free_drop(ex - w / 2, high_top);
  bool e_ok = !e_ground && !e.stand && !e.falling && std::abs(e.pet.top - high_top) <= 1;
  auto [f, f_ground] = free_drop(ex - w / 2, pf3.top - MulDiv(15, s96, 96) - start.foot);
  int f_err = f.pet.top + f.foot - pf3.top;
  bool f_ok = f_ground && f.stand == platform && std::abs(f_err) <= 1;
  auto [g, g_ground] = free_drop(work.left + MulDiv(60, s96, 96), int(work.bottom) + MulDiv(20, s96, 96) - start.foot);
  int g_err = g.stand ? 999 : g.pet.top + g.foot - int(work.bottom);
  bool g_ok = g_ground && std::abs(g_err) <= 1;

  app.ui([&] {
    DestroyWindow(cover);
    DestroyWindow(platform);
  });
  bool from_page;
  {
    std::lock_guard<std::mutex> lock(app.mu);
    from_page = app.foot_frac > 0;
  }
  char buf[1536];
  snprintf(buf, sizeof buf,
           "{\"foot_px\":%d,\"foot_from\":\"%s\",\"covered\":{\"landed_on\":\"%s\",\"ok\":%s},"
           "\"land\":{\"err_px\":%d,\"ok\":%s},\"follow\":{\"err_px\":%d,\"dx_err\":%d,\"ok\":%s},"
           "\"minimized\":{\"landed_on\":\"%s\",\"err_px\":%d,\"ok\":%s},"
           "\"free_high\":{\"grounded\":%s,\"top_err\":%ld,\"ok\":%s},\"free_near\":{\"landed_on\":\"%s\",\"err_px\":%d,\"ok\":%s},"
           "\"free_below\":{\"err_px\":%d,\"ok\":%s},\"ok\":%s}",
           start.foot, from_page ? "page" : "mask", where(a, platform), a_ok ? "true" : "false", b_err,
           b_ok ? "true" : "false", c_err, c_dx, c_ok ? "true" : "false", where(d, platform), d_err,
           d_ok ? "true" : "false", e_ground ? "true" : "false", long(e.pet.top - high_top), e_ok ? "true" : "false",
           where(f, platform), f_err, f_ok ? "true" : "false", g_err, g_ok ? "true" : "false",
           (a_ok && b_ok && c_ok && d_ok && e_ok && f_ok && g_ok) ? "true" : "false");
  return buf;
}
