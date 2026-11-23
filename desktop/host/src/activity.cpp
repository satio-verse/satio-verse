#include "activity.h"

#include <cwctype>
#include <vector>

#include "app.h"
#include "json.h"

namespace {

struct Known {
  const wchar_t* exe;
  const char* category;
  const char* app;
  bool keep_title;
};

// 程序名 (小写) -> 归类. keep_title: 标题留不留 (聊天软件、文件管理器、密码管理器不留; 游戏的标题就是游戏名, 不用留)
const Known kKnown[] = {
    {L"qq.exe", "chat", "QQ", false}, {L"tim.exe", "chat", "TIM", false}, {L"wechat.exe", "chat", "微信", false},
    {L"weixin.exe", "chat", "微信", false}, {L"wechatappex.exe", "chat", "微信", false}, {L"wxwork.exe", "chat", "企业微信", false},
    {L"dingtalk.exe", "chat", "钉钉", false}, {L"feishu.exe", "chat", "飞书", false}, {L"lark.exe", "chat", "飞书", false},
    {L"telegram.exe", "chat", "Telegram", false}, {L"discord.exe", "chat", "Discord", false}, {L"kook.exe", "chat", "KOOK", false},
    {L"slack.exe", "chat", "Slack", false}, {L"ms-teams.exe", "chat", "Teams", false}, {L"teams.exe", "chat", "Teams", false},
    {L"whatsapp.exe", "chat", "WhatsApp", false},
    {L"chrome.exe", "browser", "Chrome", true}, {L"msedge.exe", "browser", "Edge", true}, {L"firefox.exe", "browser", "Firefox", true},
    {L"360se.exe", "browser", "360 浏览器", true}, {L"360chrome.exe", "browser", "360 极速浏览器", true},
    {L"qqbrowser.exe", "browser", "QQ 浏览器", true}, {L"sogouexplorer.exe", "browser", "搜狗浏览器", true},
    {L"opera.exe", "browser", "Opera", true}, {L"brave.exe", "browser", "Brave", true}, {L"vivaldi.exe", "browser", "Vivaldi", true},
    {L"arc.exe", "browser", "Arc", true},
    {L"potplayermini64.exe", "video", "PotPlayer", true}, {L"potplayermini.exe", "video", "PotPlayer", true}, {L"vlc.exe", "video", "VLC", true},
    {L"mpc-hc64.exe", "video", "MPC-HC", true}, {L"哔哩哔哩.exe", "video", "哔哩哔哩", true}, {L"bilibili.exe", "video", "哔哩哔哩", true},
    {L"cloudmusic.exe", "music", "网易云音乐", true}, {L"qqmusic.exe", "music", "QQ 音乐", true}, {L"kugou.exe", "music", "酷狗音乐", true},
    {L"kwmusic.exe", "music", "酷我音乐", true}, {L"spotify.exe", "music", "Spotify", true}, {L"foobar2000.exe", "music", "foobar2000", true},
    {L"code.exe", "code", "VS Code", true}, {L"cursor.exe", "code", "Cursor", true}, {L"devenv.exe", "code", "Visual Studio", true},
    {L"idea64.exe", "code", "IntelliJ IDEA", true}, {L"pycharm64.exe", "code", "PyCharm", true}, {L"clion64.exe", "code", "CLion", true},
    {L"webstorm64.exe", "code", "WebStorm", true}, {L"goland64.exe", "code", "GoLand", true}, {L"rider64.exe", "code", "Rider", true},
    {L"sublime_text.exe", "code", "Sublime Text", true}, {L"notepad++.exe", "code", "Notepad++", true},
    {L"windowsterminal.exe", "code", "终端", true}, {L"cmd.exe", "code", "命令行", true}, {L"powershell.exe", "code", "PowerShell", true},
    {L"pwsh.exe", "code", "PowerShell", true}, {L"claude.exe", "code", "Claude", true},
    {L"winword.exe", "office", "Word", true}, {L"excel.exe", "office", "Excel", true}, {L"powerpnt.exe", "office", "PowerPoint", true},
    {L"onenote.exe", "office", "OneNote", true}, {L"outlook.exe", "office", "Outlook", true}, {L"wps.exe", "office", "WPS", true},
    {L"et.exe", "office", "WPS 表格", true}, {L"wpp.exe", "office", "WPS 演示", true}, {L"obsidian.exe", "office", "Obsidian", true},
    {L"notion.exe", "office", "Notion", true}, {L"typora.exe", "office", "Typora", true}, {L"acrobat.exe", "office", "Acrobat", true},
    {L"acrord32.exe", "office", "Acrobat Reader", true},
    {L"steam.exe", "game", "Steam", false}, {L"steamwebhelper.exe", "game", "Steam", false}, {L"epicgameslauncher.exe", "game", "Epic", false},
    {L"wegame.exe", "game", "WeGame", false}, {L"yuanshen.exe", "game", "原神", false}, {L"genshinimpact.exe", "game", "原神", false},
    {L"starrail.exe", "game", "崩坏：星穹铁道", false}, {L"zenlesszonezero.exe", "game", "绝区零", false},
    {L"league of legends.exe", "game", "英雄联盟", false}, {L"leagueclient.exe", "game", "英雄联盟", false},
    {L"leagueclientux.exe", "game", "英雄联盟", false}, {L"valorant-win64-shipping.exe", "game", "无畏契约", false},
    {L"cs2.exe", "game", "CS2", false}, {L"dota2.exe", "game", "DOTA 2", false}, {L"tslgame.exe", "game", "绝地求生", false},
    {L"minecraft.exe", "game", "Minecraft", false},
    {L"explorer.exe", "files", "文件资源管理器", false}, {L"keepass.exe", "other", "KeePass", false},
    {L"keepassxc.exe", "other", "KeePassXC", false}, {L"1password.exe", "other", "1Password", false}, {L"bitwarden.exe", "other", "Bitwarden", false},
};

// 网页标题里认得出的视频站: 认出来就算"在看视频", app 换成站名, 去掉站名后缀
struct Site {
  const wchar_t* key;   // 标题里出现这个 (小写比较)
  const char* name;
  const wchar_t* tail;  // 标题末尾要去掉的站名后缀 (可空)
};
const Site kSites[] = {
    {L"bilibili", "哔哩哔哩", L"_哔哩哔哩_bilibili"}, {L"哔哩哔哩", "哔哩哔哩", L"_哔哩哔哩_bilibili"}, {L"youtube", "YouTube", L" - YouTube"},
    {L"优酷", "优酷", nullptr}, {L"爱奇艺", "爱奇艺", nullptr}, {L"腾讯视频", "腾讯视频", nullptr}, {L"芒果tv", "芒果TV", nullptr},
    {L"抖音", "抖音", nullptr}, {L"netflix", "Netflix", L" | Netflix"}, {L"虎牙", "虎牙直播", nullptr}, {L"斗鱼", "斗鱼直播", nullptr},
};

std::wstring lower(std::wstring s) {
  for (auto& c : s) c = wchar_t(std::towlower(c));
  return s;
}

bool ends_with(const std::wstring& s, const std::wstring& tail) {
  return s.size() >= tail.size() && s.compare(s.size() - tail.size(), tail.size(), tail) == 0;
}

// 标题清理: 去掉控制字符和看不见的格式字符 (零宽、双向), 空白并成一个, 最多 40 个字
std::wstring clean_title(const std::wstring& t) {
  std::wstring out;
  bool space = false;
  for (wchar_t c : t) {
    if (c < 0x20 || c == 0x7f || (c >= 0x200b && c <= 0x200f) || (c >= 0x202a && c <= 0x202e) || (c >= 0x2066 && c <= 0x2069) || c == 0xfeff)
      continue;
    if (std::iswspace(c)) {
      space = !out.empty();
      continue;
    }
    if (space) out += L' ';
    space = false;
    out += c;
  }
  size_t n = 0, cut = out.size();
  for (size_t i = 0; i < out.size(); i++) {
    if (out[i] >= 0xdc00 && out[i] <= 0xdfff) continue;  // 代理对的后半个不算一个字
    if (++n > 40) {
      cut = i;
      break;
    }
  }
  if (cut < out.size()) out = out.substr(0, cut) + L"…";
  return out;
}

// 浏览器标题: 去掉浏览器名、Edge 的个人资料名和"和另外 N 个页面"
std::wstring strip_browser(std::wstring t) {
  static const wchar_t* kTails[] = {L" - Google Chrome", L" - Microsoft Edge", L" — Mozilla Firefox", L" - Mozilla Firefox",
                                    L" - Opera", L" - Brave", L" - Vivaldi", L" - 360安全浏览器", L" - 360极速浏览器",
                                    L" - QQ浏览器", L" - 搜狗高速浏览器", L" - 个人", L" - Personal", L" - 工作", L" - Work"};
  for (bool again = true; again;) {
    again = false;
    for (const wchar_t* tail : kTails) {
      if (ends_with(t, tail)) {
        t.resize(t.size() - wcslen(tail));
        again = true;
      }
    }
  }
  for (const wchar_t* more : {L" 和另外 ", L" and "}) {
    size_t at = t.rfind(more);
    if (at != std::wstring::npos && (t.find(L"个页面", at) != std::wstring::npos || t.find(L"more page", at) != std::wstring::npos))
      t.resize(at);
  }
  return t;
}

}  // namespace

ActivityInfo classify_activity(const std::wstring& exe_path, const std::wstring& raw_title) {
  ActivityInfo a;
  std::wstring path = lower(exe_path);
  size_t slash = path.find_last_of(L"\\/");
  std::wstring exe = slash == std::wstring::npos ? path : path.substr(slash + 1);
  std::wstring title = clean_title(raw_title);
  bool keep = true;
  const Known* k = nullptr;
  for (const Known& x : kKnown)
    if (exe == x.exe) k = &x;
  // Steam / Epic 装的游戏: 按安装目录认, 游戏名取 common\ 下面那一层目录名
  size_t common = path.find(L"\\steamapps\\common\\");
  size_t epic = path.find(L"\\epic games\\");
  if (!k && (common != std::wstring::npos || epic != std::wstring::npos)) {
    size_t from = common != std::wstring::npos ? common + 18 : epic + 12;
    size_t end = exe_path.find_first_of(L"\\/", from);
    a.category = "game";
    a.app = utf8(exe_path.substr(from, end == std::wstring::npos ? std::wstring::npos : end - from));
    return a;  // 游戏的标题就是游戏名, 不另外留
  }
  if (k) {
    a.category = k->category;
    a.app = k->app;
    keep = k->keep_title;
  } else {
    a.category = "other";
    a.app = utf8(exe.size() > 4 && ends_with(exe, L".exe") ? exe_path.substr(exe_path.size() - exe.size(), exe.size() - 4) : exe);
  }
  if (a.category == "browser") {
    std::wstring low = lower(title);
    for (const wchar_t* priv : {L"inprivate", L"无痕", L"incognito", L"隐身", L"private browsing", L"隐私浏览"})
      if (low.find(priv) != std::wstring::npos) keep = false;  // 无痕窗口: 标题不取
    title = strip_browser(title);
    low = lower(title);
    for (const Site& s : kSites) {
      if (low.find(s.key) == std::wstring::npos) continue;
      a.category = "video";
      a.app = s.name;
      if (s.tail && ends_with(title, s.tail)) title.resize(title.size() - wcslen(s.tail));
      break;
    }
  }
  if (keep) a.title = utf8(title);
  return a;
}

void ActivityWatch::reset() {
  last_ = ActivityInfo();
  sent_ = false;
}

void ActivityWatch::tick() {
  ActivityInfo now;
  LASTINPUTINFO li{sizeof li};
  DWORD idle_ms = GetLastInputInfo(&li) ? GetTickCount() - li.dwTime : 0;
  if (idle_ms >= 5 * 60 * 1000) {
    now.category = "away";
  } else {
    HWND fg = GetForegroundWindow();
    if (!fg) return;
    DWORD pid = 0;
    GetWindowThreadProcessId(fg, &pid);
    if (pid == GetCurrentProcessId()) return;  // 在和桌宠互动 (输入框、设置窗口): 照旧
    wchar_t cls[64] = L"";
    GetClassNameW(fg, cls, 64);
    if (!wcscmp(cls, L"Shell_TrayWnd") || !wcscmp(cls, L"Shell_SecondaryTrayWnd")) return;  // 点了任务栏: 照旧
    if (!wcscmp(cls, L"Progman") || !wcscmp(cls, L"WorkerW")) {
      now.category = "desktop";
    } else {
      std::wstring path;
      if (HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid)) {
        wchar_t buf[MAX_PATH * 2];
        DWORD n = ARRAYSIZE(buf);
        if (QueryFullProcessImageNameW(h, 0, buf, &n)) path.assign(buf, n);
        CloseHandle(h);
      }
      wchar_t title[256] = L"";
      GetWindowTextW(fg, title, 256);
      now = classify_activity(path, title);
    }
  }
  if (sent_ && now == last_) return;
  last_ = now;
  sent_ = true;
  Json m = Json::object();
  m.set("t", "activity").set("on", true).set("category", now.category).set("app", now.app).set("title", now.title);
  app_.web.post_json(widen(m.dump()));
}

int run_activity_test(const std::wstring& cases_path) {
  std::vector<char> data;
  Json cases, out = Json::array();
  if (!read_file(cases_path, data) || !Json::parse(std::string(data.begin(), data.end()), cases) || !cases.is_array()) return 2;
  for (const Json& c : cases.items()) {
    ActivityInfo a = classify_activity(widen(c.get_str("exe")), widen(c.get_str("title")));
    Json r = Json::object();
    r.set("exe", c.get_str("exe")).set("title_in", c.get_str("title")).set("category", a.category).set("app", a.app).set("title", a.title);
    out.push(r);
  }
  std::string line = out.dump() + "\n";
  HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
  DWORD n = 0;
  if (h && h != INVALID_HANDLE_VALUE) WriteFile(h, line.data(), DWORD(line.size()), &n, nullptr);
  return 0;
}
