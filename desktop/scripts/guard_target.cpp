// 守门自检用的测试窗口 (computertest.ps1 编译、启动): 一个普通窗口, 上面有"发送""好的"两个按钮、一个普通输入框、一个密码框.
// 把各个控件中心的屏幕坐标 (物理像素) 和输入框的窗口句柄写成 JSON, 测试脚本据此让假云端去点.
// 复制一份改名成 Weixin.exe 再起, 就是"聊天软件"的窗口 (守门按程序名认).
//   guard_target.exe <标题> <输出 JSON> [左 上]
#define _WIN32_WINNT 0x0A00  // SetProcessDpiAwarenessContext
#include <windows.h>

#include <cstdio>
#include <string>

namespace {

HWND g_send, g_ok, g_edit, g_pwd;

std::string utf8(const std::wstring& w) {
  int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), int(w.size()), nullptr, 0, nullptr, nullptr);
  std::string s(n, '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.c_str(), int(w.size()), s.data(), n, nullptr, nullptr);
  return s;
}

std::string center(HWND h) {
  RECT r;
  GetWindowRect(h, &r);
  char b[64];
  snprintf(b, sizeof b, "[%ld, %ld]", (r.left + r.right) / 2, (r.top + r.bottom) / 2);
  return b;
}

LRESULT CALLBACK proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  if (m == WM_DESTROY) {
    PostQuitMessage(0);
    return 0;
  }
  return DefWindowProcW(h, m, wp, lp);
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
  std::wstring title = argc > 1 ? argv[1] : L"守门测试窗口";
  std::wstring out = argc > 2 ? argv[2] : L"";
  int x = argc > 4 ? _wtoi(argv[3]) : 80, y = argc > 4 ? _wtoi(argv[4]) : 80;
  HINSTANCE inst = GetModuleHandleW(nullptr);
  WNDCLASSW wc{};
  wc.lpfnWndProc = proc;
  wc.hInstance = inst;
  wc.hCursor = LoadCursorW(nullptr, reinterpret_cast<LPCWSTR>(IDC_ARROW));
  wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
  wc.lpszClassName = L"WmGuardTarget";
  RegisterClassW(&wc);
  HWND w = CreateWindowExW(WS_EX_TOPMOST, wc.lpszClassName, title.c_str(), WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_VISIBLE, x, y, 420, 260,
                           nullptr, nullptr, inst, nullptr);
  HFONT font = reinterpret_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));
  auto make = [&](const wchar_t* cls, const wchar_t* text, DWORD style, int cx, int cy, int cw, int ch) {
    HWND c = CreateWindowExW(cls[0] == L'E' ? WS_EX_CLIENTEDGE : 0, cls, text, WS_CHILD | WS_VISIBLE | style, cx, cy, cw, ch, w, nullptr, inst, nullptr);
    SendMessageW(c, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
    return c;
  };
  g_edit = make(L"EDIT", L"", ES_AUTOHSCROLL, 20, 20, 360, 28);
  g_pwd = make(L"EDIT", L"", ES_PASSWORD | ES_AUTOHSCROLL, 20, 60, 360, 28);
  g_ok = make(L"BUTTON", L"好的", BS_PUSHBUTTON, 20, 110, 160, 40);
  g_send = make(L"BUTTON", L"发送", BS_PUSHBUTTON, 220, 110, 160, 40);
  ShowWindow(w, SW_SHOWNOACTIVATE);
  UpdateWindow(w);
  if (!out.empty()) {
    RECT r;
    GetWindowRect(w, &r);
    char head[160];
    snprintf(head, sizeof head, "{\"pid\": %lu, \"hwnd\": %lld, \"edit_hwnd\": %lld, \"rect\": [%ld, %ld, %ld, %ld], ", GetCurrentProcessId(),
             static_cast<long long>(reinterpret_cast<intptr_t>(w)), static_cast<long long>(reinterpret_cast<intptr_t>(g_edit)), r.left, r.top,
             r.right, r.bottom);
    std::string json = head;
    json += "\"title\": \"" + utf8(title) + "\", \"window\": " + center(w) + ", \"edit\": " + center(g_edit) + ", \"password\": " + center(g_pwd) +
            ", \"ok\": " + center(g_ok) + ", \"send\": " + center(g_send) + "}";
    std::wstring tmp = out + L".tmp";
    if (FILE* f = _wfopen(tmp.c_str(), L"wb")) {
      fwrite(json.data(), 1, json.size(), f);
      fclose(f);
      MoveFileExW(tmp.c_str(), out.c_str(), MOVEFILE_REPLACE_EXISTING);
    }
  }
  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
  return 0;
}
