// M0 自检: 在桌宠下面铺一张自己画的棋盘格 (不截用户屏幕上的内容), 截屏判断透明区域是不是透出了棋盘格、
// 有没有黑底; 用 WindowFromPoint 判断点穿; 再按几种帧率测宿主 + WebView2 进程组的 CPU、内存、GPU、显存.
#include <pdh.h>
#include <pdhmsg.h>
#include <psapi.h>
#include <tlhelp32.h>

#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <sstream>

#include "app.h"

namespace {

std::string fmt(const char* f, ...) {
  char b[8192];
  va_list ap;
  va_start(ap, f);
  vsnprintf(b, sizeof b, f, ap);
  va_end(ap);
  return b;
}

constexpr int kSquare = 16;
constexpr COLORREF kA = RGB(40, 160, 80), kB = RGB(230, 210, 60);

COLORREF checker(int x, int y) { return ((x / kSquare + y / kSquare) & 1) ? kB : kA; }

LRESULT CALLBACK backdrop_proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
  if (m == WM_PAINT) {
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(h, &ps);
    RECT rc;
    GetClientRect(h, &rc);
    HBRUSH a = CreateSolidBrush(kA), b = CreateSolidBrush(kB);
    for (int y = 0; y < rc.bottom; y += kSquare)
      for (int x = 0; x < rc.right; x += kSquare) {
        RECT cell{x, y, x + kSquare, y + kSquare};
        FillRect(dc, &cell, checker(x, y) == kA ? a : b);
      }
    DeleteObject(a);
    DeleteObject(b);
    EndPaint(h, &ps);
    return 0;
  }
  if (m == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
  return DefWindowProcW(h, m, wp, lp);
}

HWND create_backdrop(HINSTANCE inst, const RECT& r) {
  WNDCLASSEXW wc{sizeof wc};
  wc.lpfnWndProc = backdrop_proc;
  wc.hInstance = inst;
  wc.lpszClassName = L"WmPetBackdrop";
  RegisterClassExW(&wc);
  HWND h = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, wc.lpszClassName, L"", WS_POPUP, r.left,
                           r.top, r.right - r.left, r.bottom - r.top, nullptr, nullptr, inst, nullptr);
  ShowWindow(h, SW_SHOWNOACTIVATE);
  return h;
}

std::vector<uint32_t> capture(const RECT& r) {
  int w = r.right - r.left, h = r.bottom - r.top;
  std::vector<uint32_t> px(size_t(w) * h);
  HDC screen = GetDC(nullptr);
  HDC mem = CreateCompatibleDC(screen);
  BITMAPINFO bi{};
  bi.bmiHeader = {sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB};
  void* bits = nullptr;
  HBITMAP bmp = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
  HGDIOBJ old = SelectObject(mem, bmp);
  BitBlt(mem, 0, 0, w, h, screen, r.left, r.top, SRCCOPY | CAPTUREBLT);
  GdiFlush();
  memcpy(px.data(), bits, px.size() * 4);
  SelectObject(mem, old);
  DeleteObject(bmp);
  DeleteDC(mem);
  ReleaseDC(nullptr, screen);
  return px;
}

void save_bmp(const std::wstring& path, int w, int h, const std::vector<uint32_t>& px) {
  FILE* f = _wfopen(path.c_str(), L"wb");
  if (!f) return;
  BITMAPFILEHEADER fh{};
  BITMAPINFOHEADER ih{sizeof ih, w, -h, 1, 32, BI_RGB};
  fh.bfType = 0x4d42;
  fh.bfOffBits = sizeof fh + sizeof ih;
  fh.bfSize = fh.bfOffBits + DWORD(px.size() * 4);
  fwrite(&fh, sizeof fh, 1, f);
  fwrite(&ih, sizeof ih, 1, f);
  fwrite(px.data(), 4, px.size(), f);
  fclose(f);
}

struct Look {
  double bg_outside = 0, black_outside = 0, content_inside = 0, black_inside = 0;
};

// 掩码外 (角色外扩一格以外) 应该全是棋盘格; 掩码内应该大部分是角色
Look classify(const std::vector<uint32_t>& px, int w, int h, const HitMask& mask) {
  long long out_n = 0, out_bg = 0, out_black = 0, in_n = 0, in_content = 0, in_black = 0;
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      uint32_t p = px[size_t(y) * w + x];
      int b = p & 0xff, g = (p >> 8) & 0xff, r = (p >> 16) & 0xff;
      COLORREF e = checker(x, y);
      bool bg = std::abs(r - GetRValue(e)) + std::abs(g - GetGValue(e)) + std::abs(b - GetBValue(e)) <= 30;
      bool black = r + g + b <= 30;
      if (mask.cell(x * mask.cols / w, y * mask.rows / h)) {
        in_n++;
        in_content += !bg;
        in_black += black;
      } else {
        out_n++;
        out_bg += bg;
        out_black += black;
      }
    }
  Look l;
  if (out_n) l.bg_outside = double(out_bg) / out_n, l.black_outside = double(out_black) / out_n;
  if (in_n) l.content_inside = double(in_content) / in_n, l.black_inside = double(in_black) / in_n;
  return l;
}

bool all_cells(const HitMask& m, int c, int r, int radius, bool want) {
  for (int dy = -radius; dy <= radius; dy++)
    for (int dx = -radius; dx <= radius; dx++) {
      int cc = c + dx, rr = r + dy;
      bool inside = cc >= 0 && rr >= 0 && cc < m.cols && rr < m.rows;
      if (!inside) {
        if (want) return false;
        continue;
      }
      if (m.cell(cc, rr) != want) return false;
    }
  return true;
}

// 角色身体中间的一格 (四周都是角色), 和同一行里角色旁边的一格 (四周两格都没有角色)
bool pick_points(const HitMask& m, const RECT& wr, POINT& opaque, POINT& clear) {
  int top = -1, bottom = -1;
  for (int r = 0; r < m.rows; r++)
    for (int c = 0; c < m.cols; c++)
      if (m.cell(c, r)) {
        if (top < 0) top = r;
        bottom = r;
      }
  if (top < 0) return false;
  int w = wr.right - wr.left, h = wr.bottom - wr.top;
  auto center = [&](int c, int r) {
    return POINT{wr.left + int((c + 0.5) * w / m.cols), wr.top + int((r + 0.5) * h / m.rows)};
  };
  int start = top + (bottom - top) * 2 / 5;  // 大约胸口到腰
  for (int k = 0; k < m.rows; k++) {
    int r = start + ((k & 1) ? -(k + 1) / 2 : k / 2);
    if (r < 0 || r >= m.rows) continue;
    for (int c = 0; c < m.cols; c++) {
      if (!all_cells(m, c, r, 1, true)) continue;
      opaque = center(c, r);
      for (int cc = c; cc >= 0; cc--)
        if (all_cells(m, cc, r, 2, false)) {
          clear = center(cc, r);
          return true;
        }
      for (int cc = c; cc < m.cols; cc++)
        if (all_cells(m, cc, r, 2, false)) {
          clear = center(cc, r);
          return true;
        }
    }
  }
  return false;
}

std::string who_is_at(POINT p, HWND pet, HWND backdrop) {
  HWND root = GetAncestor(WindowFromPoint(p), GA_ROOT);
  if (root == pet) return "pet";
  if (root == backdrop) return "backdrop";
  char cls[64] = "";
  if (root) GetClassNameA(root, cls, sizeof cls);
  return std::string("other:") + cls;
}

std::set<DWORD> process_tree(DWORD root) {
  std::multimap<DWORD, DWORD> children;
  HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  PROCESSENTRY32W e{sizeof e};
  if (Process32FirstW(snap, &e)) do {
      children.emplace(e.th32ParentProcessID, e.th32ProcessID);
    } while (Process32NextW(snap, &e));
  CloseHandle(snap);
  std::set<DWORD> out{root};
  std::vector<DWORD> todo{root};
  while (!todo.empty()) {
    DWORD p = todo.back();
    todo.pop_back();
    auto range = children.equal_range(p);
    for (auto it = range.first; it != range.second; ++it)
      if (out.insert(it->second).second) todo.push_back(it->second);
  }
  return out;
}

ULONGLONG cpu_100ns(DWORD pid) {
  HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
  if (!h) return 0;
  FILETIME c, x, k, u;
  ULONGLONG t = 0;
  if (GetProcessTimes(h, &c, &x, &k, &u))
    t = ((ULONGLONG(k.dwHighDateTime) << 32) | k.dwLowDateTime) + ((ULONGLONG(u.dwHighDateTime) << 32) | u.dwLowDateTime);
  CloseHandle(h);
  return t;
}

void read_array(PDH_HCOUNTER c, const std::function<void(const wchar_t*, double)>& f) {
  DWORD bytes = 0, count = 0;
  if (PdhGetFormattedCounterArrayW(c, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, &bytes, &count, nullptr) != PDH_STATUS(PDH_MORE_DATA))
    return;
  std::vector<BYTE> buf(bytes);
  auto* items = reinterpret_cast<PDH_FMT_COUNTERVALUE_ITEM_W*>(buf.data());
  if (PdhGetFormattedCounterArrayW(c, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, &bytes, &count, items) != ERROR_SUCCESS) return;
  for (DWORD i = 0; i < count; i++)
    if (items[i].FmtValue.CStatus == PDH_CSTATUS_VALID_DATA || items[i].FmtValue.CStatus == PDH_CSTATUS_NEW_DATA)
      f(items[i].szName, items[i].FmtValue.doubleValue);
}

// 实例名形如 pid_1234_luid_0x00000000_0x0000D1B3_phys_0_eng_0_engtype_3D
DWORD pid_of(const std::wstring& name) {
  unsigned long pid = 0;
  return swscanf(name.c_str(), L"pid_%lu", &pid) == 1 ? DWORD(pid) : 0;
}
std::wstring part(const std::wstring& name, const wchar_t* key, const wchar_t* until) {
  size_t a = name.find(key);
  if (a == std::wstring::npos) return L"";
  a += wcslen(key);
  size_t b = until ? name.find(until, a) : std::wstring::npos;
  return name.substr(a, b == std::wstring::npos ? std::wstring::npos : b - a);
}

std::map<DWORD, std::wstring> process_names() {
  std::map<DWORD, std::wstring> out;
  HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  PROCESSENTRY32W e{sizeof e};
  if (Process32FirstW(snap, &e)) do {
      out[e.th32ProcessID] = e.szExeFile;
    } while (Process32NextW(snap, &e));
  CloseHandle(snap);
  return out;
}

struct Usage {
  double cpu_pct = 0, ws_mb = 0, priv_mb = 0, gpu_3d = 0, gpu_max = 0, vram_mb = 0, shared_mb = 0;
  std::string gpu_max_eng, pdh;
  std::string detail = "[]";    // 每个进程: pid、名字、CPU、工作集、私有、GPU 专用/共享
  std::string adapters = "{}";  // 每块显卡 (luid) 的专用显存占用, 用来认出哪块是独显
  int procs = 0;
};

// CPU 计时窗口放在两次 PDH 采样之间: 枚举 GPU 引擎计数器 (几百个实例) 本身要花不少 CPU, 不能算进宿主的占用.
Usage measure(int seconds) {
  Usage u;
  DWORD self = GetCurrentProcessId();
  PDH_HQUERY q = nullptr;
  PDH_HCOUNTER eng = nullptr, ded = nullptr, shr = nullptr, adapter = nullptr;
  PDH_STATUS s = PdhOpenQueryW(nullptr, 0, &q);
  if (s == ERROR_SUCCESS) s = PdhAddEnglishCounterW(q, L"\\GPU Engine(*)\\Utilization Percentage", 0, &eng);
  if (s == ERROR_SUCCESS) s = PdhAddEnglishCounterW(q, L"\\GPU Process Memory(*)\\Dedicated Usage", 0, &ded);
  if (s == ERROR_SUCCESS) s = PdhAddEnglishCounterW(q, L"\\GPU Process Memory(*)\\Shared Usage", 0, &shr);
  if (s == ERROR_SUCCESS) s = PdhAddEnglishCounterW(q, L"\\GPU Adapter Memory(*)\\Dedicated Usage", 0, &adapter);
  if (s != ERROR_SUCCESS) {
    char b[32];
    snprintf(b, sizeof b, "0x%08lx", s);
    u.pdh = b;
  }
  if (q) PdhCollectQueryData(q);
  std::set<DWORD> pids = process_tree(self);
  std::map<DWORD, ULONGLONG> t0;
  for (DWORD p : pids) t0[p] = cpu_100ns(p);
  LARGE_INTEGER f, a, b;
  QueryPerformanceFrequency(&f);
  QueryPerformanceCounter(&a);
  Sleep(DWORD(seconds) * 1000);
  QueryPerformanceCounter(&b);
  std::set<DWORD> now = process_tree(self);
  std::map<DWORD, ULONGLONG> t1;
  for (DWORD p : now) t1[p] = cpu_100ns(p);
  if (q) PdhCollectQueryData(q);
  double wall = double(b.QuadPart - a.QuadPart) / f.QuadPart;

  std::map<DWORD, std::wstring> names = process_names();
  DWORD cores = GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
  struct Proc { double cpu = 0, ws = 0, priv = 0, ded = 0, shr = 0; };
  std::map<DWORD, Proc> per;
  ULONGLONG used = 0;
  for (DWORD p : now) {
    auto it = t0.find(p);
    ULONGLONG d = t1[p] - (it != t0.end() ? std::min(it->second, t1[p]) : 0);
    used += d;
    Proc& pr = per[p];
    pr.cpu = d / 1e7 / wall / (cores ? cores : 1) * 100.0;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, p);
    if (!h) continue;
    PROCESS_MEMORY_COUNTERS_EX pm{};
    pm.cb = sizeof pm;
    if (GetProcessMemoryInfo(h, reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pm), sizeof pm)) {
      pr.ws = pm.WorkingSetSize / 1048576.0;
      pr.priv = pm.PrivateUsage / 1048576.0;
      u.ws_mb += pr.ws;
      u.priv_mb += pr.priv;
    }
    CloseHandle(h);
  }
  u.procs = int(now.size());
  u.cpu_pct = used / 1e7 / wall / (cores ? cores : 1) * 100.0;

  if (q && u.pdh.empty()) {
    std::map<std::wstring, double> by_engine;  // luid + 引擎类型 -> 本进程组合计
    read_array(eng, [&](const wchar_t* name, double v) {
      std::wstring n = name;
      if (!now.count(pid_of(n))) return;
      by_engine[part(n, L"luid_", L"_phys") + L"|" + part(n, L"engtype_", nullptr)] += v;
    });
    for (auto& [k, v] : by_engine) {
      if (k.size() >= 3 && k.compare(k.size() - 3, 3, L"|3D") == 0) u.gpu_3d = std::max(u.gpu_3d, v);
      if (v > u.gpu_max) u.gpu_max = v, u.gpu_max_eng = utf8(k);
    }
    read_array(ded, [&](const wchar_t* name, double v) {
      DWORD pid = pid_of(name);
      if (!now.count(pid)) return;
      u.vram_mb += v / 1048576.0;
      per[pid].ded += v / 1048576.0;
    });
    read_array(shr, [&](const wchar_t* name, double v) {
      DWORD pid = pid_of(name);
      if (!now.count(pid)) return;
      u.shared_mb += v / 1048576.0;
      per[pid].shr += v / 1048576.0;
    });
    std::string a = "{";
    read_array(adapter, [&](const wchar_t* name, double v) {
      a += fmt("%s\"%s\":%.0f", a.size() > 1 ? "," : "", json_escape(utf8(part(name, L"luid_", L"_phys"))).c_str(),
               v / 1048576.0);
    });
    u.adapters = a + "}";
  }
  std::string d = "[";
  for (auto& [pid, pr] : per)
    d += fmt("%s{\"pid\":%lu,\"name\":\"%s\",\"cpu_pct\":%.2f,\"ws_mb\":%.1f,\"private_mb\":%.1f,\"gpu_ded_mb\":%.1f,"
             "\"gpu_shared_mb\":%.1f}",
             d.size() > 1 ? "," : "", pid, json_escape(utf8(names[pid])).c_str(), pr.cpu, pr.ws, pr.priv, pr.ded, pr.shr);
  u.detail = d + "]";
  if (q) PdhCloseQuery(q);
  return u;
}

bool wait_for(const std::function<bool()>& pred, int ms) {
  for (int t = 0; t < ms; t += 100) {
    if (pred()) return true;
    Sleep(100);
  }
  return pred();
}

double now_ms() {
  LARGE_INTEGER f, c;
  QueryPerformanceFrequency(&f);
  QueryPerformanceCounter(&c);
  return c.QuadPart * 1000.0 / f.QuadPart;
}

// 测资源阶段的连拍 (--measure-film 秒): 从切到这一阶段那一刻起, 每 every_ms 截一张桌宠 (下面是自检自己的棋盘格), 存成 folder\NNN.bmp.
// 每张记 [毫秒, 和上一张比变了的像素 %, 不是棋盘格的 (角色) %, 黑的 %]: 变了 0 = 页面这段没画新的一帧; 突然变很多 = 跳了一下;
// 隐藏后回来的头几张角色 % 是 0 = 回来时先是空的, 黑 % 高 = 闪黑. 汇总: 变了的张数、最长没变多久、变得最多的一张.
std::string film_phase(const std::wstring& folder, const RECT& r, int seconds, int every_ms) {
  CreateDirectoryW(folder.c_str(), nullptr);
  WIN32_FIND_DATAW fd;  // 上一次拍得多的话, 多出来的旧帧会混进来: 先清掉这个文件夹里的
  HANDLE fh = FindFirstFileW((folder + L"\\*.bmp").c_str(), &fd);
  if (fh != INVALID_HANDLE_VALUE) {
    do DeleteFileW((folder + L"\\" + fd.cFileName).c_str());
    while (FindNextFileW(fh, &fd));
    FindClose(fh);
  }
  int w = r.right - r.left, h = r.bottom - r.top;
  std::vector<uint32_t> prev;
  std::string list = "[";
  int n = 0, changed = 0;
  double max_pct = 0, max_at = 0, last_change = 0, still_max = 0, t = 0;
  const double t0 = now_ms();
  for (; now_ms() - t0 < seconds * 1000.0; n++) {
    t = now_ms() - t0;
    std::vector<uint32_t> px = capture(r);
    long long diff = 0, content = 0, black = 0;
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++) {
        size_t i = size_t(y) * w + x;
        uint32_t p = px[i];
        int b = p & 0xff, g = (p >> 8) & 0xff, rr = (p >> 16) & 0xff;
        COLORREF e = checker(x, y);
        content += std::abs(rr - GetRValue(e)) + std::abs(g - GetGValue(e)) + std::abs(b - GetBValue(e)) > 30;
        black += rr + g + b <= 30;
        if (!prev.empty()) {
          uint32_t q = prev[i];
          diff += std::abs(b - int(q & 0xff)) + std::abs(g - int((q >> 8) & 0xff)) + std::abs(rr - int((q >> 16) & 0xff)) > 30;
        }
      }
    double all = double(w) * h, pct = prev.empty() ? 0 : diff * 100.0 / all;
    if (!prev.empty() && diff > 0) {
      changed++;
      still_max = (std::max)(still_max, t - last_change);
      last_change = t;
      if (pct > max_pct) max_pct = pct, max_at = t;
    }
    wchar_t name[16];
    swprintf(name, 16, L"\\%03d.bmp", n);
    save_bmp(folder + name, w, h, px);
    list += fmt("%s[%.0f,%.3f,%.2f,%.2f]", n ? "," : "", t, pct, content * 100.0 / all, black * 100.0 / all);
    prev.swap(px);
    double wait = t0 + double(n + 1) * every_ms - now_ms();
    if (wait > 1) Sleep(DWORD(wait));
  }
  still_max = (std::max)(still_max, t - last_change);
  std::string leaf = utf8(folder.substr(folder.find_last_of(L"\\/") + 1));
  return fmt("{\"dir\":\"%s\",\"every_ms\":%d,\"frames\":%d,\"changed\":%d,\"longest_still_ms\":%.0f,\"max_pct\":%.3f,\"max_at_ms\":%.0f,"
             "\"t_diff_content_black\":",
             json_escape(leaf).c_str(), every_ms, n, changed, still_max, max_pct, max_at) +
         list + "]}";
}

}  // namespace

void run_selftest(App& app) {
  std::string name = utf8(app.opt.host + L"-" + app.opt.ct);
  std::string json = fmt("{\"config\":\"%s\",\"webview2\":\"%s\"", name.c_str(), utf8(app.web.version).c_str());
  std::wstring dir = app.opt.out.empty() ? exe_dir() : app.opt.out.substr(0, app.opt.out.find_last_of(L"\\/"));
  auto finish = [&](bool ok) {
    json += fmt(",\"ok\":%s}", ok ? "true" : "false");
    logf("自检 %s: %s", name.c_str(), json.c_str());
    if (!app.opt.out.empty())
      if (FILE* f = _wfopen(app.opt.out.c_str(), L"ab")) {
        fputs(json.c_str(), f);
        fputc('\n', f);
        fclose(f);
      }
    app.selftest_ok = ok;
  };

  bool ready = wait_for([&] {
    std::lock_guard<std::mutex> lock(app.mu);
    return (app.page_ready && app.mask.valid()) || app.nav_failed;
  }, 90000);
  HitMask mask;
  std::string page;
  {
    std::lock_guard<std::mutex> lock(app.mu);
    ready = ready && !app.nav_failed && app.page_ready;
    page = app.page_info;
  }
  if (!ready) {
    json += ",\"error\":\"页面没有就绪 (见 wmpet.log)\"";
    finish(false);
    app.ui([&] { DestroyWindow(app.hwnd); });
    return;
  }

  app.ct_frozen = true;
  HWND backdrop = nullptr;
  RECT wr{};
  UINT dpi = 96;
  double scale = 0;
  app.ui([&] {
    GetWindowRect(app.hwnd, &wr);
    backdrop = create_backdrop(app.inst, wr);
    SetWindowPos(app.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    SetWindowPos(backdrop, app.hwnd, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);  // 紧贴在桌宠下面
    dpi = GetDpiForWindow(app.hwnd);
    scale = app.web.rasterization_scale();
  });
  Sleep(1500);
  {
    std::lock_guard<std::mutex> lock(app.mu);
    mask = app.mask;
  }
  int w = wr.right - wr.left, h = wr.bottom - wr.top;
  json += fmt(",\"dpi\":%u,\"raster_scale\":%.2f,\"window\":[%d,%d],\"mask\":{\"cols\":%d,\"rows\":%d,\"opaque\":%d,\"foot\":%d},\"page\":%s",
              dpi, scale, w, h, mask.cols, mask.rows, mask.opaque_cells(), mask.foot, page.c_str());

  POINT op{}, cp{};
  bool points = pick_points(mask, wr, op, cp);
  bool ok = points;
  json += ",\"states\":[";
  for (int on = 0; on < 2; on++) {
    app.ui([&] { app.set_click_through(on != 0); });
    Sleep(700);
    // 截图前现取最新的掩码 (至多晚 250 ms): StayStill 待机时角色一直在动, 用一秒多以前的掩码会把动出去的部分算成"不透明"
    HitMask now;
    {
      std::lock_guard<std::mutex> lock(app.mu);
      now = app.mask;
    }
    std::vector<uint32_t> px = capture(wr);
    save_bmp(dir + L"\\" + app.opt.host + L"-" + app.opt.ct + (on ? L"-穿透.bmp" : L"-接收.bmp"), w, h, px);
    Look l = classify(px, w, h, now.valid() ? now : mask);
    std::string at_char = points ? who_is_at(op, app.hwnd, backdrop) : "?";
    std::string beside = points ? who_is_at(cp, app.hwnd, backdrop) : "?";
    bool look_ok = l.bg_outside >= 0.97 && l.content_inside >= 0.25;
    bool hit_ok = on ? (at_char == "backdrop" && beside == "backdrop") : at_char == "pet";
    ok = ok && look_ok && hit_ok;
    json += fmt("%s{\"click_through\":%s,\"bg_outside\":%.4f,\"black_outside\":%.4f,\"content_inside\":%.4f,"
                "\"black_inside\":%.4f,\"at_character\":\"%s\",\"beside\":\"%s\",\"look_ok\":%s,\"hit_ok\":%s}",
                on ? "," : "", on ? "true" : "false", l.bg_outside, l.black_outside, l.content_inside, l.black_inside,
                json_escape(at_char).c_str(), json_escape(beside).c_str(), look_ok ? "true" : "false",
                hit_ok ? "true" : "false");
  }
  json += "]";
  bool film = app.opt.film_s > 0;  // 连拍时棋盘格留到对话/动作自检做完
  app.ui([&] {
    app.set_click_through(false);
    if (backdrop && !film) DestroyWindow(backdrop);
  });
  app.ct_frozen = false;

  // 对话自检 (页面地址带 talk=...) 或动作自检 (skills=1, 或 skills=squat,bow 只做这几个): 让页面开始, 等它把结果报回来. 只在 mock 服务上跑.
  // 连拍 (--film 秒数): 这段时间每 0.4 秒截一张桌宠 (下面是自检自己的棋盘格), 存成 film-NNN.bmp, 用来看动作和穿模.
  const std::wstring& qs = app.opt.query;
  if (qs.find(L"talk=") != std::wstring::npos || qs.find(L"skills=") != std::wstring::npos ||
      qs.find(L"ui=demo") != std::wstring::npos || qs.find(L"gaze=") != std::wstring::npos || qs.find(L"lean=") != std::wstring::npos ||
      qs.find(L"activity=") != std::wstring::npos || qs.find(L"roomtest=") != std::wstring::npos) {
    app.ui([&] { app.web.post_json(L"{\"t\":\"talk-go\"}"); });
    auto have = [&] {
      std::lock_guard<std::mutex> lock(app.mu);
      return !app.talk_result.empty();
    };
    int frames = 0;
    for (ULONGLONG t0 = GetTickCount64(); film && !have() && GetTickCount64() - t0 < ULONGLONG(app.opt.film_s) * 1000; frames++) {
      wchar_t name[32];
      swprintf(name, 32, L"\\film-%03d.bmp", frames);
      save_bmp(dir + name, w, h, capture(wr));
      Sleep(400);
    }
    if (film) {
      json += fmt(",\"film_frames\":%d", frames);
      app.ui([&] { DestroyWindow(backdrop); });
    }
    // 小屋自检 (roomtest=秒) 可能比 2 分钟长 (按顺序做一串事): 等它定的秒数再加 40 秒
    int wait_ms = 120000;
    if (size_t at = qs.find(L"roomtest="); at != std::wstring::npos) {
      wait_ms = (std::max)(wait_ms, (_wtoi(qs.c_str() + at + 9) + 40) * 1000);
    }
    bool got = wait_for(have, wait_ms);
    std::string talk;
    {
      std::lock_guard<std::mutex> lock(app.mu);
      talk = app.talk_result;
    }
    json += ",\"talk\":" + (got ? talk : std::string("null"));
    ok = ok && got;
  }

  if (app.opt.ledge_test) {
    std::string ledge = run_ledge_test(app);
    json += ",\"ledge\":" + ledge;
    const std::string pass = ",\"ok\":true}";  // 最外层的总结果在最后
    ok = ok && ledge.size() >= pass.size() && ledge.compare(ledge.size() - pass.size(), pass.size(), pass) == 0;
  }

  if (!app.opt.measure.empty()) {
    json += ",\"usage\":[";
    std::wstringstream ss(app.opt.measure);
    std::wstring phase;
    bool first = true;
    int idx = 0;
    while (std::getline(ss, phase, L',')) {
      idx++;
      bool hidden = phase == L"hidden";
      int fps = hidden ? 0 : _wtoi(phase.c_str());
      // 连拍 (--measure-film): 棋盘格先铺好、画出来再切 (切过来的头几帧, 比如隐藏后回来, 也要拍到); 拍完撤掉再测, 截图不算进 CPU
      bool shoot = app.opt.measure_film_s > 0 && !hidden;
      HWND under = nullptr;
      RECT fr{};
      auto stack = [&] {
        SetWindowPos(app.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        SetWindowPos(under, app.hwnd, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
      };
      if (shoot) {
        app.ui([&] {
          GetWindowRect(app.hwnd, &fr);
          under = create_backdrop(app.inst, fr);
          stack();
        });
        Sleep(300);
      }
      const double switched = now_ms();
      app.ui([&] {
        app.user_hidden = hidden;
        app.paused = !hidden && fps == 0;
        if (fps > 0) app.fps = fps;
        app.apply_visibility();
        if (under) stack();
      });
      std::string film;
      if (shoot) {
        film = film_phase(dir + L"\\film-measure-" + std::to_wstring(idx) + L"-" + phase, fr, app.opt.measure_film_s,
                          app.opt.measure_film_ms);
        app.ui([&] { DestroyWindow(under); });
      }
      if (double left = 2500 - (now_ms() - switched); left > 1) Sleep(DWORD(left));
      {
        std::lock_guard<std::mutex> lock(app.mu);
        app.last_stats.clear();
      }
      Usage u = measure(app.opt.measure_s);
      std::string stats;
      {
        std::lock_guard<std::mutex> lock(app.mu);
        stats = app.last_stats;
      }
      json += fmt("%s{\"phase\":\"%s\",\"cpu_pct\":%.2f,\"ws_mb\":%.1f,\"private_mb\":%.1f,\"gpu_3d_pct\":%.2f,"
                  "\"gpu_max_pct\":%.2f,\"gpu_max_engine\":\"%s\",\"vram_mb\":%.1f,\"shared_gpu_mb\":%.1f,\"procs\":%d,"
                  "\"page_stats\":\"%s\",\"pdh\":\"%s\",\"adapters\":%s,\"detail\":%s",
                  first ? "" : ",", utf8(phase).c_str(), u.cpu_pct, u.ws_mb, u.priv_mb, u.gpu_3d, u.gpu_max,
                  json_escape(u.gpu_max_eng).c_str(), u.vram_mb, u.shared_mb, u.procs, json_escape(stats).c_str(),
                  u.pdh.c_str(), u.adapters.c_str(), u.detail.c_str());
      json += film.empty() ? "}" : ",\"film\":" + film + "}";  // 连拍的逐帧列表可能很长, 不走 fmt (8 KB 的缓冲)
      first = false;
    }
    json += "]";
    app.ui([&] {
      app.user_hidden = false;
      app.paused = false;
      app.fps = 15;
      app.apply_visibility();
    });
  }
  finish(ok);
  app.ui([&] { DestroyWindow(app.hwnd); });
}
