# 工作台原生网页面板

此实现把第三方网页放在独立 WebView2 控件中。网页按顶层文档正常加载，不使用 iframe 或移除网站的 `frame-ancestors` 限制。工作台界面和网页使用不同的 WebView2 环境、用户数据目录；网页不加载桌宠脚本、文件代理或设置桥接。

## 宿主接口

可信工作台通过 `chrome.webview.postMessage` 发送 `workbench ` 前缀的 JSON：

```json
{"op":"browser","action":"navigate","tab_id":"web-example","url":"https://example.com/","bounds":{"x":400,"y":80,"width":800,"height":600,"scale":1.25},"visible":true}
```

动作包括 `navigate`、`back`、`forward`、`reload`、`stop`、`bounds`、`hide`、`focus`、`state`、`close`、`external`、`zoom` 和 `selection`。各标签由 `tab_id` 标识，分别拥有控制器与浏览历史。`external` 经地址检查后调用系统默认浏览器，不创建另一个具有宿主权限的 WebView。`bounds` 使用 CSS 视口坐标与 `devicePixelRatio`，宿主裁剪到客户区；隐藏保留浏览历史，关闭释放指定标签的控制器。无 `tab_id` 的 `hide` 隐藏所有网页，以免覆盖对话框或设置。

宿主回传：

```json
{"t":"workbench-browser","tab_id":"web-example","state":"ready","url":"https://example.com/","title":"Example","can_back":false,"can_forward":false,"zoom_factor":1,"error":"","visible":true}
```

状态包括 `starting`、`loading`、`ready`、`error` 和 `closed`。候选宿主能力声明为版本 3，包含 `native_browser`、`browser_tabs`、`browser_zoom`、`browser_selection`、`reveal_file`；旧的文件夹选择等版本 1 能力仍保留。只有 `navigate` 创建浏览器控制器。

前端逐项检查能力。版本 2 宿主只显示当前单标签，保留其他已保存标签供升级后恢复，不发送不支持的选区或缩放消息。普通浏览器没有原生能力时使用文字阅读。晚到的能力声明会接管尚未主动切换为文字阅读的页面。

`zoom` 使用 `factor`，仅缩放网页。主工作台 WebView 固定 100%，禁用浏览器快捷键缩放和捏合，Windows DPI 不受影响。`selection` 携带 `request_id`；回包 `workbench-browser-selection` 包含请求身份、标签、标题、URL 与最多 16,000 字符的选区。前端保存点击时的会话身份，将选区作为文字快照加入材料，不自动发送聊天。

文件区使用独立的 `op: reveal_file`，携带 `rid`、`workspace` 和 `path`。宿主解析真实路径并检查包含关系与存在性，成功后定位系统资源管理器；回包为 `workbench-file-revealed`。它不接受任意命令。

原生控件显示在 HTML 之上。设置页、任务画布、弹窗及菜单打开时，前端必须发送 `hide`；关闭后重新发送 `bounds` 和 `visible:true`，无需重新导航。

## 边界与实现

- 浏览器数据在宿主目录旁的 `browser-data`，不与工作台的登录、缓存和宿主权限混用。
- 禁用 `IsWebMessageEnabled` 和 `AreHostObjectsAllowed`，无宿主消息处理器，也不预载应用脚本。仅在用户点击引用时读取网页选区。即使网页看见 `chrome.webview` 对象，消息也不会交付宿主。
- 地址仅允许 HTTP/HTTPS，拒绝文件、脚本、数据 URL、凭据 URL 和应用服务端口。应用服务端口按端口整体隔离，避免数字回环 IP、DNS 别名等写法绕过。
- 使用 `ICoreWebView2_22` 的全部请求来源过滤，覆盖 iframe、共享工作线程与 Service Worker；运行时太旧时显示明确错误。
- 回归测试发现，仅取消 `NavigationStarting` 会发生“界面阻止但目标服务已经收到 302 请求”的情况。现在同时使用 Chromium `Fetch` 请求和响应阶段拦截，在跟随 `Location` 前验证跳转目的地址。首次导航等待隔离配置完成。
- 工作台自身的 WebView 只允许导航到可信工作台页面，其消息来源也必须精确匹配该页面；外站不能导航进有权限的宿主控件。
- 服务端自身仍需保留 Host/Origin 验证，尤其是 WebSocket。浏览器隔离不是替代服务端认证的机制。
- 用户点击的新窗口链接在当前隔离标签导航，自动弹窗被拒绝。网页设备权限和自动下载暂不提供；下载可明确选择系统浏览器。各标签独立页面状态，共用独立于应用宿主的浏览器 profile。
- 初始化失败可再次导航重试；初始化尚未完成时关闭，不会被迟到回调重新打开。

## 验证

本地脚本 `tools/test_native_browser.py --out <独立证据目录>` 编译一个隐藏原生窗口测试程序，使用临时 WebView2 profile 和两个临时 HTTP 服务。它不启动、重启或操作正式桌宠，不读取用户浏览器数据。

原版本安全回归报告：`E:/WorldModel-terminal-browser-20261031/native-browser-11/native-result.json`。17 项检查通过，保护服务收到 0 次请求，包括真实网页桥接调用、iframe/fetch/页面 302、实际 Service Worker 请求及重定向、自动弹窗、前后导航、隐藏、关闭和失败后重试。`frame-ancestors 'none'` 的测试页面也可正常显示。新版再次完成 17 项隔离验证，并新增实际 WorkbenchWindow 的 20 项联调及强制延迟握手的 21 项验证，证据位于 `E:/WorldModel-liuli-workspace-20261031/browser`。

历史版本 2 已于 2026-11-01 03:34–03:35 UTC 正常发布，见 [原发布记录](workbench-terminal-browser-20261031.md)。版本 3 已通过 `desktop/scripts/build.ps1 -NoPublish` 编译，**尚未替换正式宿主或重启后端**：当前并行桌宠动作变更未验收，且存在两个正式宿主进程。当前发布状态见 `E:/WorldModel-liuli-workspace-20261031/browser/production-readiness.json`，交付说明见 [文件、网页与共享外观更新](workbench-liuli-workspace-20261101.md)。历史发布不代表新版已上线。

实现参考：[WebView2 安全指南](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security)、[用户数据目录](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/user-data-folder)、[请求过滤及来源](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/webresourcerequested)、[Chromium Fetch 协议](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
