// 看你在做什么 (像《妹居物语》那样: 她知道你在玩游戏、看视频、写代码, 给点反应和关心). 默认关, 托盘里打开 (记在设置里 pet.activity).
// 只在本机: 每 3 秒看一次前台窗口的程序名和标题, 归成一类; 5 分钟没动键盘鼠标算"走开了". 不截图, 不读窗口里的内容.
//   - 标题: QQ、微信这类聊天软件不取 (用户要的), 无痕 / InPrivate 窗口、密码管理器、文件管理器也不取; 留下的去掉控制字符, 最多 40 个字.
//   - 变了才告诉页面: {"t":"activity","on":true,"category","app","title"}. 页面决定她怎么反应、告诉服务端多少 (见 web/activity.js).
//   - 日志里不记标题.
#pragma once
#include <windows.h>

#include <string>

class App;

struct ActivityInfo {
  std::string category;  // game / chat / video / music / browser / code / office / files / desktop / other / away
  std::string app;       // 给人看的名字: 游戏名 (Steam 的游戏取安装目录名)、"Chrome"、"微信"...
  std::string title;     // 窗口标题 (处理过的), 不取时为空
  bool operator==(const ActivityInfo& o) const { return category == o.category && app == o.app && title == o.title; }
};

// 程序路径 + 窗口标题 -> 归类 (纯函数, 自检直接调)
ActivityInfo classify_activity(const std::wstring& exe_path, const std::wstring& title);

class ActivityWatch {
 public:
  explicit ActivityWatch(App& app) : app_(app) {}
  void tick();   // UI 线程, 3 秒一次
  void reset();  // 打开时: 下一拍一定发一次
 private:
  App& app_;
  ActivityInfo last_;
  bool sent_ = false;
};

// wmpet.exe --activity-test <cases.json>: [{"exe": 路径, "title": 标题}...] 逐个归类, 结果 (JSON) 写到 stdout. 不看真实的窗口
int run_activity_test(const std::wstring& cases_path);
