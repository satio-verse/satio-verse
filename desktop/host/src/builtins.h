// 桌宠自带的工具 (扩展中心 E1, 见 docs/roadmap/extensions.md 第 4 节): 和 load_skill 一样在宿主里实现, 不用下载, 只交给云端模型.
//   remind / cancel_reminder: 到点提醒你. 只在本机定时, 存在设置目录的 pet-reminders.json (重启后照样提醒, 关着时错过的打开后补上);
//     到点告诉页面, 她在对话栏里说、挥挥手, 小屋里走过来看你.
//   room_do: 让她在小屋里做某件事 (交给页面, room-life.js 的 requestAct 完成回执); 不在小屋模式时回"做不了".
//   open_url: 用默认浏览器打开一个网址. 每次确认 (没有"总是允许"); 只认 http / https, 本机和内网地址、付款页面、机器人控制台不开.
// 权限在设置的 builtins 段: {"remind": "auto|ask|off", "room_do": ..., "open_url": "ask|off"}, 没写的用默认 (提醒、小屋动作自动, 网址每次确认).
// 每个人设预设的 builtins 是启用了哪几组 (["remind", "room_do", "open_url"]); 没写 = 都启用.
#pragma once
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "json.h"

namespace builtins {

bool known(const std::string& name);                                     // 是不是自带的工具
std::string group(const std::string& name);                              // 权限、启用按组: cancel_reminder 跟 remind 一组
std::string title(const std::string& name);                              // 给人看的名字 (确认卡片、审计)
Json declare(const Json& settings, const Json& persona, bool unrestricted = false);                 // 交给模型的工具 (这个预设启用了的)
std::string permission(const Json& settings, const std::string& name);  // auto / ask / off
bool room_result_ok(bool transport_ok, const std::string& text);  // Only completed terminal JSON may succeed.
int room_timeout_ms(const Json& args);  // Bounded terminal wait; default 180 seconds.
Json options(const std::string& name);                                   // 确认卡片的按钮: 网址只有"允许一次 / 拒绝"
// 在宿主里就能做完的 (remind、cancel_reminder、open_url): 结果是给模型的一段文字. room_do 要页面做, 不在这里
bool run(const std::string& name, const Json& args, bool dry_open, std::string& text, bool unrestricted = false);
std::string check_url(const std::string& url);  // 不能开的原因; 能开时为空

// 提醒: 一个后台线程等最近的那个到点. 到点的从列表里拿掉, 交给 fire (在后台线程上调)
class Reminders {
 public:
  using Fire = std::function<void(const Json& item, double late_min)>;
  void start(Fire fire);  // 读文件、起线程 (只起一次)
  Json add(const std::string& text, const Json& in_minutes, const std::string& at, std::string& error);
  bool cancel(const std::string& id);
  Json list();  // [{id, text, due (本地时间 "YYYY-MM-DD HH:MM"), due_s}]

 private:
  void load();  // 第一次用到时读 (面板可能比桌宠页面先问)
  bool loaded_ = false;
  void save();  // 在 mu_ 里调
  void loop();
  std::mutex mu_;
  std::condition_variable cv_;
  Json items_ = Json::array();  // {id, text, due_s (time_t), created_s}
  int seq_ = 0;
  Fire fire_;
  bool started_ = false;
  std::thread thread_;
};
Reminders& reminders();
std::string local_time(double epoch_s);  // "10 月 25 日 08:00 (周六)"
std::string default_browser();           // 默认浏览器给人看的名字 (Firefox / Chrome / Edge), 认不出时是"浏览器"

}  // namespace builtins
