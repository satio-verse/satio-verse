# SnowLuma 随桌宠启动

QQ 之前只有 OneBot 连接与重连逻辑，没有负责启动 SnowLuma 的代码。现在“设置 → 外部渠道 → 连接与模型 → 随桌宠启动”可以启动用户已经安装的 SnowLuma；取消开关后仍可连接手动启动的协议端。

## 使用条件

- QQ 已启用，SnowLuma 目录指向完整安装目录（含 `node.exe`、`index.mjs`）。
- 首次运行、条款选择和账号登录在 SnowLuma 中完成。桌宠不会代填同意标记。
- 已有协议端口或对应 SnowLuma 进程时直接复用，不会重复启动。
- 使用安装目录自带的 Node，避免 `launcher.bat` 取到 PATH 上不兼容的 Node。
- 后台启动不会弹出额外终端。关闭桌宠或关闭自动启动开关，不会杀掉 SnowLuma 和 QQ。

## 状态含义

启动管理与 QQ 登录分别显示。看到“后台启动”说明进程已创建；看到“等待 QQ 登录或挂接”说明 SnowLuma 进程在运行但 OneBot 端口尚未就绪；只有账号核对及心跳通过后，QQ 才显示在线。

当前本机 SnowLuma 的 `hookAutoLoad` 为 `false`。这意味着重启 SnowLuma 后，QQ 账号可能仍需在它的控制台挂接一次。上游的 `SNOWLUMA_HOOK_AUTOLOAD=1` 会挂接所有发现的 QQ 进程，不能精确对应桌宠设置的账号，因此本次没有自动设置它，也没有改变现有 SnowLuma 配置。真正做到指定账号无操作恢复，需要单独接入按账号挂接的接口。

**2026-11-07 更新：已接入。** 保管箱里存了 SnowLuma 管理页的密码后，程序登录管理页、只对配置里那个 QQ 号点“挂接”，崩了自己重启、QQ 要扫码时把二维码给用户，见 `docs/account-vault-qq-auto-20261107.md`。

这次是对已安装程序的启动集成，没有把第三方协议程序放进桌宠安装包。桌宠服务不会向它传递模型 API 密钥、Node 预加载参数或自动接受条款的环境变量。

## 实现与验证

- `wm/channels/snowluma.py`：异步检查、进程复用、跨进程启动锁、失败退避和状态信息。
- `wm/channels/onebot.py`：重连前启动钩子；既有账号核对和心跳继续生效。
- `wm/channels/manager.py`：生命周期与状态接线。
- `desktop/web/panel-qq.js`、`desktop/host/src/settings.cpp`：开关保存和透传。
- `config/default.json`：`channels.qq.snowluma_autostart` 默认开启，QQ 的 `enabled` 默认仍关闭。

`tests/test_snowluma_startup.py` 15 项通过；合并 QQ 与自主渠道回归共 106 项通过，1 项既有依赖弃用警告。Python 编译检查和页面 JS 语法检查通过。测试只使用临时文件、替身进程和本机临时端口；没有启动真实 SnowLuma、登录 QQ、发送消息或调用云端模型。正式原生设置透传需要统一重编、刷新宿主后生效。
