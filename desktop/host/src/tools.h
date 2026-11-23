// 桌宠替云端模型调工具 (服务端下发 tool_call, 协议见根目录 README 的"工具调用"):
//   当前人设预设启用的 MCP 服务器的工具 (宿主是 MCP 客户端, 连接常驻), 加上 load_skill (读 skill 的 SKILL.md 全文),
//   和桌宠自带的工具 (提醒、小屋动作、打开网址, 见 builtins.h; 每个预设选启用哪几个).
// 工具在模型那里叫 mcp__<服务器名>__<工具名> (和 Claude Code、dsh 的叫法一样). 权限按操作面板里定的:
//   自动 (直接调) / 每次确认 (在对话栏里问用户: 允许一次、总是允许、拒绝, 30 秒不回答算拒绝) / 禁用 (根本不交给模型).
// 操控电脑的服务器 (设置里带 kind, 现在只认 Windows-MCP) 另有四档和守门 (guard.h):
//   读屏、动手每次确认, 卡片上是"允许一次 / 这一轮都允许 / 拒绝" (不给"总是允许"); 锁定的工具不交给模型;
//   动手之前守门看目标, 付款、发消息、删文件、改系统设置这类地方直接拦; 一轮最多 30 次、5 分钟.
//   一键停 (对话栏的"停"、全局快捷键、真人动了鼠标键盘、托盘"断开"): 停掉在调的, 作废"这一轮都允许", 让页面打断这一轮.
// 工具结果里的图片 (截图) 不交给服务端, 换成一句"只留在本机"; 操控类服务器的文字结果里, 聊天和密码软件的窗口标题换成占位.
// 每次调用记一行审计日志 (设置目录下的 pet-tool-audit.jsonl): 时间、工具、参数摘要、谁决定的、成败、结果字数、耗时.
// 本地模型不接工具, 所以只在当前预设选了云端模型时才连 MCP 服务器.
#pragma once
#include <windows.h>

#include <atomic>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>

#include "builtins.h"
#include "autonomy.h"
#include "guard.h"
#include "json.h"
#include "mcp_client.h"

class App;

class ToolHub {
 public:
  explicit ToolHub(App& app) : app_(app) {}
  void refresh();                                                      // 页面就绪、设置保存后: 后台重连, 好了把工具表发给页面
  void on_call(const Json& call);                                      // UI 线程: 页面转来的 tool_call
  void on_answer(const std::string& call_id, const std::string& how);  // once | turn | always | deny | timeout
  void on_cancel(const std::string& call_id);                          // 服务端不等了 (超时、这一轮被打断): 在问的收起, 在调的停掉
  void on_stop(const std::string& call_id);                            // 用户在对话栏里点了"停"
  void on_turn_done();                                                 // 页面: 这一轮说完了 (或被打断了), "这一轮都允许"作废
  // 渠道任务单 (QQ 上主人说的事 / 她自己想看的, 见 wm/channels/manager.py): 页面在这一轮开始前 task_begin, 说完后 task_end.
  //   by = "owner": 主人亲口说了就算同意 -> 这一轮里的读屏、动手不再逐个问 (守门照旧在前面: 付款、发消息、删文件、装软件、
  //   登录、改系统设置照拦; 一键停、真人动鼠标键盘照停); by = "self": 她自己想看, 只放行读屏, 动手还是问.
  //   task_end 把这件事里最后一张截图 (若有, 且当时屏幕上没有聊天、密码这类窗口) 作为 task-shot 发回页面, 页面压小以后发到 QQ.
  void task_begin(const std::string& id, const std::string& by);
  void task_end(const std::string& id);
  void on_builtin_done(const std::string& call_id, bool ok, const std::string& text);  // 页面做完了自带工具 (room_do)
  void start();  // 页面就绪后: 起提醒的后台线程 (到点告诉页面)
  // 一键停操控电脑: 在问的收起、在调的停掉、"这一轮都允许"作废、这一轮之后的操控一律拒绝, 让页面打断这一轮
  void emergency_stop(const std::string& why);
  void disconnect();  // 托盘"断开电脑操控": 先停, 再在设置里停用所有操控类服务器
  bool has_control() const { return has_control_; }

 private:
  // 一个 MCP 服务器的连接. id、key、cfg、error、tools 建好之后就不再改 (配置变了就换一个新的 Server)
  struct Server {
    std::string id, key, kind;  // key: 怎么连的指纹, 变了才重连; kind: 操控类服务器的种类 (guard.h)
    Json cfg;
    std::mutex mu;        // McpClient 不是线程安全的: 连接、调用都在锁里 (一次调用可能要等一分钟, 别在 mu_ 里拿它)
    McpClient client;
    std::atomic<bool> connected{false};
    std::string error;
    Json tools = Json::array();  // 连上时列出来的工具
    std::string current;         // 正在调的那次的 call_id (在 ToolHub::mu_ 里读写)
  };
  struct Tool {
    std::string name, server_id, server_name, raw, title, kind, workspace;
    int generation = 0;
    bool read_only = false;
    bool builtin = false;  // 桌宠自带的 (builtins.h), 不经 MCP 服务器
    bool requires_unrestricted = false;  // 未分类电脑工具只在全局授权下提供
    guard::Tier tier = guard::Tier::none;
    bool control() const { return tier == guard::Tier::screen || tier == guard::Tier::act || tier == guard::Tier::power; }
    bool power() const { return tier == guard::Tier::power; }  // 高风险: 每次都问, 没有"这一轮都允许", QQ 任务单的授权也不算
  };
  struct Ask {
    Json call;
    Tool tool;
  };
  struct Grant {  // "这一轮都允许"
    std::string turn;
    ULONGLONG until = 0;
  };
  struct Task {  // 渠道任务单: id 空是没有; turn 是 task_begin 之后的第一轮 (只有这一轮算); until 到点作废
    std::string id, by, turn, shot, mime, skipped;
    ULONGLONG until = 0;
  };
  // 工作线程上调用, 结果发回页面. checked: 操控类的刚过了守门 (没等用户), 不用再看一遍
  void run(Tool tool, Json call, std::string how, bool checked = false);
  void dispatch(Tool tool, Json call);
  void finish_answer(Ask a, std::string how);
  void persist_answer(Ask a);
  void control_call(Tool tool, Json call);         // 工作线程: 操控类的调用先过上限、守门、"这一轮都允许", 再问用户
  autonomy::Call policy_call(const Tool& tool, const Json& call);
  std::string dispatch_error(const Tool& tool, const Json& call, const std::string& how, bool gui, autonomy::Policy* snapshot = nullptr);
  bool dispatch_ok(const Tool& tool, const Json& call, const std::string& how, bool gui, autonomy::Policy* snapshot = nullptr);
  void stash_shot(const Json& call, const Json& image);  // 渠道任务单里暂存最后一张截图 (见 task_end)
  void stop(const std::string& call_id, const std::string& who);  // 停掉在调的 (who: server | user | halt:<原因>)
  void ask_shot();                                  // 自检 (--ask-shot): 第一次弹确认框时截一张页面
  void builtin_run(Tool tool, Json call, std::string how);  // 自带的工具: 在宿主里做, 或交给页面 (room_do)
  struct Pending {  // 交给页面做、还没回的自带工具
    Json call;
    Tool tool;
    std::string how;
    ULONGLONG t0 = 0;
  };
  std::map<std::string, Pending> pending_;
  void skill(Json call);                             // load_skill
  void reply(const std::string& call_id, Json content, bool is_error);
  void audit(const Json& call, const Tool& tool, const std::string& how, bool ok, size_t chars, double ms, const std::string& error);
  std::string permission(const Tool& tool, bool unrestricted);  // 显式禁用优先；授权快照来自共享治理
  void update_control();                     // "在操作"的状态变了: 告诉页面 (提示条) 和宿主 (真人输入检测)
  void post(Json msg);                       // 发给页面 (哪个线程都行)

  App& app_;
  std::mutex mu_;
  std::map<std::string, std::shared_ptr<Server>> servers_;  // 服务器 id -> 连接
  std::map<std::string, Tool> tools_;                       // 模型那里的名字 -> 工具
  std::map<std::string, std::string> skills_;               // load_skill 能读的: 名字 -> 文件夹
  std::map<std::string, Ask> asks_;                         // 等用户回答的调用
  std::map<std::string, std::string> stopped_;              // 被停掉的调用 -> 谁停的 (server | user)
  std::map<std::string, std::shared_ptr<Server>> running_;  // 在调 (或在等这个服务器空出来) 的调用
  // 操控电脑
  std::map<std::string, Grant> grants_;    // 服务器 id -> 这一轮都允许
  std::set<std::string> in_flight_;  // includes policy fetch and approval waits
  std::set<std::string> control_calls_;    // 正在守门、问、调的操控类调用
  int control_running_ = 0;                // 正在调的操控类调用 (提示条、真人输入检测跟着它)
  Task task_;                              // 正在做的渠道任务单 (同时只有一张)
  bool task_active_locked(ULONGLONG now) const { return !task_.id.empty() && now < task_.until; }
  std::string halted_turn_, last_turn_;    // 喊停的那一轮 (之后这一轮的操控一律拒绝); 最近一次操控类调用的轮次
  std::map<std::string, std::pair<int, ULONGLONG>> turn_use_;  // 轮次 -> (操控类调用次数, 第一次的时间)
  bool control_on_ = false;                // 上一次告诉页面的"在操作"
  bool stop_on_input_ = true;              // 操控期间真人动了鼠标键盘就停 (面板里每个操控类服务器一个开关)
  std::atomic<bool> declared_unrestricted_{false}, policy_refresh_pending_{false};
  std::atomic<bool> has_control_{false};   // 当前工具表里有操控类服务器 (要不要注册全局快捷键)
  int generation_ = 0;
  std::atomic<bool> shot_taken_{false}, control_shot_{false};
};
