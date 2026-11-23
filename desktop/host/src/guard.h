// 操控电脑时的守门 (设计见 docs/roadmap/desktop-pet.md 的"玩电脑 = 远程操控电脑和手机").
// 认得的操控类 MCP 服务器 (现在只有 Windows-MCP, 设置里 kind = "windows-mcp") 的工具分四档:
//   read 只读 (不读屏幕内容、没副作用, 自动) / screen 读屏 (屏幕上的文字会交给云端) /
//   act 动手 (点、打字、按键、切窗口) / power 高风险 (执行命令、文件、注册表、进程、剪贴板、抓网页、通知: 每次都问用户,
//   卡片上写着要做什么, 没有"这一轮都允许"、QQ 上主人说了也不算; 删文件、装软件、改注册表和系统设置、关机、受保护的路径这类先拦掉;
//   设置里 guard.power=false 可以整档关掉) / locked 锁定 (表里没有的、新版本加的工具, 看清楚再放开).
// 动手之前看目标: 鼠标落点的窗口和程序, UI 自动化读到的那个元素的名字、是不是密码框; 键盘看前台窗口.
// 碰到付款、发消息、删文件、改系统设置、输密码、同意授权、命令行、桌宠自己的窗口、QQ 机器人的后台 (SnowLuma、NapCat、AstrBot),
// 不问, 直接拦, 回模型"这一步要用户自己来". 屏幕上开着机器人的控制台时连读屏也不行 (网页里有账号和 token).
// 名单和按钮名字是尽力而为 (自己画界面的程序、游戏、图片按钮读不到名字), 主要还是靠每次确认、看得见和一键停.
#pragma once
#include <string>

#include "json.h"

// 上述是默认限制。autonomy::guard_config 的 _unrestricted=true 只保留用户额外名单与协议校验；
// 完全授权的确认/停止/统一黑名单由调用层处理，不提供任意脚本隔离。
namespace guard {

enum class Tier { none, read, screen, act, power, locked };  // none: 不是认得的操控类服务器 (照原来的权限办)
Tier tier(const std::string& kind, const std::string& tool);
const char* tier_name(Tier t);  // read / screen / act / power / locked, none 是 ""
bool power_on(const Json& guard_cfg);  // 设置里 guard.power (默认开): 高风险档的工具交不交给模型
// 起服务器的参数: Windows-MCP 的 --tools 名单按 power_on 加上或去掉高风险档的 7 个 (面板里存的参数是只有 12 个的老名单)
Json args(const std::string& kind, const Json& args, bool power);
// unrestricted 保留 App 原参数与模式，仍删除 label/labels (定位名单目标需要坐标) 和不支持的 sampling。
// 交给模型的参数结构: 去掉按编号点的 label / labels (守门要知道点在哪, 只收坐标) 和 App 按路径启动 (等于执行任意命令)
Json schema(const std::string& kind, const std::string& tool, Json schema, bool unrestricted = false);
// 工具说明前面加的一句 (没有时为空): Snapshot 的网页模式只读前台的浏览器, 不在前台要先切过去
std::string hint(const std::string& kind, const std::string& tool, bool unrestricted = false);
// 起服务器时一定加上的环境变量: 关掉匿名统计; 聊天、密码、命令行软件和设置里"另外不碰的程序" (extra.processes) 的窗口不进元素树
Json env(const std::string& kind, const Json& extra = Json());
int connect_timeout_ms(const std::string& kind);  // Windows-MCP 起一次要 20 秒上下 (第一次 36 秒)
int discover_timeout_ms(const std::string& kind);  // 新版探测也要等它起来 (见 McpClient::connect)

struct Verdict {
  bool ok = true;
  std::string why;  // 拦下时: 给模型和用户看的一句话
};
// extra._unrestricted 是调用层合成的完全授权标记；默认 false，不在 guard 中读取设置文件。
// 读屏和动手之前查一遍. extra: 设置里 guard 的补充名单 {"processes":["x.exe"], "words":["词"]}
Verdict check(const std::string& kind, const std::string& tool, const Json& args, const Json& extra);
// Windows-MCP 在调用开始前就拒了 (什么都没做), 稍等可以再试: 返回等多少毫秒, 不用再试时返回 0.
//   CONTROL_UNAVAILABLE (刚截完图、指示框暂停、正在换钩子): 等 0.7 秒;
//   USER_CONTROL (你刚动过鼠标键盘, 它要等你停手 10 秒): 等它说的秒数, 超过 12 秒就不等了, 照原样告诉模型
int busy_wait_ms(const std::string& kind, const Json& result);
// 读屏结果里的文字: 不碰的窗口 (聊天、邮件、密码管理器) 的标题换成占位. 结果交给云端之前过一遍
std::string redact(const std::string& text, const Json& extra);
// 屏幕上 (没最小化、不在别的虚拟桌面的窗口里) 开着聊天、邮件、密码管理器、付款页面、机器人后台、"不碰"名单里的程序吗:
// 开着就返回原因 (这时的截图不该发去别处, 比如 QQ 任务做完发给主人的那张), 没有返回 nullptr. 桌宠自己的窗口不算.
// who 不为空时写上是哪个程序 (只有程序名, 不带窗口标题; 给宿主日志核对误判用).
const char* screen_private(const Json& extra, std::string* who = nullptr);

}  // namespace guard
