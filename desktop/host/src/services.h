// 桌宠管服务 (docs/roadmap/own-harness.md 第 9 节 A, 2026-10-26 用户: "能不能只下载一个桌宠就能实现所有功能呢?"):
// 带 --services 启动时在后台拉起 wm/services.py 的看守 (pythonw -m wm.services run, 已经有一个在跑它自己会退出);
// 看守按配置开服务端、本机模型、语音合成, 挂了重启, 状态写在 %USERPROFILE%\.worldmodel\services\state.json.
// 托盘"本地服务"子菜单读这个文件; 点一项 = 请看守重启那一样 (python -m wm.services restart <名字>).
// 退出桌宠默认不停服务 (QQ、微信接着收), "退出并停掉服务"才停.
#pragma once
#include <string>
#include <vector>

namespace services {

struct Item {
  std::string name, label, state, error;
  int port = 0;
};
struct State {
  bool running = false;  // 看守还活着
  std::vector<Item> items;
};

// root: 仓库根目录 (desktop/web 往上两级); python: 指定的 pythonw.exe (空 = 在 PATH 里找)
void configure(const std::wstring& root, const std::wstring& python);
bool found_python();
bool start();                                                       // 拉起看守
State read();                                                       // 读状态文件
bool command(const std::string& what, const std::string& name = "");  // restart / stop [名字]
std::wstring logs_dir();
std::wstring mark(const Item& it);  // "● 服务端 · 8790 · 在跑"

}  // namespace services
