// 桌宠宿主 (M0): 透明、置顶、按角色像素点穿、可拖动、有托盘的窗口, 里面是 WebView2 打开的桌宠页面.
#pragma once
#include <windows.h>
#include <shellapi.h>

#include <atomic>
#include <mutex>
#include <thread>

#include "json.h"
#include "services.h"
#include "webview_host.h"

class PetApi;
class PanelWindow;
class WorkbenchWindow;
class ToolHub;
class ActivityWatch;

struct Options {
  std::wstring server = L"http://127.0.0.1:8790";
  std::wstring web_dir;            // 默认 <exe>\..\..\web
  std::wstring host = L"hwnd";     // hwnd | comp
  std::wstring ct = L"layered";    // 点穿方式: layered (一直是分层窗口, 只切 WS_EX_TRANSPARENT) | toggle (两个一起切) | plain (只切 WS_EX_TRANSPARENT, 对照)
  std::wstring out;                // 自检结果 (jsonl, 追加)
  std::wstring measure;            // 自检时测资源的阶段, 例如 "hidden,0,15,30,60"
  std::wstring query;              // 附加在页面地址后的查询参数, 例如 "tex=0"
  bool memory_low = true;          // 页面就绪后把 WebView2 的内存目标设成"低" (--memory-level normal 关掉)
  bool ledge_test = false;         // 自检时再测站在窗口顶边 (自建测试窗口)
  std::wstring fake_mic;           // 自检: 用这个 WAV 当麦克风 (Chromium 的假设备), 不碰真实麦克风
  int film_s = 0;                  // 自检: 对话/动作自检期间连拍这么多秒 (0 不拍)
  int measure_s = 10;
  int measure_film_s = 0;          // 自检: 测资源的每个阶段 (隐藏除外) 从切过去那一刻起连拍这么多秒, 拍完再测 (0 不拍)
  int measure_film_ms = 50;        // 上面那段连拍多久截一张
  bool api = false;               // 本机接口 (OpenAI 兼容 /v1 和 MCP /mcp, 见 api.h): 默认关, --api 或托盘里打开
  int api_port = 8793;
  int width = 420, height = 800;   // 逻辑像素 (96 DPI 时); 顶部约 16% 留给气泡
  bool selftest = false;
  bool devtools = false;
  bool mcp_stdio = false;          // --mcp-stdio: 只做 MCP 的 stdio 转发
  // 操作面板 (panel.h)
  bool panel_at_start = false;     // --panel: 页面就绪后马上打开面板
  std::wstring panel_query;        // --panel-query: 面板页面的查询参数 (自检: "selftest=1")
  std::wstring panel_shot;         // --panel-shot <路径前缀>: 自检, 依次截面板的每一页 (<前缀>-<页>.png), 截完退出
  // 工作台 (workbench.h)
  bool workbench_at_start = false; // --workbench: 页面就绪后马上打开工作台
  std::wstring workbench_query;    // --workbench-query: 工作台页面的查询参数
  std::wstring workbench_shot;     // --workbench-shot <png>: 自检, 工作台页面就绪 2 秒后截一张, 截完退出
  std::wstring settings_dir;       // --settings-dir: 设置和 skill 换个目录放 (自检用, 不碰用户真正的设置)
  std::wstring mcp_probe;          // --mcp-probe <配置.json>: 用 MCP 客户端连一次, 结果写到 stdout
  std::string mcp_call;            // --mcp-call <工具名> <参数.json>: 连上之后再调一个工具
  std::wstring mcp_call_args;
  std::wstring ask_shot;           // --ask-shot <png>: 自检, 第一次弹工具确认框时截一张页面
  bool dry_open = false;           // --dry-open: 自检, 自带工具 open_url 不真开浏览器 (只记日志)
  bool task_shot_any = false;      // --task-shot-any: 自检, QQ 任务单暂存截图时不看屏幕上有没有聊天、命令行这类窗口 (测暂存和回报)
  bool no_autohide = false;        // --no-autohide: 自检, 用户开着全屏程序时也不自动隐藏 (隐藏时本机接口回 409, 测试就断了)
  int lean_after_s = -1;           // --lean-after <秒>: 放着不动多久趴下 (默认 15, 自检默认不趴; 0 = 不趴)
  std::wstring activity_test;      // --activity-test <cases.json>: 只测"看你在做什么"的归类, 结果写到 stdout, 不开窗口
  bool services = true;            // 启动时在后台拉起本地服务的看守 (services.h): 默认开, 双击 exe 也会起; --no-services 关掉
  bool services_given = false;     // 命令行里显式给了 --services / --no-services (自检默认不起服务, 显式给了就听)
  std::wstring python;             // --python <pythonw.exe>: 看守用哪个 Python (默认在 PATH 里找)
};

class App {
 public:
  Options opt;
  HINSTANCE inst = nullptr;
  HWND hwnd = nullptr;
  WebViewHost web;

  // 页面状态: UI 线程写, 自检线程读
  std::mutex mu;
  HitMask mask;
  std::string page_info = "{}", last_stats, talk_result;
  bool page_ready = false;
  // 工作台和桌宠合在一起 (2026-10-26 用户: "工作台打开的时候桌宠的小屋等都最小化关闭, 右侧的功能键放到工作台上, 设置也是")
  void on_workbench(bool shown);                                  // 工作台显示 / 关了或最小化: 收起 / 放回桌宠
  void dock_action(const std::string& act, const std::string& arg);  // 工作台右边的功能键
  void send_dock();                                               // 功能键的状态告诉工作台页面
  void set_persona(const std::string& id);                        // 换人设预设 (侧菜单、工作台都用)
  void panel_message(const std::string& text, WebViewHost* from);  // 工作台里的设置页 (iframe) 经工作台转来的 "panel ..." 消息
  Json pet_prefs() const;                                         // 设置"桌宠与工作台"页: 托盘里那些开关
  void set_pet_pref(const std::string& key, const Json& v);
  services::State svc_items;  // 托盘菜单打开时读的本地服务状态 (点第几项 = 重启第几样)
  bool nav_failed = false;

  // 显示与帧率
  // 空闲帧率 (托盘可改). 原来默认 10 帧省电 (核显 3D 约 3.5%), 用户嫌待机、头发一卡一卡的, 改成 30 (约 8%); 想省电在托盘里调回 10 / 15
  int fps = 30;
  bool paused = false;       // 暂停渲染
  bool user_hidden = false;  // 托盘里隐藏
  bool auto_hidden = false;  // 全屏程序、游戏、演示时自动隐藏
  bool peeking = false;      // 全屏自动隐藏时她陪看说了一句 (页面发 "peek 1"): 临时露出对话栏, 说完 (页面发 "peek 0") 收回
  bool wb_hidden = false;    // 工作台开着 (没最小化) 时收起: 她在工作台里 (麦克风照常, 只是不画)
  bool wb_hides = true;      // 设置: 打开工作台时收起桌宠 (pet.workbench_hides)
  bool muted = false;        // 托盘里静音
  bool mic_on = false;       // 托盘或侧菜单开了麦克风 (默认关; 隐藏时暂停采音, 显示回来再开)
  bool hovered = false;      // 光标在角色 (或对话栏、菜单) 上
  bool topmost = true;       // 置顶 (侧菜单的图钉、托盘里切换, 存在设置里). 不置顶时和普通窗口一样会被别的窗口盖住
  bool follow_mouse = true;  // 眼睛跟着鼠标 (托盘里切换, 存在设置里): 开着时把光标位置发给页面
  double cursor_x = -1e9, cursor_y = -1e9;  // 上一次发给页面的光标位置 (窗口的比例)
  void send_mic();
  void send_ui();            // 把麦克风、静音、置顶的状态告诉页面 (侧菜单的按钮跟着变)
  void set_topmost(bool on);
  void send_cursor();
  void save_prefs();         // 置顶、眼睛跟着鼠标写进设置 (下次启动照用)
  void update_tray_tip();
  int effective_fps() const { return (paused || user_hidden || (auto_hidden && !peeking) || wb_hidden) ? 0 : fps; }
  // 现在是不是"全屏时露出对话栏": 只在因全屏自动隐藏时露, 托盘里隐藏的、工作台收起的不露
  bool peek_shown() const { return peeking && auto_hidden && !user_hidden && !wb_hidden; }
  void send_fps();
  void apply_visibility();
  void set_user_hidden(bool hidden) {
    if (hidden == user_hidden) return;
    user_hidden = hidden;
    apply_visibility();
  }

  // 点穿
  bool ct_on = false;
  std::atomic<bool> ct_frozen{false};  // 自检时由自检线程控制
  void set_click_through(bool on);
  void update_click_through();

  // 拖动、落地、站在窗口顶边 (见 ledge.h)
  bool dragging = false, falling = false;
  POINT drag_off{};
  double fall_y = 0, fall_v = 0;
  int fall_x = 0, fall_target = 0;
  ULONGLONG fall_last = 0;
  HWND fall_onto = nullptr;  // 正在往哪个窗口上掉 (空: 工作区底边)
  HWND stand_on = nullptr;   // 站在哪个窗口上; 那个窗口移动就跟着走, 没了就往下掉
  int stand_dx = 0;          // 桌宠窗口左边相对那个窗口左边的偏移
  double foot_frac = -1;     // 页面报的脚底位置 (窗口高度的比例); 没报时用掩码最下面一行
  // 松手以后: 默认停在松手的地方 (用户要把桌宠放到自己想放的位置), 脚离下面的地面 (窗口顶边、任务栏顶边) 很近时才吸上去;
  // 托盘里打开"松手落到地面"时照原来那样往下掉. 位置存在设置里 (pet.pos: 左、上、是不是站在地面上), 下次启动放回去
  bool gravity = false;
  bool grounded = true;      // 站在地面上 (停在半空时不趴)
  void start_drag();
  void drag_tick();
  void settle_free();
  void begin_fall();
  void stand_tick();
  void drop_from(int left, int top);  // 把桌宠窗口放到这里再松手 (自检用)
  int foot_px(int window_h);
  void place_initial();
  void place_on_ground();    // 按现在的脚底 (或趴着时的栏杆线) 站回地面: 窗口顶边上的交给 stand_tick

  // 趴着: 在地面 (任务栏顶边或窗口顶边) 上放着不动 lean_after_s 秒, 就把地面当栏杆趴上去 (胳膊叠在边上, 下半身藏在后面).
  // 页面摆姿势并报"栏杆线"在窗口里的位置 (lean_frac), 宿主把窗口往下挪, 让这条线落在地面上. 拖动时站起来
  bool lean_on = true;       // 托盘里切换, 存在设置里
  bool leaning = false;
  bool lean_unsupported = false;  // 页面说这个角色做不了 (没有行动层的 3D)
  double lean_frac = -1;     // 页面报的栏杆线 (窗口高度的比例)
  ULONGLONG settled_at = 0;  // 落地的时刻 (0: 正在拖、正在掉)
  void lean_tick();
  void set_leaning(bool on, bool replace = true);  // replace: 站起来时顺便站回地面

  // 托盘
  NOTIFYICONDATAW nid{};
  UINT taskbar_created = 0;
  void tray_add();
  void tray_menu();
  void on_command(int id);

  void check_fullscreen();
  void take_focus();  // 打开输入框时把窗口设成前台, 键盘才进得来
  void on_page(const std::string& msg);
  void ui(std::function<void()> fn);  // 在 UI 线程上同步执行 (自检线程用)
  void post_ui(std::function<void()> fn);  // 投递到 UI 线程, 不等它执行完 (低级钩子里用)
  // 操控电脑 (tools.h): 一键停的全局快捷键 Ctrl+Alt+Shift+Backspace (工具表里有操控类服务器时才注册);
  // 操控期间用低级键鼠钩子看真人动没动鼠标键盘 (面板里可以关)
  bool stop_hotkey = false;
  void set_stop_hotkey(bool on);
  void set_input_watch(bool on);
  // 正在操作电脑 (和停下后 5 秒内) 不因为"全屏程序"隐藏: Windows-MCP 操作时屏幕边缘发光、截图后闪一圈边框, 系统会当成全屏程序,
  // 桌宠一藏, 用户就点不到"停"、看不到提示条了
  bool controlling = false;
  ULONGLONG control_grace = 0;
  void set_controlling(bool on);

  PetApi* api = nullptr;  // 本机接口 (第一次打开时才建)
  bool api_on = false;
  void set_api(bool on);

  PanelWindow* panel = nullptr;  // 操作面板 (第一次打开时才建)
  void open_panel();
  WorkbenchWindow* workbench = nullptr;  // 工作台 (第一次打开时才建)
  void open_workbench();
  // 小屋模式: 桌宠窗口下半部分是一小块 3D 空间, 她在「抹茶樱花」小筑居里生活 (页面带 room=1, 见 web/room-life.js).
  // 侧菜单的小房子、托盘里切换, 记在设置里 (pet.room). 只有 3D 门面有
  bool room_mode = false;
  void set_room_mode(bool on);
  // 看你在做什么 (activity.h): 默认关, 托盘里打开, 记在设置里 (pet.activity)
  ActivityWatch* activity = nullptr;
  bool activity_on = false;
  void set_activity(bool on);
  void send_profile();  // 把当前人设 (名字、人设文字、模型、skill) 告诉页面, 页面再转给服务端; 顺带刷新工具表
  // QQ 渠道 (web/qq.js, 服务端 wm/channels): 设置里的 qq 段转成 channel_config 交给页面; 页面报回的最后一份状态给面板看
  void send_channel();
  void set_qq_paused(bool on);
  std::string qq_status;  // 页面报回的 channel_status (JSON, 不带 token 和 key)
  ToolHub* tools = nullptr;  // 替云端模型调工具 (tools.h)
  std::string face = "vrm";  // 页面现在加载的门面: vrm (3D) 或 live2d (2D), 跟着当前人设预设走
  std::wstring page_query() const;  // 页面地址的查询参数: --query 加上门面

  std::thread selftest;
  bool selftest_ok = false;
};

void run_selftest(App& app);
std::string run_ledge_test(App& app);  // 自检: 站上窗口顶边、跟着窗口走、窗口没了往下掉
