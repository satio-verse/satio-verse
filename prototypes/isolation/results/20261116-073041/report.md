# 隔离原型测量结果

- 时间：2026-11-16T07:30:41-08:00（本机时区）；退出码 0
- Python：3.10.2（64 位）；Windows 版本号：10.0.26200
- 提权确认三项设置（只读）：EnableLUA=1，ConsentPromptBehaviorAdmin=0，PromptOnSecureDesktop=0
- 提权那一条：用户选了真试；结论：测法成立 (对照写了 ELEVATED); 三种关法都要不到
- 应用容器：没有一种配法起得来

## 结果表

| 关法 | Python 起得来 | 读假保管箱 | 读假文件 | 写外面(work) | 写外面(临时) | 写自己文件夹 | 连本机回环 | 要管理员权限 | 起一次(ms) | 结论 |
|---|---|---|---|---|---|---|---|---|---|---|
| 应用容器 | 做不到(203) | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 |  | 不能用: Python 起不来 |
| 受限令牌·只降级 | 做不到(0xC0000135) | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 |  | 不能用: Python 起不来 |
| 受限令牌·加限制SID | 做不到(87) | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 | 没验 |  | 不能用: Python 起不来 |
| 对照(不关) | 做得到 | 做得到 | 做得到 | 做得到 | 做得到 | 做得到 | 做得到 | 做得到 | 102 | 对照正常: 这几样平时都做得到 |

读、写外面、连、要管理员权限这几列，“做不到”才是想要的；“做得到”说明漏了。Python 起得来和写自己文件夹这两列反过来。对照那一行应当全是“做得到”。

## 每种关法的细节

### 应用容器
- 最后用的配置：（没起成功）
- 试过：配法=只按名字算出 SID, 没登记；起来了吗=没起来；错误=CreateProcessW 失败, 错误码 2
- 试过：配法=登记后再起；起来了吗=没起来；错误=CreateProcessW 失败, 错误码 203
- Python 起得来：做不到(203) -- Python 没起来 (见“试过”)
- 读假保管箱：没验 -- 没验: Python 没起来
- 读假文件：没验 -- 没验: Python 没起来
- 写外面(work)：没验 -- 没验: Python 没起来
- 写外面(临时)：没验 -- 没验: Python 没起来
- 写自己文件夹：没验 -- 没验: Python 没起来
- 连本机回环：没验 -- 没验: Python 没起来
- 要管理员权限：没验 -- 没验: Python 没起来

### 受限令牌·只降级
- 最后用的配置：（没起成功）
- 试过：配法=只降级, 不加限制 SID；创建进程用时ms=77.8；起来了吗=创建了进程，但没回话；没回话的原因=eof；退出码=0xC0000135；stderr=
- Python 起得来：做不到(0xC0000135) -- Python 没起来 (见“试过”)
- 读假保管箱：没验 -- 没验: Python 没起来
- 读假文件：没验 -- 没验: Python 没起来
- 写外面(work)：没验 -- 没验: Python 没起来
- 写外面(临时)：没验 -- 没验: Python 没起来
- 写自己文件夹：没验 -- 没验: Python 没起来
- 连本机回环：没验 -- 没验: Python 没起来
- 要管理员权限：没验 -- 没验: Python 没起来

### 受限令牌·加限制SID
- 最后用的配置：（没起成功）
- 试过：配法=严: 限制 SID = 沙箱 SID + ALL APPLICATION PACKAGES；起来了吗=没起来；错误=CreateRestrictedToken 失败, 错误码 87
- 试过：配法=松: 限制 SID = 沙箱 SID + Users + Everyone + RESTRICTED + 登录 SID；起来了吗=没起来；错误=CreateRestrictedToken 失败, 错误码 87
- Python 起得来：做不到(87) -- Python 没起来 (见“试过”)
- 读假保管箱：没验 -- 没验: Python 没起来
- 读假文件：没验 -- 没验: Python 没起来
- 写外面(work)：没验 -- 没验: Python 没起来
- 写外面(临时)：没验 -- 没验: Python 没起来
- 写自己文件夹：没验 -- 没验: Python 没起来
- 连本机回环：没验 -- 没验: Python 没起来
- 要管理员权限：没验 -- 没验: Python 没起来

### 对照(不关)
- 最后用的配置：不关
- 试过：配法=不关；创建进程用时ms=4.7；起来了吗=起来了；到第一句回话ms=102
- 探针读到的令牌信息：是不是提权的=否，提权类型=受限(未提权)，完整性级别=中，是不是应用容器=否，有没有限制SID(受限令牌)=否
- 要管理员权限：做得到 -- 拿到了管理员权限 (结果文件写着 ELEVATED)
- 收尾时核对：exited=True，active_in_job=0，exit_code=0

## 提权那一条

- 对照(不关)：请求返回值=1，请求错误码=0，结果文件=ELEVATED，拿到进程句柄=True，等它退出=exited，这次试出来的进程已经没有了=是，结果文件夹里多出的文件=只多了预期的那一个
- 生成的命令（探针原样用）：`C:\WINDOWS\System32\cmd.exe /d /s /c "C:\WINDOWS\System32\whoami.exe /groups | C:\WINDOWS\System32\findstr.exe /c:S-1-16-12288 >nul && (>G:\RunProgram\WorldModel_v1\prototypes\isolation\results\20261116-073041\elev_<关法>.txt echo ELEVATED) || (>G:\RunProgram\WorldModel_v1\prototypes\isolation\results\20261116-073041\elev_<关法>.txt echo NOT_ELEVATED)"`

## 每一步用时

| 步 | 名称 | 毫秒 |
|---|---|---|
| 1 | 自查 | 2 |
| 2 | 建东西 | 6561 |
| 3 | 登记沙箱身份、加权限 | 69 |
| 4 | 起探针 | 101200 |
| 5 | 提权那一条 | 0 |
| 6 | 收尾 | 195 |

## 权限里有谁（只读核对，已抹掉本人和别的账号）

- 临时文件夹(假保管箱所在)：<本人>、SYSTEM、Administrators
- work(假文件所在的上一级)：Administrators、SYSTEM、Users、Authenticated Users
- 假文件所在文件夹 work\decoy：Administrators、SYSTEM、Users、Authenticated Users
- work\pyrt：<沙箱SID>、Administrators、SYSTEM、Users、Authenticated Users
- work\home：<本人>、<沙箱SID>、Administrators、SYSTEM、Users、Authenticated Users

## 收尾核对

- 起过的探针个数：2
- 全部已退出：True
- 作业里没有剩下的进程：True
- 提权请求试出来的进程都没有了：是
- 删登记的沙箱身份：已删
- 登记表里还有这个沙箱身份吗：没有
- AppData\Local\Packages 下还有它的文件夹吗：没有
- 临时文件夹里的子目录：已删
- work 目录：已删
- 迟到出现的提权结果文件：没有
- 原型目录里多出来的东西：没有 (只剩源文件、tests 和 results)

个人信息扫描：没找到个人信息

