#include "util.h"

#include <cstdarg>
#include <cstdio>
#include <mutex>

std::string utf8(const std::wstring& w) {
  if (w.empty()) return {};
  int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), int(w.size()), nullptr, 0, nullptr, nullptr);
  std::string s(n, '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.data(), int(w.size()), s.data(), n, nullptr, nullptr);
  return s;
}

std::wstring widen(const std::string& s) {
  if (s.empty()) return {};
  int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), nullptr, 0);
  std::wstring w(n, L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.data(), int(s.size()), w.data(), n);
  return w;
}

std::wstring exe_dir() {
  wchar_t buf[MAX_PATH];
  DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
  std::wstring p(buf, n);
  return p.substr(0, p.find_last_of(L"\\/"));
}

HICON app_icon(int size) {
  HICON icon = static_cast<HICON>(LoadImageW(GetModuleHandleW(nullptr), MAKEINTRESOURCEW(1), IMAGE_ICON, size, size, LR_DEFAULTCOLOR));
  return icon ? icon : LoadIconW(nullptr, IDI_APPLICATION);
}

std::wstring full_path(const std::wstring& p) {
  wchar_t buf[MAX_PATH];
  DWORD n = GetFullPathNameW(p.c_str(), MAX_PATH, buf, nullptr);
  return n && n < MAX_PATH ? std::wstring(buf, n) : p;
}

bool read_file(const std::wstring& path, std::vector<char>& out) {
  HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
  if (f == INVALID_HANDLE_VALUE) return false;
  LARGE_INTEGER size{};
  bool ok = GetFileSizeEx(f, &size) && size.QuadPart < (64 << 20);
  if (ok) {
    out.resize(size_t(size.QuadPart));
    DWORD got = 0;
    ok = out.empty() || (ReadFile(f, out.data(), DWORD(out.size()), &got, nullptr) && got == out.size());
  }
  CloseHandle(f);
  return ok;
}

static FILE* g_log = nullptr;
static std::mutex g_log_mu;

void log_open(const std::wstring& path) {
  g_log = _wfopen(path.c_str(), L"ab");
}

void logf(const char* fmt, ...) {
  char msg[2048];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof msg, fmt, ap);
  va_end(ap);
  SYSTEMTIME t;
  GetLocalTime(&t);
  char line[2200];
  snprintf(line, sizeof line, "%02d:%02d:%02d.%03d %s\n", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds, msg);
  std::lock_guard<std::mutex> lock(g_log_mu);
  OutputDebugStringA(line);
  if (g_log) {
    fputs(line, g_log);
    fflush(g_log);
  }
}

std::string json_escape(const std::string& s) {
  std::string o;
  o.reserve(s.size() + 8);
  for (unsigned char c : s) {
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if (c < 0x20) {
          char b[8];
          snprintf(b, sizeof b, "\\u%04x", c);
          o += b;
        } else {
          o += char(c);
        }
    }
  }
  return o;
}

std::string base64_encode(const uint8_t* data, size_t n) {
  static const char* tab = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  out.reserve((n + 2) / 3 * 4);
  for (size_t i = 0; i < n; i += 3) {
    unsigned v = unsigned(data[i]) << 16 | (i + 1 < n ? unsigned(data[i + 1]) << 8 : 0) | (i + 2 < n ? data[i + 2] : 0);
    out += tab[(v >> 18) & 63];
    out += tab[(v >> 12) & 63];
    out += i + 1 < n ? tab[(v >> 6) & 63] : '=';
    out += i + 2 < n ? tab[v & 63] : '=';
  }
  return out;
}

bool base64_decode(const std::string& in, std::vector<uint8_t>& out) {
  auto val = [](char c) -> int {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
  };
  out.clear();
  int acc = 0, n = 0;
  for (char c : in) {
    if (c == '=') break;
    int v = val(c);
    if (v < 0) return false;
    acc = (acc << 6) | v;
    n += 6;
    if (n >= 8) {
      n -= 8;
      out.push_back(uint8_t((acc >> n) & 0xff));
    }
  }
  return true;
}

int HitMask::opaque_cells() const {
  int n = 0;
  for (uint8_t b : bits) n += b;
  return n;
}

bool HitMask::parse(const std::string& msg, HitMask& out) {
  int cols = 0, rows = 0, foot = -1, used = 0;
  if (sscanf(msg.c_str(), "mask %d %d %d %n", &cols, &rows, &foot, &used) != 3 || cols <= 0 || rows <= 0 ||
      cols > 1024 || rows > 1024)
    return false;
  std::vector<uint8_t> packed;
  if (!base64_decode(msg.substr(size_t(used)), packed)) return false;
  size_t cells = size_t(cols) * rows;
  if (packed.size() < (cells + 7) / 8) return false;
  out.cols = cols;
  out.rows = rows;
  out.foot = foot;
  out.bits.assign(cells, 0);
  for (size_t i = 0; i < cells; i++) out.bits[i] = (packed[i >> 3] >> (i & 7)) & 1;
  out.received = GetTickCount64();
  return true;
}
