// 桌宠能站的"地面": 任务栏顶边 (所在显示器工作区的底边), 或者普通窗口的顶边.
// 只用公开的窗口接口 (EnumWindows、DwmGetWindowAttribute), 不注入、不读别的进程内存 (MateEngine 为避开游戏反作弊关掉过坐窗口).
#pragma once
#include <windows.h>

struct Ledge {
  HWND hwnd = nullptr;  // 站在哪个窗口上; 空表示工作区底边
  int top = 0;          // 地面的屏幕 y (物理像素)
};

// 窗口看得见的边框 (Win10/11 的 GetWindowRect 含不可见的缩放边, 约 7 像素, 要去掉)
RECT visible_frame(HWND h);

// 这个窗口能不能当地面: 可见、没最小化/最大化、没被系统隐藏 (cloaked)、带标题栏、不是工具窗口、够宽
bool can_stand_on(HWND h, HWND self);

// 脚在 (foot_x, foot_y) 往下掉, 找最近的地面. foot_px: 脚在桌宠窗口里的位置 (站上去后头不能出屏幕顶).
// 地面的顶边在脚的位置要露在外面 (没被上面的窗口挡住), 否则跳过它.
Ledge find_ledge(HWND self, int foot_x, int foot_y, int foot_px, const RECT& monitor, const RECT& work);
