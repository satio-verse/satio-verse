# 社区、数据贡献与应用更新

这些功能使用 `wm/platform_services.py`，与模型供应商 API 的数据政策独立。客户端没有内置公共社区或接收地址，也没有自动上传历史记录。

## 数据贡献

诊断统计与训练对话分开同意，默认关闭。流程为：预览当前会话或指定回复 → 检查脱敏内容 → 加入待上传 → 明确上传。保存选择不触发上传。训练候选只包含用户/助手正文与反馈，不含附件、工具输出、系统提示。自动脱敏只是辅助，不能识别所有个人信息。

关闭某项同意会清除对应待传正文。成功上传后本地只保留收据；远程删除需要收据中的删除令牌。服务当前不执行训练，因此可以删除原始数据；如果以后增加训练流水线，必须另建用途版本、保留周期、数据集血缘和删除传播机制，不能声称删除数据即撤销模型训练。

## 部署共享社区与接收服务

`tools/community_server.py` 提供真实 HTTP 服务：公开索引 `/index.json`、插件包 `/packages/<file>`、鉴权发布 `POST /packages`、接收 `/contributions`、带删除令牌撤回 `DELETE /contributions/<id>`。不执行上传代码。

1. 在服务器准备持久化目录和 `authors.json`，内容为 `SHA256(随机作者令牌)` → 稳定作者 ID。客户端作者名最终会由服务验证的作者 ID 替换；其他作者不能抢占既有插件 ID。已发布版本不可覆盖。
2. 将 `WM_RECEIPT_SECRET` 设为至少 32 字节随机密钥，保存到服务器 secret 管理，不写仓库。更换它会使已有删除令牌失效，因此必须安全持久化。
3. 运行：`python tools/community_server.py --home <目录> --public-url https://community.example.com --authors <authors.json> --port 8792`。它仅监听本机；在前面配置 HTTPS 反向代理、请求速率限制和存储配额。示例域名不是上线地址。
4. 给每个作者单独发放令牌；用户在工作台配置 `https://community.example.com/index.json` 后可浏览其他人发布的真实插件。公开发布会上传包内所有文件，因此页面在发布前列出文件清单。没有配置服务时只导出本地包，不显示“公开成功”。
5. 数据贡献接收地址填写相同 HTTPS 根地址；生产部署应补充真实的维护者信息、隐私说明、保留期限、滥用处理、监控备份策略与服务级鉴权。示例 collector 是可测试的最小服务，不等于已上线运营的平台。

现有 Creator 格式支持 SKILL.md、stdio MCP 和隔离 HTML 界面。远程导入先生成关闭的草稿，用户在我的扩展页检查启用。此实现不把 npm/DSH 插件伪装成兼容 WorldModel；大型模型和语音组件仍走原组件下载管理。

## 更新与回滚

四层更新应区别处理：

| 内容 | 更新方式 |
|---|---|
| HTML/CSS/JS 等工作台界面 | 可信清单校验，空闲时原子替换文件，重新打开工作台 |
| Creator 插件 | 独立不可变版本，检查后启用，可切回旧版 |
| 模型/语音组件 | 原组件管理器按固定文件哈希下载，不塞入应用小补丁 |
| Python 后端 / Windows EXE、DLL | 下载并校验暂存，停止程序后离线替换，再启动健康检查 |

构建小型更新包：

```text
python tools/release_bundle.py build --root G:/RunProgram/WorldModel_v1 --version 0.2.0 --output release.json desktop/web/workbench-platform.js desktop/web/workbench-platform.css
```

工具输出 `trusted_manifest_sha256`。用户/发布运维从独立渠道取得此哈希，在更新来源中配置；不能把包里自带的哈希当成签名。也支持对不含 `signature` 的规范 JSON 做 Ed25519 签名：UTF-8、键排序、无额外空白，签名和公钥使用 Base64。签名验证依赖 `cryptography`；缺依赖时明确失败，不降级为不校验。

客户端限制文件目标范围、大小、重复路径、路径越界，下载连接固定公网 DNS 地址且不跟随跳转。每个文件验证 SHA256，应用前再次验证整个清单。更新写入恢复日志和原文件备份，出错会回滚。回滚前校验现文件哈希，避免覆盖用户后来修改的文件。未完成事务可由回滚工具恢复。

主程序/后端更新会返回“需要退出后更新”，不会停止任务或覆盖运行中的 EXE。退出桌宠及后端后运行：

```text
python tools/release_bundle.py apply --root <安装目录> --home <agent 状态目录> --processes-stopped
python tools/release_bundle.py rollback --root <安装目录> --home <agent 状态目录> --processes-stopped
```

`--processes-stopped` 是运维对已经停止进程的声明；工具不负责强杀进程。Windows 文件锁仍会触发事务回滚。上线版安装器应在外部 updater 进程增加进程退出等待、重启、健康探测、失败自动恢复、代码签名和分批发布。本轮没有把这些尚未接入宿主的步骤标记为全自动热更新。

当前补丁上限为 16 MiB，适用于界面与小型应用代码；此通道不承担大组件下载。所有修改与服务操作应先在隔离测试目录验证。

## 可交付部署包与从零启动

交付物为 `platform-deployable.zip`，仅包含服务、发布工具、必要的 Python 模块及本说明；不包含聊天、账户、密钥、模型、数据库或桌宠程序。解压后所有命令从解压目录执行。它部署的是社区服务，现有桌宠仍是独立客户端。

当前没有服务器时，可以先在本机做启动检查、导出插件包并通过本地索引共享；不要把本机运行误认为已经公开。真正让不同电脑的用户看到同一社区，需要一台持续在线的服务器、解析到它的域名和 HTTPS 证书。

最低运行环境：Python 3.10 或更高版本，以及 `deploy/requirements-platform.txt`。已用现有 Python 3.10.2 / aiohttp 3.14.4 验证本包；无需模型 API Key、GPU 或下载桌宠组件。

Windows PowerShell 示例：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r deploy/requirements-platform.txt
& .\.venv\Scripts\python.exe tools/platform_admin.py --private-dir 'D:\WorldModelPlatform\private' --author ayuki
& .\deploy\start-platform.ps1 -DataRoot 'D:\WorldModelPlatform' -PublicUrl 'https://community.example.com' -Python '.\.venv\Scripts\python.exe'
```

Linux 示例：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r deploy/requirements-platform.txt
.venv/bin/python tools/platform_admin.py --private-dir /srv/worldmodel-data/private --author ayuki
sh deploy/start-platform.sh /srv/worldmodel-data https://community.example.com .venv/bin/python
```

这里的域名和数据目录是示例，必须换成自己的。启动脚本在前台运行，关闭终端会停止服务；持续运行请交给系统服务管理器，并使用同一个受限系统用户和持久化数据目录。不要把工作目录置于网站的公开静态目录中。

`platform_admin.py` 会生成作者令牌、哈希到作者 ID 的映射，以及单独的删除回执密钥。它只打印保存位置，不输出密钥。作者 ID 使用英文、数字、下划线或短横线。将 `private/<作者>-token.txt` 中的令牌只交给对应作者；其他作者不能发布同 ID 插件。给新作者重复执行初始化命令，然后重启服务加载新的映射。已有令牌不会覆盖；轮换令牌需要运维修改映射并重启，不应改变插件 `owners` 归属记录。

Windows 上请将 `private` 目录的 ACL 设置为仅运行服务的账户和管理员可读。Linux 初始化工具会设置目录 0700、凭据文件 0600。`receipt-secret.txt` 必须持久保留；不要每次启动重新生成，否则用户已有删除回执会失效。

数据布局：

```text
<数据目录>/
  private/                 # 非公开：作者令牌、authors.json、receipt-secret.txt
  store/packages/          # 对外可获取的不可变插件包
  store/owners/            # 插件 ID 与已验证作者的归属
  store/validation/        # 静态校验用 Creator 文件；不会执行
  store/contributions/     # 非公开：用户明确贡献的训练候选或统计
  store/deleted/           # 删除墓碑（编号与时间，无正文）
```

服务只监听 `127.0.0.1:8792`。本机检查 `http://127.0.0.1:8792/index.json` 应返回 `{"worldmodel_index":1,"packages":[]}`；作者尚未发布时为空列表是正常状态。部署包提供 `deploy/nginx-platform.conf.example`，在服务器上替换域名、证书路径和发布目录，经过 nginx 配置检查后启用。不要把 `private` 或 `store/contributions` 设置为静态文件目录。请给数据磁盘配置配额；示例反向代理已有请求体与频率限制，仍需要根据实际使用量设置保留周期和告警。

客户端填写：

| 客户端位置 | 值 |
|---|---|
| 社区索引 | `https://自己的域名/index.json` |
| 公开发布服务 | `https://自己的域名`，再输入该作者的令牌 |
| 数据贡献接收服务 | `https://自己的域名`，另行勾选诊断或训练同意 |

客户端远程连接必须是公网 HTTPS，拒绝内网/回环地址并不跟随跳转。因此本机 HTTP 启动检查用于运维验证，不能直接填作客户端远程服务。无公网时请使用导出的本地插件索引；贡献保留在本机待传。

接收服务目前允许无需作者账号的贡献上传，以随机贡献凭据验证重试、凭删除回执撤回；“作者发布令牌”只用于发布插件，不是用户登录系统。本包没有注册、支付、审核队列、运营后台，也不会使用收到的数据训练模型。训练数据收集与实际训练是两个独立阶段。

备份应同时保存 `store` 和回执密钥，访问权限与原数据相同。恢复备份前，须保留最新 `deleted` 墓碑，并删除恢复出来、但已在墓碑中撤回的正文，避免让删除的数据重新出现。不能将完整备份公开上传到插件索引。

## 可选签名的具体配置

默认固定清单 SHA256 路径不需要额外依赖，已完成端到端暂存、应用、回滚验证。若使用签名，发布端和执行验证的桌宠 Python 环境都需要额外安装 `deploy/requirements-signatures.txt`。本次已使用项目原有 Python 3.12.10 / cryptography 49.0.0，在临时目录验证密钥生成、签名、验签暂存、应用、回滚、篡改拒绝及拒绝覆盖密钥（7 项通过）；没有修改正式桌宠 Python 3.10 的依赖。正式运行时尚未安装 cryptography，选择签名更新前仍须为实际执行验证的 Python 安装这项依赖。

```text
python -m pip install -r deploy/requirements-signatures.txt
python tools/sign_release.py keygen --private-key <安全目录>/release-private.txt --public-key release-public.txt
python tools/sign_release.py sign --private-key <安全目录>/release-private.txt --input release.json --output release-signed.json
```

私钥不进社区包或更新目录，只在发布者的安全目录保管。客户端“更新来源与信任”里填写更新 JSON 的 URL，并粘贴 `release-public.txt` 中的 Base64 公钥；签名值由工具写在包中。公钥须通过独立渠道预先取得。更新来源中的固定 SHA256 若不再使用应清空，避免继续接受之前已固定的旧清单。

离线 `apply` / `rollback` 的 `--home` 是桌宠的 **agent 状态目录**，不是社区服务器的数据目录。默认位置为当前用户的 `.worldmodel/agent`；若设置过 `agent.home`，以该配置为准。客户端先检查并暂存更新，退出桌宠及后端，再用部署包中的更新 CLI 指向实际安装目录与该 agent 状态目录执行。

包内完整性清单 `CONTENTS.sha256` 可以核对打包内容；压缩包本身的 SHA256 由交付验证报告提供。它们只证明内容完整性，不取代来自可信发布方的签名或独立哈希验证。
