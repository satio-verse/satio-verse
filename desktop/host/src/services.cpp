#include "services.h"

#include <windows.h>

#include "json.h"
#include "util.h"

namespace services {
namespace {

std::wstring g_root, g_python;

std::wstring home() {
  wchar_t buf[MAX_PATH] = {};
  DWORD n = GetEnvironmentVariableW(L"USERPROFILE", buf, MAX_PATH);
  return (n && n < MAX_PATH ? std::wstring(buf) : std::wstring(L".")) + L"\\.worldmodel\\services";
}

bool alive(DWORD pid) {
  if (!pid) return false;
  HANDLE h = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
  if (!h) return false;
  bool on = WaitForSingleObject(h, 0) == WAIT_TIMEOUT;
  CloseHandle(h);
  return on;
}

// 起一个和桌宠脱开的 pythonw (不弹窗; 桌宠退出它照跑). 桌宠在作业对象里时先试着脱出来, 不让就照常起
bool spawn(const std::wstring& args) {
  if (g_python.empty()) return false;
  std::wstring line = L"\"" + g_python + L"\" " + args;
  STARTUPINFOW si{sizeof si};
  PROCESS_INFORMATION pi{};
  DWORD flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT;
  for (DWORD extra : {DWORD(CREATE_BREAKAWAY_FROM_JOB), DWORD(0)}) {
    std::wstring buf = line;
    if (CreateProcessW(nullptr, buf.data(), nullptr, nullptr, FALSE, flags | extra, nullptr, g_root.c_str(), &si, &pi)) {
      CloseHandle(pi.hThread);
      CloseHandle(pi.hProcess);
      logf("本地服务: %s", utf8(args).c_str());
      return true;
    }
  }
  logf("本地服务: 起不来 %s (错误 %lu)", utf8(line).c_str(), GetLastError());
  return false;
}

}  // namespace

void configure(const std::wstring& root, const std::wstring& python) {
  g_root = full_path(root);
  g_python = python;
  if (g_python.empty()) {
    wchar_t buf[MAX_PATH] = {};
    if (SearchPathW(nullptr, L"pythonw.exe", nullptr, MAX_PATH, buf, nullptr)) g_python = buf;
  }
  // PATH 里没有: 用桌宠自带的便携 Python (tools/setup_python.py 装的 runtime/python, 别的电脑上不用另装 Python)
  std::wstring own = g_root + L"\\runtime\\python\\pythonw.exe";
  if (g_python.empty() && GetFileAttributesW(own.c_str()) != INVALID_FILE_ATTRIBUTES) g_python = own;
  logf("本地服务用的 Python: %s", g_python.empty() ? "(找不到)" : utf8(g_python).c_str());
}

bool found_python() { return !g_python.empty(); }

bool start() { return spawn(L"-m wm.services run"); }

bool command(const std::string& what, const std::string& name) {
  return spawn(L"-m wm.services " + widen(what) + (name.empty() ? L"" : L" " + widen(name)));
}

std::wstring logs_dir() { return g_root + L"\\logs\\services"; }

State read() {
  State st;
  std::vector<char> raw;
  Json j;
  if (!read_file(home() + L"\\state.json", raw) || !Json::parse(std::string(raw.begin(), raw.end()), j) || !j.is_object()) return st;
  st.running = alive(DWORD(j["pid"].number())) && !j["stopping"].boolean();
  for (const Json& s : j["services"].items()) {
    st.items.push_back({s.get_str("name"), s.get_str("label"), s.get_str("state"), s.get_str("error"), int(s["port"].number())});
  }
  return st;
}

std::wstring mark(const Item& it) {
  const std::string& s = it.state;
  const wchar_t* dot = (s == "up" || s == "external") ? L"●" : s == "starting" ? L"◌" : (s == "down" || s == "failed") ? L"✕" : L"○";
  const wchar_t* word = s == "up" ? L"在跑" : s == "external" ? L"在跑 (外面开的)" : s == "starting" ? L"启动中…"
                      : s == "down" ? L"挂了, 等重启" : s == "failed" ? L"起不来, 点一下再试" : s == "stopped" ? L"停了, 点一下开" : L"还没开";
  return std::wstring(dot) + L"  " + widen(it.label) + L" · " + std::to_wstring(it.port) + L" · " + word;
}

}  // namespace services
