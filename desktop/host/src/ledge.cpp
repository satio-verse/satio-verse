#include "ledge.h"

#include <dwmapi.h>

#include <vector>

namespace {

constexpr int kMinWidth = 160;  // 太窄的窗口站不住
constexpr int kEdgeMargin = 12; // 脚离窗口左右边至少这么远

bool cloaked(HWND h) {
  DWORD c = 0;
  return SUCCEEDED(DwmGetWindowAttribute(h, DWMWA_CLOAKED, &c, sizeof c)) && c;
}

bool own_helper(HWND h) {
  wchar_t cls[64] = L"";
  GetClassNameW(h, cls, 64);
  return lstrcmpW(cls, L"WmPetBackdrop") == 0;  // 自检铺的棋盘格
}

struct Search {
  HWND self;
  int foot_x, foot_y, foot_px;
  RECT monitor, work;
  Ledge best;
  std::vector<RECT> above;  // 已经扫过的 (z 序更靠前的) 可见窗口, 用来判断顶边有没有被挡住
};

BOOL CALLBACK visit(HWND h, LPARAM lp) {
  auto& s = *reinterpret_cast<Search*>(lp);
  if (h == s.self || own_helper(h) || !IsWindowVisible(h) || IsIconic(h) || cloaked(h)) return TRUE;
  RECT f = visible_frame(h);
  if (f.right <= f.left || f.bottom <= f.top) return TRUE;
  if (can_stand_on(h, s.self) && f.top >= s.foot_y - 2 && f.top < s.best.top && f.top < s.work.bottom &&
      s.foot_x >= f.left + kEdgeMargin && s.foot_x <= f.right - kEdgeMargin && f.top - s.foot_px >= s.monitor.top) {
    POINT p{s.foot_x, f.top + 2};
    bool covered = false;
    for (const RECT& r : s.above) covered = covered || PtInRect(&r, p);
    if (!covered) s.best = {h, int(f.top)};
  }
  // 鼠标穿透的浮层 (显卡的游戏浮层之类, 常常是全屏的) 看不见也点不到, 不算挡住
  if (!(GetWindowLongPtrW(h, GWL_EXSTYLE) & WS_EX_TRANSPARENT)) s.above.push_back(f);
  return TRUE;
}

}  // namespace

RECT visible_frame(HWND h) {
  RECT r{};
  if (FAILED(DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, &r, sizeof r))) GetWindowRect(h, &r);
  return r;
}

bool can_stand_on(HWND h, HWND self) {
  if (h == self || !IsWindow(h) || !IsWindowVisible(h) || IsIconic(h) || IsZoomed(h) || cloaked(h)) return false;
  if (GetAncestor(h, GA_ROOT) != h) return false;
  LONG_PTR style = GetWindowLongPtrW(h, GWL_STYLE), ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
  if ((style & WS_CAPTION) != WS_CAPTION || (ex & WS_EX_TOOLWINDOW)) return false;
  RECT f = visible_frame(h);
  return f.right - f.left >= kMinWidth;
}

Ledge find_ledge(HWND self, int foot_x, int foot_y, int foot_px, const RECT& monitor, const RECT& work) {
  Search s{self, foot_x, foot_y, foot_px, monitor, work, Ledge{nullptr, int(work.bottom)}, {}};
  EnumWindows(visit, reinterpret_cast<LPARAM>(&s));  // 按 z 序从上往下
  return s.best;
}
