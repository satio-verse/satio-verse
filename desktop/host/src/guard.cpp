#include "guard.h"

#include <windows.h>
#include <dwmapi.h>
#include <uiautomation.h>

#include <cerrno>
#include <cmath>
#include <cwctype>
#include <limits>
#include <set>
#include <vector>

#include "activity.h"
#include "builtins.h"
#include "util.h"

namespace guard {
namespace {

const char* kWindowsMcp = "windows-mcp";

// Synthesized by autonomy::guard_config; never read settings again inside the guard.
bool is_unrestricted(const Json& extra) { return extra["_unrestricted"].boolean(false); }

struct TierOf {
  const char* tool;
  Tier tier;
};
// Windows-MCP 0.8.8 的 21 个工具 (最后 7 个是高风险档 power: 每次都问, 见 check_power). 表里没有的 (以后的新版本加的) 一律锁定: 先看清楚是什么再放开
const TierOf kWindowsMcpTiers[] = {
    {"ControlStatus", Tier::read},   {"DisplayInventory", Tier::read}, {"Wait", Tier::read},
    {"Screenshot", Tier::screen},    {"Snapshot", Tier::screen},       {"WaitFor", Tier::screen},
    {"Click", Tier::act},            {"Type", Tier::act},              {"Scroll", Tier::act},
    {"Move", Tier::act},             {"Shortcut", Tier::act},          {"MultiSelect", Tier::act},
    {"MultiEdit", Tier::act},        {"App", Tier::act},               {"PowerShell", Tier::power},
    {"FileSystem", Tier::power},     {"Registry", Tier::power},        {"Process", Tier::power},
    {"Clipboard", Tier::power},      {"Scrape", Tier::power},          {"Notification", Tier::power},
};

const char* kWhyChat = "发消息、看聊天要你自己来 (聊天和邮件软件她不碰)";
const char* kWhyPassword = "密码和登录要你自己来";
const char* kWhySystem = "改系统设置要你自己来";
const char* kWhyShell = "命令行要你自己来 (在里面打字等于执行命令)";
const char* kWhySelf = "桌宠自己的窗口 (确认卡片、停) 要你自己点";
const char* kWhyPay = "付款要你自己来";
const char* kWhyExplorer = "在文件管理器、桌面、任务栏里打字或按键可能会删文件、改名、运行东西, 要你自己来";
const char* kWhyStart = "在开始菜单、搜索框里打字会直接运行程序, 要你自己来";
const char* kWhyUnknown = "看不出这是哪个程序 (可能是管理员权限的窗口), 要你自己来";
const char* kWhyExtra = "这个程序在你的「不碰」名单里, 要你自己来";
const char* kWhyExtraWord = "这个内容在你的「不碰」关键词名单里, 要你自己来";
const char* kWhyBot = "QQ 机器人的后台 (SnowLuma、NapCat、AstrBot) 里有账号和 token, 要你自己来";

struct Hands {
  const wchar_t* exe;
  const char* why;
};
// 程序名 (小写) -> 为什么不碰. 聊天软件另按"看你在做什么"的归类认 (activity.cpp, 和那边同一份名单)
const Hands kHands[] = {
    {L"outlook.exe", kWhyChat}, {L"olk.exe", kWhyChat}, {L"hxoutlook.exe", kWhyChat}, {L"foxmail.exe", kWhyChat},
    {L"thunderbird.exe", kWhyChat}, {L"mailmaster.exe", kWhyChat},
    {L"keepass.exe", kWhyPassword}, {L"keepassxc.exe", kWhyPassword}, {L"1password.exe", kWhyPassword},
    {L"bitwarden.exe", kWhyPassword}, {L"credentialuibroker.exe", kWhyPassword},
    {L"systemsettings.exe", kWhySystem}, {L"systemsettingsadminflows.exe", kWhySystem}, {L"control.exe", kWhySystem},
    {L"mmc.exe", kWhySystem}, {L"regedit.exe", kWhySystem}, {L"taskmgr.exe", kWhySystem}, {L"resmon.exe", kWhySystem},
    {L"sechealthui.exe", kWhySystem}, {L"msconfig.exe", kWhySystem}, {L"msiexec.exe", kWhySystem}, {L"consent.exe", kWhySystem},
    {L"useraccountcontrolsettings.exe", kWhySystem}, {L"rundll32.exe", kWhySystem},
    {L"systempropertiesadvanced.exe", kWhySystem}, {L"systempropertiescomputername.exe", kWhySystem},
    {L"systempropertiesprotection.exe", kWhySystem}, {L"systempropertiesremote.exe", kWhySystem},
    {L"systempropertiesperformance.exe", kWhySystem},
    {L"cmd.exe", kWhyShell}, {L"powershell.exe", kWhyShell}, {L"pwsh.exe", kWhyShell}, {L"windowsterminal.exe", kWhyShell},
    {L"wt.exe", kWhyShell}, {L"conhost.exe", kWhyShell}, {L"openconsole.exe", kWhyShell}, {L"wsl.exe", kWhyShell},
    {L"wslhost.exe", kWhyShell}, {L"bash.exe", kWhyShell}, {L"mintty.exe", kWhyShell},
    // QQ 机器人的后台: NapCat 的桌面管理器和启动器 (它们把 QQ.exe 拉起来, QQ 本身按聊天软件认).
    // SnowLuma 用自带的 node.exe 跑, 控制台窗口按命令行认, 网页控制台按标题认 (kBotTitles)
    {L"napcatqq-desktop.exe", kWhyBot}, {L"napcatwinbootmain.exe", kWhyBot}, {L"astrbot-launcher.exe", kWhyBot},
};
// 只拦打字和按键 (点一下可以): 文件管理器 (含桌面、任务栏)、开始菜单和搜索
const Hands kNoTyping[] = {
    {L"explorer.exe", kWhyExplorer}, {L"searchhost.exe", kWhyStart}, {L"startmenuexperiencehost.exe", kWhyStart},
};
// 窗口标题里有这些 (小写): 付款页面
const wchar_t* kPayTitles[] = {L"支付", L"付款", L"收银台", L"网上银行", L"网银", L"转账", L"checkout", L"payment"};
// 窗口标题里有这些 (小写): QQ 机器人的控制台 (SnowLuma 在浏览器里 localhost:5099 "SnowLuma 控制台", NapCat 的 WebUI 6099, AstrBot 的面板)
const wchar_t* kBotTitles[] = {L"snowluma", L"napcat", L"astrbot"};

struct Word {
  const wchar_t* w;
  const char* why;
};
// 按钮、链接、菜单项的名字里有这些: 不点. 中文按"包含"认, 英文按整词认 (不区分大小写)
const Word kWords[] = {
    {L"支付", kWhyPay}, {L"付款", kWhyPay}, {L"购买", kWhyPay}, {L"下单", kWhyPay}, {L"提交订单", kWhyPay}, {L"结算", kWhyPay},
    {L"转账", kWhyPay}, {L"充值", kWhyPay}, {L"打赏", kWhyPay}, {L"pay", kWhyPay}, {L"buy", kWhyPay}, {L"purchase", kWhyPay},
    {L"checkout", kWhyPay}, {L"place order", kWhyPay},
    {L"发送", "发消息、发帖要你自己来"}, {L"发布", "发消息、发帖要你自己来"}, {L"回复", "发消息、发帖要你自己来"},
    {L"评论", "发消息、发帖要你自己来"}, {L"私信", "发消息、发帖要你自己来"}, {L"转发", "发消息、发帖要你自己来"},
    {L"send", "发消息、发帖要你自己来"}, {L"post", "发消息、发帖要你自己来"}, {L"reply", "发消息、发帖要你自己来"},
    {L"publish", "发消息、发帖要你自己来"}, {L"tweet", "发消息、发帖要你自己来"}, {L"comment", "发消息、发帖要你自己来"},
    {L"删除", "删东西要你自己来"}, {L"清空", "删东西要你自己来"}, {L"格式化", "删东西要你自己来"},
    {L"delete", "删东西要你自己来"}, {L"format", "删东西要你自己来"},
    {L"卸载", "装卸软件要你自己来"}, {L"安装", "装卸软件要你自己来"}, {L"install", "装卸软件要你自己来"},
    {L"uninstall", "装卸软件要你自己来"},
    {L"同意", "同意条款、授权要你自己来"}, {L"授权", "同意条款、授权要你自己来"}, {L"允许", "同意条款、授权要你自己来"},
    {L"接受", "同意条款、授权要你自己来"}, {L"agree", "同意条款、授权要你自己来"}, {L"accept", "同意条款、授权要你自己来"},
    {L"allow", "同意条款、授权要你自己来"}, {L"authorize", "同意条款、授权要你自己来"},
    {L"登录", "登录、注册要你自己来"}, {L"登入", "登录、注册要你自己来"}, {L"注册", "登录、注册要你自己来"},
    {L"login", "登录、注册要你自己来"}, {L"log in", "登录、注册要你自己来"}, {L"sign in", "登录、注册要你自己来"},
    {L"sign up", "登录、注册要你自己来"},
};
// App 按名字打开、切换的: 名字里有这些 (小写) 不行
const Word kAppNames[] = {
    {L"cmd", kWhyShell}, {L"powershell", kWhyShell}, {L"terminal", kWhyShell}, {L"终端", kWhyShell}, {L"命令", kWhyShell},
    {L"wsl", kWhyShell}, {L"bash", kWhyShell}, {L"ubuntu", kWhyShell},
    {L"regedit", kWhySystem}, {L"注册表", kWhySystem}, {L"设置", kWhySystem}, {L"settings", kWhySystem}, {L"control", kWhySystem},
    {L"控制面板", kWhySystem}, {L"任务管理器", kWhySystem}, {L"task manager", kWhySystem}, {L"组策略", kWhySystem},
    {L"gpedit", kWhySystem}, {L"服务", kWhySystem}, {L"services", kWhySystem}, {L"设备管理器", kWhySystem},
    {L"device manager", kWhySystem}, {L"安全中心", kWhySystem}, {L"security", kWhySystem}, {L"运行", kWhySystem},
    {L"qq", kWhyChat}, {L"tim", kWhyChat}, {L"微信", kWhyChat}, {L"wechat", kWhyChat}, {L"weixin", kWhyChat}, {L"钉钉", kWhyChat},
    {L"dingtalk", kWhyChat}, {L"飞书", kWhyChat}, {L"feishu", kWhyChat}, {L"lark", kWhyChat}, {L"wxwork", kWhyChat},
    {L"telegram", kWhyChat}, {L"discord", kWhyChat}, {L"slack", kWhyChat}, {L"teams", kWhyChat}, {L"whatsapp", kWhyChat},
    {L"kook", kWhyChat}, {L"outlook", kWhyChat}, {L"邮件", kWhyChat}, {L"mail", kWhyChat}, {L"foxmail", kWhyChat},
    {L"thunderbird", kWhyChat}, {L"snowluma", kWhyBot}, {L"napcat", kWhyBot}, {L"astrbot", kWhyBot},
    {L"keepass", kWhyPassword}, {L"1password", kWhyPassword}, {L"bitwarden", kWhyPassword}, {L"密码", kWhyPassword},
    {L"支付宝", kWhyPay}, {L"alipay", kWhyPay}, {L"银行", kWhyPay}, {L"bank", kWhyPay},
};
// 不管前台是谁都能按的: 切窗口、回桌面、贴边
const std::set<std::string> kNavKeys = {"alt+tab", "alt+shift+tab", "win+tab", "win+d", "win+m", "win+up", "win+down",
                                        "win+left", "win+right", "alt+esc", "esc"};
// 怎么都不按的: 运行框、系统菜单、设置、搜索、开始菜单、任务管理器、安全选项
const std::set<std::string> kBadKeys = {"win+r", "win+x", "win+i", "win+s", "win+q", "win", "ctrl+esc", "ctrl+shift+esc",
                                        "ctrl+alt+delete"};

// UI 自动化 (CUIAutomation / IUIAutomation; 自己写 GUID, 不依赖 MinGW 的 uuid 库里有没有)
const CLSID kClsidCUIAutomation = {0xff48dba4, 0x60ef, 0x4201, {0xaa, 0x87, 0x54, 0x10, 0x3e, 0xef, 0x59, 0x4e}};
const IID kIidIUIAutomation = {0x30cbe57d, 0xd9d0, 0x452a, {0xab, 0x13, 0x7a, 0xc5, 0xac, 0x48, 0x25, 0xee}};

std::wstring lower(std::wstring s) {
  for (auto& c : s) c = towlower(c);
  return s;
}

bool is_word_char(wchar_t c) { return iswalnum(c) && c < 128; }

// 名字里有没有这个词: 有中文的按包含, 纯英文的按整词
bool has_word(const std::wstring& name, const std::wstring& word) {
  bool ascii = true;
  for (wchar_t c : word) ascii = ascii && c < 128;
  for (size_t at = name.find(word); at != std::wstring::npos; at = name.find(word, at + 1)) {
    if (!ascii) return true;
    bool left = at == 0 || !is_word_char(name[at - 1]);
    bool right = at + word.size() >= name.size() || !is_word_char(name[at + word.size()]);
    if (left && right) return true;
  }
  return false;
}

std::wstring exe_of(DWORD pid) {
  std::wstring out;
  if (HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid)) {
    wchar_t buf[MAX_PATH * 2];
    DWORD n = ARRAYSIZE(buf);
    if (QueryFullProcessImageNameW(h, 0, buf, &n)) out.assign(buf, n);
    CloseHandle(h);
  }
  return out;
}

std::wstring base_name(const std::wstring& path) {
  size_t slash = path.find_last_of(L"\\/");
  return lower(slash == std::wstring::npos ? path : path.substr(slash + 1));
}

const char* extra_word_blocked(const std::wstring& raw, const Json& extra) {
  const std::wstring name = lower(raw);
  for (const Json& e : extra["words"].items())
    if (!e.str().empty() && name.find(lower(widen(e.str()))) != std::wstring::npos) return kWhyExtraWord;
  return nullptr;
}

const char* extra_process_blocked(const std::wstring& raw, const Json& extra) {
  auto name = [](const std::wstring& value) {
    std::wstring n = base_name(value);
    if (n.size() > 4 && n.substr(n.size() - 4) == L".exe") n.resize(n.size() - 4);
    return n;
  };
  const std::wstring target = name(raw);
  if (target.empty()) return nullptr;
  for (const Json& e : extra["processes"].items())
    if (!e.str().empty() && target == name(widen(e.str()))) return kWhyExtra;
  return nullptr;
}

const char* extra_args_blocked(const Json& args, const Json& extra) {
  if (args.is_string()) return extra_word_blocked(widen(args.str()), extra);
  for (const Json& child : args.items())
    if (const char* why = extra_args_blocked(child, extra)) return why;
  for (const auto& child : args.members())
    if (const char* why = extra_args_blocked(child.second, extra)) return why;
  return nullptr;
}

const char* element_name_blocked(const std::wstring& name, const Json& extra) {
  if (is_unrestricted(extra)) return extra_word_blocked(name, extra);
  if (name.empty() || name.size() > 40) return nullptr;
  for (const Word& w : kWords)
    if (has_word(name, w.w)) return w.why;
  for (const Json& e : extra["words"].items())
    if (!e.str().empty() && name.find(lower(widen(e.str()))) != std::wstring::npos) return "这个按钮在你的「不碰」名单里, 要你自己来";
  return nullptr;
}

// 这个进程的程序要不要整个躲开 (返回原因; 不用躲开时返回 nullptr). typing: 是打字、按键 (还要看 kNoTyping)
const char* hands_off(DWORD pid, bool typing, const Json& extra) {
  const bool unrestricted = is_unrestricted(extra);
  if (!unrestricted && pid == GetCurrentProcessId()) return kWhySelf;
  std::wstring path = exe_of(pid);
  if (path.empty()) return !unrestricted && pid ? kWhyUnknown : nullptr;
  std::wstring exe = base_name(path);
  if (unrestricted) return extra_process_blocked(exe, extra);
  if (classify_activity(path, L"").category == "chat") return kWhyChat;
  for (const Hands& h : kHands)
    if (exe == h.exe) return h.why;
  if (typing)
    for (const Hands& h : kNoTyping)
      if (exe == h.exe) return h.why;
  for (const Json& e : extra["processes"].items())
    if (!e.str().empty() && exe == lower(widen(e.str()))) return kWhyExtra;
  return nullptr;
}

std::wstring title_of(HWND w) {
  wchar_t buf[512];
  int n = GetWindowTextW(w, buf, ARRAYSIZE(buf));
  return std::wstring(buf, n > 0 ? n : 0);
}

// 一个窗口 (连同它的顶层窗口) 要不要躲开
const char* window_hands_off(HWND w, bool typing, const Json& extra) {
  if (!w) return nullptr;
  HWND root = GetAncestor(w, GA_ROOT);
  for (HWND h : {w, root}) {
    if (!h) continue;
    DWORD pid = 0;
    GetWindowThreadProcessId(h, &pid);
    if (const char* why = hands_off(pid, typing, extra)) return why;
  }
  std::wstring title = lower(title_of(root ? root : w));
  if (is_unrestricted(extra)) return extra_word_blocked(title, extra);
  for (const wchar_t* t : kPayTitles)
    if (title.find(t) != std::wstring::npos) return kWhyPay;
  for (const wchar_t* t : kBotTitles)
    if (title.find(t) != std::wstring::npos) return kWhyBot;
  return nullptr;
}

// 屏幕上 (这个虚拟桌面上没最小化的窗口里) 开着 QQ 机器人的控制台吗: 开着就不读屏 (网页里的账号和 token 会进元素树)
bool bot_console_visible() {
  bool found = false;
  EnumWindows(
      [](HWND w, LPARAM lp) -> BOOL {
        BOOL cloaked = FALSE;
        if (!IsWindowVisible(w) || IsIconic(w)) return TRUE;
        DwmGetWindowAttribute(w, DWMWA_CLOAKED, &cloaked, sizeof cloaked);  // 别的虚拟桌面上的窗口是"隐身"的
        if (cloaked) return TRUE;
        RECT rc;  // 很小的辅助窗口 (AstrBot Launcher 有一个 14x14 的单实例窗口) 不是控制台
        if (!GetWindowRect(w, &rc) || rc.right - rc.left < 100 || rc.bottom - rc.top < 60) return TRUE;
        std::wstring title = lower(title_of(w));
        DWORD pid = 0;
        GetWindowThreadProcessId(w, &pid);
        bool bot = hands_off(pid, false, Json()) == kWhyBot;
        for (const wchar_t* t : kBotTitles) bot = bot || title.find(t) != std::wstring::npos;
        if (bot) *reinterpret_cast<bool*>(lp) = true;
        return bot ? FALSE : TRUE;
      },
      reinterpret_cast<LPARAM>(&found));
  return found;
}

class Uia {
 public:
  Uia() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    owns_ = SUCCEEDED(hr);
    CoCreateInstance(kClsidCUIAutomation, nullptr, CLSCTX_INPROC_SERVER, kIidIUIAutomation, reinterpret_cast<void**>(a_.put()));
    if (a_) a_->get_ControlViewWalker(walker_.put());
  }
  ~Uia() {
    walker_.reset();
    a_.reset();
    if (owns_) CoUninitialize();
  }
  // 看一个元素: 自己和往上三层里的按钮、链接、菜单项的名字; typing 时看是不是密码框
  const char* check(IUIAutomationElement* el, bool typing, const Json& extra) {
    if (!el) return nullptr;
    int pid = 0;
    if (SUCCEEDED(el->get_CurrentProcessId(&pid)))
      if (const char* why = hands_off(DWORD(pid), typing, extra)) return why;
    BOOL pwd = FALSE;
    if (!is_unrestricted(extra) && typing && SUCCEEDED(el->get_CurrentIsPassword(&pwd)) && pwd) return kWhyPassword;
    Com<IUIAutomationElement> cur;
    cur.borrow(el);
    for (int depth = 0; cur && depth < 4; depth++) {
      CONTROLTYPEID type = 0;
      cur->get_CurrentControlType(&type);
      if (type == UIA_WindowControlTypeId) break;
      bool actionable = type == UIA_ButtonControlTypeId || type == UIA_SplitButtonControlTypeId || type == UIA_HyperlinkControlTypeId ||
                        type == UIA_MenuItemControlTypeId || type == UIA_CheckBoxControlTypeId ||
                        type == UIA_RadioButtonControlTypeId || type == UIA_ImageControlTypeId;
      if (actionable) {
        BSTR b = nullptr;
        std::wstring name;
        if (SUCCEEDED(cur->get_CurrentName(&b)) && b) name = lower(std::wstring(b, SysStringLen(b)));
        SysFreeString(b);
        if (const char* why = element_name_blocked(name, extra)) return why;
      }
      if (!walker_) break;
      Com<IUIAutomationElement> parent;
      walker_->GetParentElement(cur.get(), parent.put());
      cur = parent;
    }
    return nullptr;
  }
  const char* at_point(POINT pt, bool typing, const Json& extra) {
    if (!a_) return nullptr;
    Com<IUIAutomationElement> el;
    a_->ElementFromPoint(pt, el.put());
    return check(el.get(), typing, extra);
  }
  const char* focused(const Json& extra) {
    if (!a_) return nullptr;
    Com<IUIAutomationElement> el;
    a_->GetFocusedElement(el.put());
    return check(el.get(), true, extra);
  }

 private:
  bool owns_ = false;
  Com<IUIAutomation> a_;
  Com<IUIAutomationTreeWalker> walker_;
};

// 坐标: [x, y]、[x, y, 文字] 或写成字符串的 "[x, y]" / "x,y"
bool parse_point(const Json& v, POINT& pt) {
  if (v.is_array() && v.items().size() >= 2 && v.items()[0].type() == Json::Number && v.items()[1].type() == Json::Number) {
    const double x = v.items()[0].number(), y = v.items()[1].number();
    const double lo = std::numeric_limits<LONG>::min(), hi = std::numeric_limits<LONG>::max();
    if (!std::isfinite(x) || !std::isfinite(y) || x < lo || x > hi || y < lo || y > hi) return false;
    pt = {LONG(x), LONG(y)};
    return true;
  }
  if (!v.is_string()) return false;
  std::vector<long> nums;
  const std::string& s = v.str();
  for (size_t i = 0; i < s.size() && nums.size() < 2;) {
    if (isdigit(static_cast<unsigned char>(s[i])) || (s[i] == '-' && i + 1 < s.size() && isdigit(static_cast<unsigned char>(s[i + 1])))) {
      char* end = nullptr;
      errno = 0;
      const long value = strtol(s.c_str() + i, &end, 10);
      if (errno == ERANGE || value < std::numeric_limits<LONG>::min() || value > std::numeric_limits<LONG>::max()) return false;
      nums.push_back(value);
      i = size_t(end - s.c_str());
    } else {
      i++;
    }
  }
  if (nums.size() < 2) return false;
  pt = {nums[0], nums[1]};
  return true;
}

// locs: [[x, y], ...] 或写成字符串的同样内容
std::vector<POINT> parse_points(const Json& v) {
  std::vector<POINT> out;
  Json list = v;
  if (v.is_string()) Json::parse(v.str(), list);
  for (const Json& item : list.items()) {
    POINT pt;
    if (parse_point(item, pt)) out.push_back(pt);
  }
  return out;
}

// 快捷键写成统一的样子: 小写、别名换掉、修饰键按 ctrl alt shift win 的顺序
std::string norm_keys(const std::string& raw) {
  std::vector<std::string> mods, keys;
  std::string part;
  auto flush = [&] {
    if (part.empty()) return;
    std::string k;
    for (char c : part)
      if (c != ' ') k += char(tolower(static_cast<unsigned char>(c)));
    if (k == "control") k = "ctrl";
    if (k == "windows" || k == "super" || k == "meta" || k == "cmd" || k == "lwin" || k == "rwin") k = "win";
    if (k == "escape") k = "esc";
    if (k == "del") k = "delete";
    if (k == "return") k = "enter";
    if (k == "ctrl" || k == "alt" || k == "shift" || k == "win") mods.push_back(k);
    else keys.push_back(k);
    part.clear();
  };
  for (char c : raw) {
    if (c == '+') flush();
    else part += c;
  }
  flush();
  std::string out;
  for (const char* m : {"ctrl", "alt", "shift", "win"})
    for (const auto& x : mods)
      if (x == m) {
        out += (out.empty() ? "" : "+") + x;
        break;
      }
  for (const auto& k : keys) out += (out.empty() ? "" : "+") + k;
  return out;
}

const char* name_blocked(const std::string& name, bool apps_only_chat) {
  std::wstring n = lower(widen(name));
  if (n.empty()) return nullptr;
  for (const Word& w : kAppNames) {
    if (apps_only_chat && w.why != kWhyChat && w.why != kWhyPassword && w.why != kWhyPay && w.why != kWhyBot) continue;
    if (has_word(n, w.w)) return w.why;
  }
  return nullptr;
}

}  // namespace

Tier tier(const std::string& kind, const std::string& tool) {
  if (kind != kWindowsMcp) return Tier::none;
  for (const TierOf& t : kWindowsMcpTiers)
    if (tool == t.tool) return t.tier;
  return Tier::locked;
}

const char* tier_name(Tier t) {
  switch (t) {
    case Tier::read: return "read";
    case Tier::screen: return "screen";
    case Tier::act: return "act";
    case Tier::power: return "power";
    case Tier::locked: return "locked";
    default: return "";
  }
}

bool power_on(const Json& guard_cfg) { return guard_cfg["power"].boolean(true); }

Json args(const std::string& kind, const Json& args, bool power) {
  if (kind != kWindowsMcp || !args.is_array()) return args;
  Json out = Json::array();
  bool next_is_list = false;
  for (const Json& a : args.items()) {
    if (!next_is_list || !a.is_string()) {
      out.push(a);
      next_is_list = a.is_string() && a.str() == "--tools";
      continue;
    }
    next_is_list = false;
    std::string list = a.str();  // "A,B,C": 去掉高风险档的, 开着的话再统一加在最后 (不管老名单里有没有)
    std::string kept;
    for (size_t at = 0; at <= list.size();) {
      size_t comma = list.find(',', at);
      std::string name = list.substr(at, comma == std::string::npos ? std::string::npos : comma - at);
      if (!name.empty() && tier(kind, name) != Tier::power) kept += (kept.empty() ? "" : ",") + name;
      if (comma == std::string::npos) break;
      at = comma + 1;
    }
    if (power)
      for (const TierOf& t : kWindowsMcpTiers)
        if (t.tier == Tier::power) kept += (kept.empty() ? "" : ",") + std::string(t.tool);
    out.push(kept);
  }
  return out;
}

std::string hint(const std::string& kind, const std::string& tool, bool unrestricted) {
  if (kind != kWindowsMcp) return "";
  if (unrestricted) {
    if (tool == "Click" || tool == "Move" || tool == "MultiSelect" || tool == "MultiEdit")
      return "(用 loc / locs 坐标定位控件，不支持 label / labels 编号；仍遵守用户设置的不碰名单) ";
    if (tool != "Snapshot" && tool != "WaitFor") return "";
  }
  if (tool == "Snapshot" || tool == "WaitFor")
    return "(看浏览器里的网页用 use_dom=true; 它只读前台的浏览器窗口, 浏览器不在前台时先用 App mode=switch 切过去, 不然看到的是空的. 每次要 20 秒左右, 能少看就少看) ";
  if (tool == "Click") return "(坐标用 Snapshot 结果里元素前面的 (x,y)) ";
  // 高风险档: 每次都弹卡片问用户, 他点了允许才会真的做; 下面这些会直接被拦
  if (tool == "PowerShell") return "(每次都要用户点头, 命令写短一点、一次只做一件事、别拼很长的; 删文件、装软件、下载来运行、改注册表和系统设置、关机、编码过的命令会被拦下) ";
  if (tool == "FileSystem") return "(每次都要用户点头; 相对路径从桌面算; 删除、移动、覆盖已有文件、系统和程序目录、密钥令牌会被拦下) ";
  if (tool == "Registry") return "(每次都要用户点头; 只能读 get / list, 改和删会被拦下) ";
  if (tool == "Process") return "(每次都要用户点头; list 看进程; kill 请写名字, 系统、桌宠、命令行这类关不了) ";
  if (tool == "Clipboard") return "(每次都要用户点头; get 会把剪贴板里的内容发给云端模型, 里面可能有密码, 没必要别读) ";
  if (tool == "Scrape") return "(每次都要用户点头; 抓网页的文字, 本机地址、付款页面不行) ";
  if (tool == "Notification") return "(每次都要用户点头; 在桌面弹一条通知) ";
  return "";
}

Json schema(const std::string& kind, const std::string& tool, Json s, bool unrestricted) {
  if (kind != kWindowsMcp || !s.is_object()) return s;
  Json& props = s.at("properties");
  if (props.is_object()) {
    props.erase("label");
    props.erase("labels");
    if (tool == "Scrape") props.erase("use_sampling");  // 让服务器回头问我们这边的模型总结: 我们不接, 要原文
    if (tool == "App" && !unrestricted) {  // 默认只留按开始菜单名打开、切换、挪窗口
      props.erase("executable");
      props.erase("args");
      props.erase("cwd");
      Json& mode = props.at("mode");
      if (mode.is_object()) {
        Json modes = Json::array();
        for (const char* m : {"launch", "resize", "switch"}) modes.push(m);
        mode.set("enum", modes);
      }
    }
  }
  Json req = Json::array();
  for (const Json& r : s["required"].items())
    if (r.str() != "label" && r.str() != "labels" && !(tool == "Scrape" && r.str() == "use_sampling")) req.push(r);
  if (s.has("required")) s.set("required", req);
  return s;
}

Json env(const std::string& kind, const Json& extra) {
  Json e = Json::object();
  if (kind != kWindowsMcp) return e;
  e.set("ANONYMIZED_TELEMETRY", "false").set("POSTHOG_API_KEY", "");
  // 聊天、邮件、密码、命令行软件的窗口不进元素树 (标题还会出现在窗口列表里, 由 redact 换掉); 终端里可能有命令输出和密钥
  std::string skip = "QQ.exe,TIM.exe,WeChat.exe,Weixin.exe,WeChatAppEx.exe,WXWork.exe,DingTalk.exe,Feishu.exe,Lark.exe,Telegram.exe,"
                     "Discord.exe,KOOK.exe,Slack.exe,ms-teams.exe,Teams.exe,WhatsApp.exe,OUTLOOK.EXE,olk.exe,HxOutlook.exe,Foxmail.exe,"
                     "thunderbird.exe,mailmaster.exe,KeePass.exe,KeePassXC.exe,1Password.exe,Bitwarden.exe,NapCatQQ-Desktop.exe,"
                     "NapCatWinBootMain.exe,astrbot-launcher.exe,WindowsTerminal.exe,OpenConsole.exe,conhost.exe,cmd.exe,powershell.exe,pwsh.exe";
  if (is_unrestricted(extra)) skip.clear();
  for (const Json& p : extra["processes"].items())  // 用户在面板里加的
    if (p.is_string() && !p.str().empty() && p.str().find(',') == std::string::npos) skip += (skip.empty() ? "" : ",") + p.str();
  e.set("WINDOWS_MCP_EXCLUDE_PROCESSES", skip);
  return e;
}

int busy_wait_ms(const std::string& kind, const Json& result) {
  if (kind != kWindowsMcp || !result["isError"].boolean()) return 0;
  for (const Json& c : result["content"].items()) {
    Json body;
    if (!Json::parse(c.get_str("text"), body)) continue;  // {"code": "...", "status": {"state", "wait_seconds"}}
    std::string code = body.get_str("code");
    if (code == "CONTROL_UNAVAILABLE") return 700;
    double wait = body["status"]["wait_seconds"].number(-1);
    if (code == "USER_CONTROL" && wait >= 0 && wait <= 12) return int(wait * 1000) + 500;
  }
  return 0;
}

int connect_timeout_ms(const std::string& kind) { return kind == kWindowsMcp ? 90000 : 20000; }
int discover_timeout_ms(const std::string& kind) { return kind == kWindowsMcp ? 90000 : 8000; }

namespace {

// ---- 高风险档 (power): 执行命令、文件、注册表、进程、剪贴板、抓网页、通知 ------------------------------------------------------
// 这些每次都问用户 (卡片上写着要做什么), 这里只是先把一定不该做的拦掉: 删文件、装软件和下载来运行、改注册表和系统设置、关机、
// 编码过看不清的命令、系统目录和密钥/令牌/浏览器登录数据这类位置. 名单是尽力而为, 最后一道还是用户看着卡片点头.
bool any_of(const std::wstring& s, std::initializer_list<const wchar_t*> words) {
  for (const wchar_t* w : words)
    if (s.find(w) != std::wstring::npos) return true;
  return false;
}

bool any_word(const std::wstring& s, std::initializer_list<const wchar_t*> words) {
  for (const wchar_t* w : words)
    if (has_word(s, w)) return true;
  return false;
}

// 受保护的位置: 路径 (斜杠换成反斜杠、转小写以后) 里有这些就拦
const wchar_t* kProtectedPaths[] = {
    L"\\windows\\", L"\\windows", L"\\system32", L"\\syswow64", L"\\program files", L"\\programdata", L"\\appdata\\roaming\\worldmodel",
    L"\\.worldmodel", L"\\.ssh", L"\\.aws", L"\\.gnupg", L"\\.claude", L"\\login data", L"\\web data", L"\\cookies", L"\\local state",
    L"\\keepass", L"\\bitwarden", L"\\1password", L"pet-settings", L"pet-api-token", L"bili_cookie", L"id_rsa", L"id_ed25519", L"\\.env",
    L"\\ntuser", L"\\$recycle.bin", L"\\system volume information", L"\\appdata\\local\\packages\\claude"};
const char* kWhyPath = "这个位置 (系统和程序目录、桌宠的设置和令牌、密钥、浏览器和密码库的数据) 要你自己来";

std::wstring norm_path(std::string p) {
  std::wstring w = lower(widen(p));
  for (auto& c : w)
    if (c == L'/') c = L'\\';
  return w;
}

const char* path_blocked(const std::string& raw) {
  if (raw.empty()) return nullptr;
  std::wstring p = norm_path(raw);
  if (p.find(L'%') != std::wstring::npos || p.find(L"$env:") != std::wstring::npos || p[0] == L'~')
    return "路径里有变量或 ~, 写成完整的路径";
  for (const wchar_t* k : kProtectedPaths)
    if (p.find(k) != std::wstring::npos) return kWhyPath;
  return nullptr;
}

const char* shell_blocked(const std::string& command) {
  std::wstring c = lower(widen(command));
  if (c.size() > 1500) return "命令太长 (超过 1500 个字), 拆成几步, 好让你一眼看清楚";
  if (any_of(c, {L"remove-", L"rmdir", L"format-volume", L"clear-disk", L"diskpart", L"sdelete", L"cipher", L"::delete", L".delete("}) ||
      any_of(c, {L"format c:", L"format d:", L"format e:", L"format f:"}) || any_word(c, {L"rm", L"del", L"erase", L"rd", L"ri"}))
    return "删文件、格式化要你自己来";
  if (any_of(c, {L"shutdown", L"restart-computer", L"stop-computer", L"logoff", L"rundll32"})) return "关机、重启、注销要你自己来";
  if (any_of(c, {L"invoke-webrequest", L"invoke-restmethod", L"start-bitstransfer", L"downloadstring", L"downloadfile", L"downloaddata",
                 L"invoke-expression", L"winget", L"choco", L"scoop", L"msiexec", L"pip install", L"pip3 install", L"npm install",
                 L"npm i ", L"install-", L"uninstall", L"add-appxpackage"}) ||
      any_word(c, {L"iwr", L"irm", L"curl", L"wget", L"iex"}))
    return "下载、安装、运行下载来的东西要你自己来";
  if (any_of(c, {L"-encodedcommand", L"-enc ", L"frombase64string", L"-windowstyle hidden", L"-w hidden"}))
    return "命令里有编码过或隐藏窗口的内容, 看不清楚做什么, 要你自己来";
  if (any_of(c, {L"runas", L"-verb", L"set-executionpolicy", L"bypass"})) return "管理员权限、改脚本执行策略要你自己来";
  if (any_of(c, {L"set-itemproperty", L"new-itemproperty", L"hklm:", L"hkcu:\\software\\microsoft\\windows\\currentversion\\run", L"reg add",
                 L"reg delete", L"reg import", L"regedit", L"schtasks", L"register-scheduledtask", L"new-service", L"set-service",
                 L"sc.exe", L"sc config", L"sc create", L"bcdedit", L"netsh", L"set-netfirewall", L"new-netfirewall", L"disable-",
                 L"set-mppreference", L"add-mppreference", L"setx", L"setenvironmentvariable", L"powercfg", L"takeown", L"icacls",
                 L"new-localuser", L"net user", L"net localgroup", L"add-localgroupmember", L"wmic"}))
    return "改系统设置、注册表、启动项、服务、计划任务、账户要你自己来";
  if (any_of(c, {L"send-mailmessage", L"sendkeys"})) return "发消息、模拟按键要你自己来";
  for (const wchar_t* k : kProtectedPaths)
    if (c.find(k) != std::wstring::npos) return kWhyPath;
  return nullptr;
}

// 关了会出事的进程 (系统、桌宠自己、服务端、操控电脑的服务器、QQ 机器人): 按名字关也拦
const wchar_t* kKeepAlive[] = {L"wmpet", L"explorer", L"system", L"svchost", L"csrss", L"winlogon", L"lsass", L"services", L"dwm", L"smss",
                               L"wininit", L"msmpeng", L"securityhealth", L"windows-mcp", L"python", L"claude", L"snowluma", L"napcat",
                               L"astrbot", L"windowsterminal", L"powershell", L"pwsh", L"cmd", L"conhost"};

Verdict check_power(const std::string& tool, const Json& args) {
  Verdict v;
  auto no = [&](const std::string& why) {
    v.ok = false;
    v.why = why;
    return v;
  };
  std::string mode = args.get_str("mode");
  if (tool == "PowerShell") {
    if (const char* why = shell_blocked(args.get_str("command"))) return no(why);
  } else if (tool == "FileSystem") {
    if (mode == "delete") return no("删文件要你自己来");
    if (mode == "move") return no("移动、改名可能搬走或覆盖文件, 要你自己来");
    if (args["overwrite"].boolean() || args.get_str("overwrite") == "true") return no("覆盖已有的文件要你自己来");
    for (const char* key : {"path", "destination"})
      if (const char* why = path_blocked(args.get_str(key))) return no(why);
  } else if (tool == "Registry") {
    if (mode == "set" || mode == "delete") return no("改注册表等于改系统设置, 要你自己来");
    if (const char* why = path_blocked(args.get_str("path"))) return no(why);
  } else if (tool == "Process") {
    if (mode == "kill") {
      std::wstring name = lower(widen(args.get_str("name")));
      if (name.empty()) return no("关进程请写名字 (不要按编号), 好让你看清楚关的是谁");
      for (const wchar_t* k : kKeepAlive)
        if (name.find(k) != std::wstring::npos) return no("这个进程关了会出事 (系统、桌宠、服务端、命令行、QQ 机器人), 要你自己来");
    }
  } else if (tool == "Scrape") {
    std::string why = builtins::check_url(args.get_str("url"));
    if (!why.empty()) return no(why);
  }
  return v;
}

}  // namespace

Verdict check(const std::string& kind, const std::string& tool, const Json& args, const Json& extra) {
  Verdict v;
  Tier t = tier(kind, tool);
  if (t == Tier::none || t == Tier::read) return v;
  auto no = [&](const char* why) {
    v.ok = false;
    v.why = why;
    return v;
  };
  const bool unrestricted = is_unrestricted(extra);
  if (unrestricted) {
    if (const char* why = extra_args_blocked(args, extra)) return no(why);
    if (tool == "App" || tool == "Process")
      for (const char* key : {"name", "executable"})
        if (const char* why = extra_process_blocked(widen(args.get_str(key)), extra)) return no(why);
  }
  if (t == Tier::power) return unrestricted ? v : check_power(tool, args);
  // The caller tiers unknown tools as power in unrestricted mode and retains stop/deny handling.
  if (t == Tier::locked) return unrestricted ? v : no("这个工具锁定了, 不能用");
  if (args.has("label") && !args["label"].is_null()) return no("请用坐标 loc 指定位置 (Snapshot 里每个元素前面的 (x,y)), 不要用编号");
  if (args.has("labels") && !args["labels"].is_null()) return no("请用坐标 locs 指定位置, 不要用编号");
  POINT checked;
  for (const char* key : {"loc", "from_loc"})
    if (args.has(key) && !args[key].is_null() && !parse_point(args[key], checked)) return no("坐标必须是有效的 x,y 数值");
  if (args.has("locs") && !args["locs"].is_null()) {
    Json list = args["locs"];
    if (list.is_string() && !Json::parse(list.str(), list)) return no("locs 必须是坐标列表");
    if (!list.is_array()) return no("locs 必须是坐标列表");
    for (const Json& point : list.items())
      if (!parse_point(point, checked)) return no("locs 中每个坐标都必须有效");
  }
  if (t == Tier::screen) {
    if (unrestricted) {
      if (const char* why = screen_private(extra)) return no(why);
    } else if (bot_console_visible()) {
      return no("屏幕上开着 QQ 机器人的控制台 (SnowLuma、NapCat、AstrBot, 里面有账号和 token), 先关掉或最小化再让她看");
    }
  }
  if (tool == "WaitFor") {
    if (!unrestricted)
      if (const char* why = name_blocked(args.get_str("window_name"), true)) return no(why);
    return v;
  }
  if (t == Tier::screen) return v;  // 截图、元素树: 结果里不碰的窗口的标题由 redact 换掉, 截图本身不发云端
  if (tool == "App") {
    std::string mode = args.get_str("mode", "launch");
    if (!unrestricted) {
      if (mode == "launch_executable") return no("按路径带参数启动程序等于执行命令, 要你自己来");
      if (const char* why = name_blocked(args.get_str("name"), mode != "launch")) return no(why);
    }
    return v;
  }
  Uia uia;
  if (tool == "Shortcut") {
    std::string keys = norm_keys(args.get_str("shortcut"));
    if (keys.empty()) return no("没说按什么键");
    if (!unrestricted && kBadKeys.count(keys)) return no("这个快捷键会打开运行框、系统设置或开始菜单, 要你自己来");
    if (!unrestricted && kNavKeys.count(keys)) return v;
    if (const char* why = window_hands_off(GetForegroundWindow(), true, extra)) return no(why);
    if (const char* why = uia.focused(extra)) return no(why);
    return v;
  }
  bool typing = tool == "Type" || tool == "MultiEdit";
  std::vector<POINT> points;
  POINT pt;
  if (parse_point(args["loc"], pt)) points.push_back(pt);
  if (parse_point(args["from_loc"], pt)) points.push_back(pt);
  for (const POINT& p : parse_points(args["locs"])) points.push_back(p);
  if (points.empty()) {
    if (tool == "Scroll" && GetCursorPos(&pt)) {
      points.push_back(pt);  // 没给位置: 在光标那里滚
    } else if (tool == "Type") {  // 没给位置: 打进现在有焦点的地方
      if (const char* why = window_hands_off(GetForegroundWindow(), true, extra)) return no(why);
      if (const char* why = uia.focused(extra)) return no(why);
      return v;
    } else {
      return no("请用坐标 loc 指定位置");
    }
  }
  for (const POINT& p : points) {
    HWND w = WindowFromPoint(p);
    DWORD pid = 0;
    GetWindowThreadProcessId(w ? GetAncestor(w, GA_ROOT) : nullptr, &pid);
    // 诊断: 落点处是哪个程序 (只记程序名, 不记标题; 守门没拦住时看得出当时点到了谁)
    logf("守门: %s (%ld,%ld) 落在 %s", tool.c_str(), p.x, p.y, utf8(base_name(exe_of(pid))).c_str());
    if (const char* why = window_hands_off(w, typing, extra)) return no(why);
    if (const char* why = uia.at_point(p, typing, extra)) return no(why);
  }
  return v;
}

std::string redact(const std::string& text, const Json& extra) {
  struct Ctx {
    const Json* extra;
    std::vector<std::string> titles;
  } ctx{&extra, {}};
  EnumWindows(
      [](HWND w, LPARAM lp) -> BOOL {
        auto* c = reinterpret_cast<Ctx*>(lp);
        if (!IsWindowVisible(w)) return TRUE;
        std::wstring title = title_of(w);
        if (title.empty() || (!is_unrestricted(*c->extra) && title.size() < 2)) return TRUE;
        DWORD pid = 0;
        GetWindowThreadProcessId(w, &pid);
        const char* why = is_unrestricted(*c->extra) ? window_hands_off(w, false, *c->extra) : hands_off(pid, false, *c->extra);
        if (why == kWhyChat || why == kWhyPassword || why == kWhyExtra || why == kWhyBot || why == kWhyExtraWord)
          c->titles.push_back(utf8(title));
        return TRUE;
      },
      reinterpret_cast<LPARAM>(&ctx));
  std::string out = text;
  for (const std::string& t : ctx.titles)
    for (size_t at = out.find(t); at != std::string::npos; at = out.find(t, at)) {
      static const std::string mark = "[她不看的窗口]";
      out.replace(at, t.size(), mark);
      at += mark.size();
    }
  return out;
}

const char* screen_private(const Json& extra, std::string* who) {
  struct Scan {
    const Json* extra;
    const char* why = nullptr;
    std::string exe;
  } scan{&extra};
  EnumWindows(
      [](HWND w, LPARAM lp) -> BOOL {
        Scan* s = reinterpret_cast<Scan*>(lp);
        BOOL cloaked = FALSE;
        const bool unrestricted = is_unrestricted(*s->extra);
        if (!IsWindowVisible(w) || IsIconic(w) || (!unrestricted && w == GetShellWindow())) return TRUE;
        DwmGetWindowAttribute(w, DWMWA_CLOAKED, &cloaked, sizeof cloaked);
        if (cloaked) return TRUE;
        RECT rc;  // 很小的辅助窗口不是内容
        if (!GetWindowRect(w, &rc) || rc.right <= rc.left || rc.bottom <= rc.top) return TRUE;
        if (!unrestricted && (rc.right - rc.left < 100 || rc.bottom - rc.top < 60)) return TRUE;
        DWORD pid = 0;
        GetWindowThreadProcessId(w, &pid);
        if (!unrestricted && pid == GetCurrentProcessId()) return TRUE;  // 默认不把桌宠透明全屏窗口算作私密内容
        if (const char* why = window_hands_off(w, false, *s->extra)) {
          s->why = why;
          s->exe = utf8(base_name(exe_of(pid)));
          return FALSE;
        }
        return TRUE;
      },
      reinterpret_cast<LPARAM>(&scan));
  if (who) *who = scan.exe;
  return scan.why;
}

}  // namespace guard
