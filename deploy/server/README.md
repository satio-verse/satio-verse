# 服务器初始化与只读基线（草稿）

> **状态：草稿。** 2026-11-14 晚的情况：
> - 用户定了只用 `ubuntu`（控制台绑好的那把 RSA 密钥），不建专用的部署用户。
> - 已经连过服务器：钉了主机指纹、做了一次**只读基线**（结果在 `state/` 下，不进知识库）。
> - 加固脚本 `wm-harden.sh` 和验收脚本 `wm-verify.sh` **还没在服务器上跑过**，本机只做了 `bash -n` 和几段文本处理的单独测试。评审通过、你点头、拍好快照以后才上机；上线的必须是评审时记下 SHA-256 的那一份。
>
> 依据：知识库《服务器接入与部署方案（2026-11-14）》的"交接清单""新服务器加固清单""以后怎么干活"，以及两份核对笔记（括号里的 B2-b、D3-a 之类是笔记的条目编号）。
>
> 脚本和日志里的提示一律用英文 ASCII，这样不管终端用什么代码页都不会乱码；唯一的例外是快照脚本在 `sudo -n` 被拒时写的那一行，开头固定是"需要密码"。这份说明用中文。

## 1. 这个文件夹里有什么

| 文件 | 干什么 | 在哪 |
|---|---|---|
| `README.md` | 本文件 | 本机 |
| `wm-ssh.sh` | **对话连服务器的唯一入口**（Git Bash 用）。只认三种用法：`--script 本机脚本`、`-- 一条命令`、`--put`（把四个加固脚本传到服务器的 `~/wm-init`，会写服务器，要你点头）；ssh 的选项全写死在里面；主机指纹只认 `known_hosts`；每次调用记一行到 `state/ssh-calls.log` | 本机 |
| `known_hosts` | 只给 `wm-ssh.sh` 用的主机公钥：一行，ED25519，主机名那一栏是别名 `wm-server`，里面没有 IP | 本机 |
| `pin-hostkey.sh` | 写 `known_hosts` 的脚本：取服务器的 ED25519 公钥、算指纹、和你给的那串逐字比对，一样才写 | 本机 |
| `wm-snapshot.sh` | 状态快照。**不带参数 = 只往标准输出打印，服务器上不写任何东西**（只读基线，第 3 节）。加固以后装成 `/usr/local/sbin/wm-snapshot`，用 `--save 标签` 存到 `/var/lib/wm-ops/` | 先不上服务器 |
| `wm-snapshot-diff.sh` | 对比服务器上最近两份快照 | `/usr/local/sbin/wm-snapshot-diff` |
| `wm-harden.sh` | 加固脚本。`--check` 只看不改，`--apply` 才动手；能重复跑；每改一个文件先备份；日志写 `/var/log/wm-ops/harden.log` | 先放 `~/wm-init/`，再装成 `/usr/local/sbin/wm-harden` |
| `wm-verify.sh` | 只读验收：照方案页加固清单逐条打 PASS / FAIL | `/usr/local/sbin/wm-verify` |
| `packages.md` | 要装的软件清单：来源、大小、许可、为什么装、第几批 | 本机 |
| `state/` | 本机上的基线（`<时间>-baseline/`）和连接记录 `ssh-calls.log`。**这个文件夹不进知识库**：里面有这台服务器的软件包清单、用户名、各项配置和主机指纹，不是密钥，但是这台机器的细节。归档脚本（`archive_only_extra.py` 之类）不要扫它 | 本机 |

后四个服务器上用的脚本（`wm-harden.sh`、`wm-snapshot.sh`、`wm-snapshot-diff.sh`、`wm-verify.sh`）由 `wm-harden.sh --apply` 一起装进 `/usr/local/sbin/`，root 所有、别人不能改。

脚本**不碰**腾讯云自己的组件（`tat_agent`、云监控、主机安全），也**不改 SSH 端口**（要换端口见第 9 节）。

## 2. 身份：只用 `ubuntu`

登录用户是 `ubuntu`，用控制台已经绑好的那把 RSA 密钥（没有口令），别名 `wm-server` 在你自己的 ssh 配置里。**代价：对话里跑的任何命令都等于 root**（`ubuntu` 有免密 sudo），拿到那把私钥就等于拿到 root。所以"会改东西的命令先写成脚本、评审、你点头、改 ssh/防火墙/内核前先拍快照"这几条是硬规矩（方案页"以后怎么干活"）。

脚本的做法：`wm-harden.sh` 开头先查 `sudo -n true`，不通就**停下并说明原因**，不会去改任何 sudo 配置；通了就用 `sudo -n` 再执行自己。没有"临时给权限、事后收回"这类步骤。

### 官方文档核实（读取日期 2026-11-14）

| 编号 | 问题 | 结论 | 出处 | 状态 |
|---|---|---|---|---|
| 4a（补充 B1-LH-a） | 轻量的 Ubuntu 镜像上，控制台绑定的密钥装在哪个用户下 | 装在**绑定时选的用户**的 `~/.ssh/authorized_keys` 里。选"系统默认"时，Ubuntu 镜像就是 `ubuntu`（其余系统是 `root`）；也可以指定别的**已存在**的用户。**例外和要留意的**：①创建 Linux 轻量实例时官方还会默认绑 1 个密钥，对应用户 `lighthouse`、具备 root 权限，OrcaTerm 登录默认用它，所以除了 `ubuntu`，实例上还可能有 `lighthouse` 这个用户（这台机器上有，见第 3 节基线）；②离线绑定的步骤里没有选用户名这一步，离线绑定落在哪个用户，文档没写（**没核实**）。 | 《轻量应用服务器 管理密钥》`cloud.tencent.com/document/product/1207/44573`（页面标注更新 2026-09-23）；《使用 OrcaTerm 登录 Linux 实例》`cloud.tencent.com/document/product/1207/44642`（更新 2026-09-11） | 已核实（②没核实） |
| 4b（补充 D2） | `ubuntu` 用户是不是免密 sudo | 腾讯云官方页**没写**：云服务器《登录和连接实例问题》里 Ubuntu 用 `sudo passwd root` 开 root，没说要不要密码。cloud-init 官方文档的《Base configuration》里，Ubuntu 的示例配置把默认用户 `ubuntu` 写成 `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`，同时锁定密码；这页说的是"Ubuntu 上的 `cloud.cfg` 应该和它相似"，是示例，不是对腾讯云镜像的保证。 | cloud-init 文档《Base configuration》`docs.cloud-init.io/en/latest/reference/base_config_reference.html`（cloud-init 26.2 文档，页面没有更新日期）；《云服务器 登录和连接实例问题》`cloud.tencent.com/document/product/213/17278`（更新 2024-11-22） | 腾讯云页：没核实；cloud-init 示例：已核实 |

两条的**实机结果**（不是官方文档，是这台机器 11-14 的基线）：`ubuntu` 的 `authorized_keys` 有 1 条；`lighthouse` 用户存在但没有 `authorized_keys`；root 是 0 条。`sudo -n true` 成功；`/etc/sudoers.d/90-cloud-init-users` 和 `/etc/sudoers` 主文件末尾各有一行 `ubuntu … NOPASSWD`。

## 3. 第一件事：只读基线

目的：在改任何东西之前，把服务器现在的样子完整记下来。**这一步不往服务器写任何东西，也不装任何东西。**

在项目根目录的 **Git Bash** 里运行（不要用 PowerShell：它的 `<` 不可用，`>` 会改编码）：

```bash
D="deploy/server/state/$(date +%Y%m%d-%H%M)-baseline"
mkdir -p "$D"
bash deploy/server/wm-ssh.sh --script deploy/server/wm-snapshot.sh > "$D/baseline.raw" 2> "$D/stderr.txt"
```

`wm-snapshot.sh` 不带参数时：

- **只往标准输出打印**，一个主题一个块（`##### 10-os #####` 这样的行分隔，最后一行 `##### END (secret_hits=0) #####`；看不到 `END` 说明没跑完）。
- 要 root 才能看的（`sshd -T`、`ufw status`、`nft`、读 `/etc/sudoers.d/`、别的用户的 crontab 和 `authorized_keys` 等）一律 `sudo -n`。**`sudo -n` 被拒时，那一块里写一行"需要密码 …"，然后继续往下跑，不会停。**
- 全部在内存里处理，不主动建临时文件（不用 `mktemp`，也不用 here-document）；脚本是从标准输入交给 `bash -s` 的，所有工作都在 `main` 里、标准输入接 `/dev/null`，不会有命令把后半截脚本吃掉。

收的内容（块名在括号里）：系统和内核、几个关键软件的版本（10）；装了哪些包（20）；apt 源和第 0 批几个包的候选版本和来源（21）；正在跑 / 开机启动 / 失败的服务、定时器（30–33）；对外监听的端口，只记"协议、端口、绑定范围（any / loopback / specific）、进程名"（40）；ufw 和 nftables（50–51）；`sshd -T`、`sshd_config` 和 `sshd_config.d/` 每个文件的名字和有效行、`ssh.socket` 的状态（60–62）；nginx（70，没装就是一行说明）；crontab、`/etc/cron.d`（80）；`/etc/sudoers` 和 `/etc/sudoers.d/` 每个文件的名字和内容（81）；能登录的用户、几个要紧的组（82）；各用户 `authorized_keys` **只数条数**，另记家目录、`.ssh`、文件本身的权限（83）；服务器自己的主机密钥指纹（84）；云厂商代理：只记进程名、所属用户和服务名（85）；`/srv/worldmodel` 的目录和哈希（90–91）；`/usr/local/sbin/wm-*`（92）；journald、自动更新配置（93–94）；磁盘、内存、journal 大小（95）；fail2ban（96）；时间同步和 chrony 配置（97）；待更新的包数、有没有待重启、上次自动更新的记录（98）；过去 24 小时 ssh 登录失败的次数，**只要次数**（99）。

**绝不收**：`/etc/shadow`、任何私钥、`authorized_keys` 的内容、进程的环境变量、`/usr/local/qcloud/`、`/var/lib/cloud/`、`/etc/cloud/` 里的文件内容；公网 IP 和登录来源 IP（不跑 `ip addr`、`curl` 回显服务、`last`、`lastb`、`who`，不记已建立的连接，不列 fail2ban 封禁的地址）。防火墙、ssh、sudoers、cron、nginx、时间同步输出里长得像地址的文字会被换成 `<ipv4>`、`<ipv6>`（`0.0.0.0`、`127.x`、`::1` 保留）；`nginx -T` 和 cron 里长得像密码或令牌的行会被盖掉；每一块输出前还会检查一遍，出现私钥块、密码哈希或公钥正文就整块扣下、退出码 3。

**副作用**：登录和 `sudo` 自己写进系统日志的那些行不算。`nft list ruleset`（或 `iptables -S`）在内核还没加载 `nf_tables` 模块时会让内核自动加载它（这台机器上 ufw 服务开机启动，多半早已加载，没核实）。`apt list --upgradable` 用的是上次 `apt update` 留下的列表，**不跑 `apt update`**，并且关掉了 apt 的缓存文件，所以不写任何东西。

存下来以后（2026-11-14 的做法）：

1. 用 Python 扫一遍：`PRIVATE KEY`、`ssh-rsa` / `ssh-ed25519` 公钥正文、`password=` / `secret` / `token` 后面带值、密码哈希、40 位以上的 base64 串、所有区块里的 IP 地址（软件包清单不查，版本号长得像地址）。有就删掉那几行，并在 `meta.txt` 里说明。
2. 把 `baseline.raw` 改名成 `baseline.txt`，写 `meta.txt`（跑的时间、用的脚本和它的 SHA-256、退出码、扫描结果）。
3. 对照方案页写 `差异.md`。

**以后再记基线或做对比**：同样的命令，输出放新的 `<时间>-baseline/`；两份 `baseline.txt` 用普通的 `diff` 对（`95-disk` 到 `99-ssh-failures` 这几块天天变，会有差异）。服务器上的两份 `--save` 快照用 `wm-snapshot-diff`。**脚本改了，旧基线就不再对应当时的脚本**——`meta.txt` 里记着当时那一份的 SHA-256。

2026-11-14 的基线：`state/20261114-1815-baseline/`（`baseline.txt`、`meta.txt`、`差异.md`）。

## 4. 连服务器的规矩：`wm-ssh.sh`、`known_hosts`

**所有对话连服务器都只经 `wm-ssh.sh`。** 三种用法（Git Bash，在项目根目录）：

```bash
bash deploy/server/wm-ssh.sh --script <本机脚本文件>     # 脚本经标准输入交给服务器上的 bash -s
bash deploy/server/wm-ssh.sh -- <一条命令>               # 例如：-- uname -sr    -- cat /etc/os-release
bash deploy/server/wm-ssh.sh --put                       # 把四个加固脚本传到服务器的 ~/wm-init（写服务器，见第 5 节 ②）
```

- 带 `~` 或空格、管道的命令，整条用**单引号**包成一个参数：`-- 'bash ~/wm-init/wm-harden.sh --check'`，否则 `~` 会在本机被展开。命令的第一个词不能以 `-` 开头。
- 用法以外的东西一概拒绝（退出码 2），包括任何 ssh 选项（`-o`、`-i`、`-L` …）。`--put` 不收任何参数，只认固定的四个文件。
- 写死的设置：Windows 的 `ssh.exe`、别名 `wm-server`、不弹提示（`BatchMode`）、严格核对主机指纹、指纹文件只认 `known_hosts`（用户的和系统的指纹文件都指向它，你自己的 `~/.ssh/known_hosts` 不读、不改）、不更新主机密钥、不转发代理、清掉所有端口转发、连接超时 15 秒、保活 30 秒。脚本里没有任何关掉主机指纹检查的选项。
- 开头设了 `MSYS_NO_PATHCONV=1` 和 `MSYS2_ARG_CONV_EXCL='*'`，所以 `/etc/…` 这样的参数不会被 Git Bash 改写成本机路径；`known_hosts` 的路径用 `cygpath -w` 转成 Windows 形式。2026-11-14 试过：`-- uname -sr` 和 `-- cat /etc/os-release` 都对。
- 每次调用往 `state/ssh-calls.log` 追加一行（制表符分隔）：本机时间（带时区）、用法（`--script`、`--`、`--put` 或 `refused`）、命令原文（`--script` 记脚本路径加 SHA-256，`--put` 记四个文件的 SHA-256）、退出码。**不记输出。**
- **指纹对不上或连不上**（ssh 退出码 255）：打印"已停止：主机指纹对不上或连不上，不要改 known_hosts，报告组长"，原样返回 255。**不要自己改 `known_hosts`、不要换成别的 ssh 命令，报告组长。**已试过：换成一把不对的密钥，会得到 255 和这句话。

### `known_hosts` 是怎么来的

```bash
bash deploy/server/pin-hostkey.sh 'SHA256:<你在网页终端里核对过的 ED25519 主机指纹>'
```

它用 `ssh.exe -G wm-server` 取主机名和端口（只放在变量里，不打印、不落盘），交给 `ssh-keyscan` 取 ED25519 公钥（错误输出丢掉，因为里面有地址），把第一栏换成 `wm-server`，写到临时文件，用 `ssh-keygen -lf` 算指纹，**和你给的那串逐字比对，一样才改名成 `known_hosts`**，不一样就什么都不写。2026-11-14 的结果：一致。端口不是 22 时它会停下（非默认端口在 `known_hosts` 里要写成方括号形式，这个脚本不处理）。

Windows 自带的 `ssh-keyscan.exe`（OpenSSH_for_Windows 9.5）连 OpenSSH 9.6 的服务器会报 `unsupported KEX method sntrup761x25519-sha512`，取不到密钥；脚本这时改用 Git for Windows 自带的 `ssh-keyscan`（OpenSSH 9.9）。日常的 `ssh.exe` 不受影响。

### 你自己的 ssh 配置（只写在你的 `%USERPROFILE%\.ssh\config`，不写进这个文件夹）

```
Host wm-server
    HostName <服务器公网 IP>
    User ubuntu
    IdentityFile ~/.ssh/<那把私钥的文件名>
    IdentitiesOnly yes
    BatchMode yes
```

## 5. 上机跑加固（评审通过、你点头、拍好快照之后）

**① 记下评审过的那一份的 SHA-256**（项目根目录，PowerShell）：

```powershell
Get-FileHash -Algorithm SHA256 deploy\server\wm-*.sh | Format-Table Hash, Path -AutoSize
```

**② 上传（会写服务器，要你点头才跑）。**

```bash
bash deploy/server/wm-ssh.sh --put
```

`--put` 不收任何参数，只认固定的四个文件：`wm-harden.sh`、`wm-snapshot.sh`、`wm-snapshot-diff.sh`、`wm-verify.sh`，一律取 `deploy/server/` 下的那一份。它分三步，每一步都用 `wm-ssh.sh` 里那组钉死的选项（只认 `known_hosts`）：

1. 在服务器上 `mkdir -p -m 700 ~/wm-init`，再 `chmod 700 ~/wm-init`。以后 root 要从这个目录跑脚本，所以别人不能往里写，已经存在的目录也一并收紧到 700。
2. 用 `scp.exe`（Windows 自带，`-S` 指定同一个 `ssh.exe`）把四个文件传到 `wm-server:wm-init/`。
3. 在服务器上跑 `sha256sum`，和本机算的**逐个比对**：全一样才算成功，退出码 0，屏幕上是四行 `same`；任何一个不一样，或者哪一步失败，都停下、退出码非 0（哈希不符是 1，连不上是 255）。**不符就不要从 `~/wm-init` 跑任何东西。**

日志 `state/ssh-calls.log` 里记下这四个文件的 SHA-256，评审时记下的哈希和它对得上。没核实：`--put` 在本机用假的 ssh / scp 测过（正常、哈希不符、scp 失败、连不上 255、被拒绝）；真的 `scp.exe` 带 `-S` 和带反斜杠路径的 `-o` 这个组合没连过服务器，第一次真跑有可能在第 2 步失败——失败时它会停在那一步并报退出码，服务器上最多留下一个空的 `~/wm-init`。

脚本必须是 **LF 行尾**：不要用记事本之类打开另存，变成 CRLF 以后 `bash` 会报 `\r` 错误。

**③ 先只检查**（退出码 0 = 没有要改的，10 = 有要改的）：

```bash
bash deploy/server/wm-ssh.sh -- 'bash ~/wm-init/wm-harden.sh --check'
```

`--check` **不写服务器上的任何东西**：不建日志目录、不写日志文件、不建锁文件、不建临时文件、不备份、不改任何配置，所有输出只打到标准输出（`--apply` 才建 `/var/log/wm-ops/`、写 `harden.log`、加锁）。它做的都是读：读配置文件、`sshd -T`、`ss`、`dpkg-query`、`systemctl` 的查询，以及 `apt-get -s` 的模拟（关了加锁和缓存文件，所以连 root 也不写）。不算在内的是系统自己写的：`ssh` 登录和 `sudo` 写进系统日志的那几行；`ufw` 查规则时可能给早已存在的 `/run/xtables.lock` 加锁（没核实）。带了 `--security-updates-now` 时，`--check` 只打印"会做什么"，不执行。

**④ 看过输出、没有意外，再动手。** 积压的安全更新有两种做法，**由你选**（原因见下面的表）：

```bash
# 做法 A（不带选项）：只加固，积压的安全更新交给定时器
bash deploy/server/wm-ssh.sh -- 'bash ~/wm-init/wm-harden.sh --apply'

# 做法 B：加固做完，当场把积压的安全更新也装掉
bash deploy/server/wm-ssh.sh -- 'bash ~/wm-init/wm-harden.sh --apply --security-updates-now'
```

| | 做法 A：不带选项 | 做法 B：`--security-updates-now` |
|---|---|---|
| 积压的更新什么时候装 | 下一次 `apt-daily-upgrade.timer` 触发时自动装。它每天 06:00（服务器时间，服务器在北京时间）触发，再随机延迟最多 60 分钟，也就是**服务器时间 06:00 到 07:00**，你电脑（UTC-8）上是前一天 14:00 到 15:00。基线那一刻显示下一次排在 2026-11-16 06:46:46（北京时间）。装之前要有新的软件包列表：`apt-daily.timer` 每天 06:00 和 18:00 触发，随机延迟最多 12 小时，负责更新列表 | 脚本的**最后一步**当场装：`apt-get update` → `unattended-upgrade --dry-run -d`（先打印"会装几个、装哪些"，完整输出进日志）→ `unattended-upgrade -v`（真装）→ 报告有没有 `/var/run/reboot-required` |
| 你看不看得到 | 看不到：无人值守的凌晨自动发生，日志在服务器的 `/var/log/unattended-upgrades/`，出问题你发现得晚 | 看得到：输出在你眼前，也记进 `/var/log/wm-ops/harden.log`。真装的那一步放在一个临时的 systemd 单元（`wm-security-updates`）里跑，ssh 连接中途断了也不会把 dpkg 切成两半；进度看 `journalctl -u wm-security-updates` |
| 装进去的东西 | 积压的**全部安全更新**，**可能包括内核、openssh、systemd**（基线时有 216 个包待升级，其中多少是安全更新没核实） | 同左，一样的东西，只是时间点在你眼前 |
| 重启 | **绝不自动重启**（配置里 `Automatic-Reboot "false"`，needrestart 设成只列出不重启服务）。内核、libc、systemd 这类更新装完以后会出现 `/var/run/reboot-required`，新内核要重启才生效。**要不要重启、什么时候重启，由你定**；重启会断开所有 ssh，动之前先拍快照 | 同左；脚本最后会明说有没有待重启，没有就不用管 |

两种做法都要先拍快照。`/var/run/reboot-required` 出现以后，在你重启之前，`wm-verify` 会把它列成 `INFO`。基线时两个定时器的触发记录是：`apt-daily` 上次 2026-11-14 19:53:45、下次 2026-11-15 14:17:23，`apt-daily-upgrade` 上次 2026-11-15 06:55:39、下次 2026-11-16 06:46:46（都是服务器的北京时间），和上面的窗口吻合；`OnCalendar` 和随机延迟的取值来自 Ubuntu noble 的 apt 包（`debian/apt-daily.timer`、`debian/apt-daily-upgrade.timer`，Launchpad `git.launchpad.net/ubuntu/+source/apt`，读取日期 2026-11-15），**这台机器上的 `systemctl cat` 基线里没采，没核实**。

`--apply` 开头先拍一份 `before-harden` 快照，结尾再拍 `after-harden`（`wm-snapshot --save`）。**改完 sshd 以后，先确认能用密钥重新连上，再做别的**：再跑一次 `bash deploy/server/wm-ssh.sh -- true`，这是一条新的连接，退出码 0 就说明密钥登录没问题；不放心就在网页终端里也看一眼（第 8 节）。

**⑤ 验收**：

```bash
bash deploy/server/wm-ssh.sh -- 'sudo /usr/local/sbin/wm-verify'
```

每行 `PASS` / `FAIL`，另有 `INFO`（事实）和 `MANUAL`（只有你在控制台才看得到的）。有 `FAIL` 退出码是 1。

**⑥ 以后每次动手前后各记一份状态，再对比**：

```bash
bash deploy/server/wm-ssh.sh -- 'sudo /usr/local/sbin/wm-snapshot --save before'
# ... 做改动 ...
bash deploy/server/wm-ssh.sh -- 'sudo /usr/local/sbin/wm-snapshot --save after'
bash deploy/server/wm-ssh.sh -- 'sudo /usr/local/sbin/wm-snapshot-diff'
```

`--save` 的快照存在服务器的 `/var/lib/wm-ops/<UTC 时间>-<标签>/`（目录 0700、文件 0600，只有 root 能读），内容和第 3 节的基线一样，只是一块一个文件。

## 6. 脚本做了什么（对照方案页的加固清单，和 11-14 基线的差异）

详细的"现在是什么样、要改什么"在 `state/20261114-1815-baseline/差异.md`。

| 加固项 | 11-14 基线 | 脚本做的事 | `wm-verify` 怎么验 |
|---|---|---|---|
| 只许密钥登录 | **不成立**：`sshd -T` 里 `passwordauthentication yes`、`permitrootlogin yes`。`50-cloud-init.conf` 的 `PasswordAuthentication yes` 盖掉了主文件末尾腾讯云加的 `no`（sshd 同一个关键字取第一个值） | 写 `/etc/ssh/sshd_config.d/00-wm-hardening.conf`：`PasswordAuthentication no`、`KbdInteractiveAuthentication no`、`PermitRootLogin no`、`PubkeyAuthentication yes`，另有 `PermitEmptyPasswords no`、`X11Forwarding no`、`AllowAgentForwarding no`、`LoginGraceTime 30`。`00-` 排在 `50-` 前面所以生效。先 `sshd -t`，再检查 `sshd -T` 真的是这四个值，才 reload；任何一步不对就回滚。**不用 `AllowUsers`**（会挡住网页终端 SSH 通道用的用户，B3-ALLOW）；保留 SFTP 子系统；不动 `AllowTcpForwarding` 和 `MaxSessions`。动手前先查 `ubuntu` 的公钥和权限，不合格就拒绝关密码登录 | `ssh.*`：`sshd -T` 的四个值、有没有 `AllowUsers`、SFTP、本机用密码登录一次被拒、`sshd_config.d/` 里别的文件有没有打开密码登录（谁排在前面谁说了算）、**主 `sshd_config` 有没有被改过**（B2-b） |
| 只开必要端口 | 对外只有 22；ufw 已装但 `inactive`、没规则；nftables 规则集空 | ufw：先放行 22/tcp（以及 sshd 实际监听的其他端口），再 `default deny incoming`、`default allow outgoing`，最后启用。不用 `ufw limit` | `firewall.*`：ufw 开着、默认策略、只有 22/tcp；`ss` 里没有别的对外监听。控制台防火墙关 80 要你自己点（`MANUAL`） |
| 自动安全更新 | **等于没开**：定时器在，但 `Allowed-Origins` 只有 noble 本体；216 个包待升级 | 写 `/etc/apt/apt.conf.d/52wm-unattended-upgrades`：每天更新列表并装更新，**只**允许 `-security` 和 ESM 来源，**不自动重启**；确认两个 apt 定时器开着。积压的更新有两种装法（第 5 节 ④）：不带选项 = 下一次 `apt-daily-upgrade` 自动装；`--apply --security-updates-now` = 脚本最后当场装。两种都不自动重启 | `updates.*`：配置的有效值（`apt-config dump`）、定时器、48 小时内有运行记录、有没有待重启 |
| 登录失败封禁 | fail2ban 没装（候选 1.0.2-3ubuntu0.1） | 只装 fail2ban 这一个包（`--no-install-recommends`，不带推荐依赖 `whois`、`python3-pyinotify`；它的硬依赖 `python3-systemd` 已经装着；`NEEDRESTART_MODE=l`，不让 needrestart 自动重启服务），写 `jail.d/90-wm-sshd.local`：`mode = normal`、`maxretry = 5`、`findtime = 10m`、`bantime = 10m`、`backend = systemd`；`fail2ban-client -t` 通过才重启 | `fail2ban.*` |
| 时间同步 | **已满足**：chrony 在跑、已同步、配置里就是 `time1`–`time5.tencentyun.com` | 五个源都在就不碰；缺的才补，并且只注释掉别的 `pool` / `server` 行（`WM_CHRONY_KEEP_POOLS=1` 可保留）；systemd-timesyncd 没装 | `time.*`：chrony 在跑、timesyncd 没在跑、配置里五个 `server` 行、chrony 认得 3 个以上源、至少选中一个、偏差小于 0.1 秒、`Leap status` 正常 |
| 日志 | journal 已落盘，没设大小上限；rsyslog 也在写 | journald：`Storage=persistent`、`SystemMaxUse=1G`、一天一个文件、留 90 天；`/var/log/wm-ops/*.log` 每周轮转 | `logs.*` |
| 程序目录和服务用户 | 都还没有 | `/srv/worldmodel/`（owner 是 `ubuntu`、group 是 `wm`、模式 2750）下建 `releases/`（2750）、`shared/`（2770）；建不能登录的系统用户 `wm`（shell 是 nologin、密码锁着、没有 home） | `dirs.*`、`accounts.wm-*` |
| 腾讯云组件别动 | `tat_agent`、`sgagent`、`barad_agent`、`YDService`、`YDLive` 在跑 | 脚本不碰 | `tencent.*`：看 `tat_agent` 还在不在（网页终端应急入口靠它） |

`wm-verify` 还会把 `ubuntu` 的 sudo 权限、`/etc/sudoers.d` 里的文件、别的可登录账号（比如 `lighthouse`）作为 `INFO` 列出来；`ubuntu` 保留免密 sudo 是设计，不算 FAIL。

## 7. 怎么回退

1. **脚本改过或替换过的每个文件**，改之前都备份在 `/var/backups/wm-ops/<UTC 时间>/` 下，路径和原来一样。例如恢复 sshd 以前的样子：删掉 `00-wm-hardening.conf`，`sudo sshd -t`，`sudo systemctl reload ssh`。
2. **按项退回**（网页终端里，root 身份）：
   - sshd：`rm /etc/ssh/sshd_config.d/00-wm-hardening.conf && sshd -t && systemctl reload ssh`
   - ufw：`ufw disable`（规则还留着，要清空用 `ufw reset`）
   - fail2ban：`rm /etc/fail2ban/jail.d/90-wm-sshd.local && systemctl restart fail2ban`；被误封了：`fail2ban-client unban --all`
   - 自动更新：`rm /etc/apt/apt.conf.d/52wm-unattended-upgrades`
   - chrony：只有当时缺源、脚本才写过 `/etc/chrony/sources.d/wm-tencent.sources`；删它，把 `/etc/chrony/chrony.conf` 里行首的 `# wm-disabled: ` 去掉（如果有），`systemctl restart chrony`
   - journald：删 `/etc/systemd/journald.conf.d/00-wm.conf`，`systemctl restart systemd-journald`
3. **整机退回**：控制台快照回滚。**回滚会把整块系统盘退回快照时间点，快照之后写入的数据全部丢失，实例运行中会自动关机，不可逆**（B5-LH-b）。所以动 ssh、防火墙、内核之前先拍一张。
4. 脚本**不会**自动删：`/usr/local/sbin/wm-*`、`/var/lib/wm-ops/`（快照）、`/var/log/wm-ops/`、`/var/backups/wm-ops/`、`/srv/worldmodel/` 和用户 `wm`（里面可能已经有数据）。要删手工删。

## 8. 应急入口（不靠 22 端口上的 sshd）

| 入口 | 在哪 | 要不要系统密码 | 说明 |
|---|---|---|---|
| 网页终端，经自动化助手的免密登录 | 轻量控制台 → 实例详情页 →「服务器登录」→「免密登录」 | 不要 | 不走 22 端口（B3-TAT-sshd，官方没有字面写"不经过 sshd 进程"，所以是部分核实）。**前提是 `tat_agent` 在线，别卸**（`wm-verify` 会查；基线里它在跑、开机启动）。ufw 只拦入站，不影响它。另有一条"预绑密钥 + `lighthouse` 用户"的 SSH 通道：加固里没有用 `AllowUsers`，所以不会被挡；但如果把轻量防火墙的 22 只对一个来源开，这条通道会断，自动化助手那条不受影响（B4-LH-b） |
| VNC | 实例详情页 →「服务器登录」→「VNC 登录」（B3-VNC-LH） | **要**（用户名 + 密码） | 不能复制粘贴、不能传文件。`ubuntu` 要是没设过密码（或忘了），先在控制台「重置密码」设一个。用完可以把它锁回去：`sudo passwd -l ubuntu`（`ubuntu` 的 sudo 是免密的，所以锁了也不会丢 root）；锁密码不影响密钥登录。在线重置会改 `sshd_config`（B2-b），我们的设置在单独的文件里排在前面，所以不受影响，`wm-verify` 会提示主配置被改过 |
| 控制台快照回滚 | 实例详情页 →「快照」→「回滚」 | 不要 | 见第 7 节第 3 点 |

## 9. 换 SSH 端口要多做什么（这一版**默认不换**）

默认不换：22 端口不按来源 IP 放行，防护靠"只许密钥登录 + 失败封禁"（方案页）。以后真要换：

1. Ubuntu 24.04 的 sshd 是 **systemd socket 激活**的（`ssh.socket`），监听端口由 socket 决定（基线里 `ssh.socket` 和 `ssh.service` 都启用，`ssh.socket` 监听 `0.0.0.0:22` 和 `[::]:22`）。Ubuntu Discourse 上那篇《SSHd now uses socket-based activation (Ubuntu 22.10 and later)》（Ubuntu 开发者写的说明帖，读取日期 2026-11-14）说：24.04 里端口和地址由 systemd 生成器从 sshd 配置里读，不再像 22.10–23.10 那样迁到 `ssh.socket.d/addresses.conf`；帖子没写生成器读不读 `sshd_config.d/` 下的文件，社区有反映在那里设端口无效——**没核实**。
2. 稳妥做法：先在 ufw 和腾讯云防火墙里**放行新端口**（22 先留着）。再 `sudo systemctl edit ssh.socket`，写成：

   ```
   [Socket]
   ListenStream=
   ListenStream=<NEW_PORT>
   ```

   第一行空的 `ListenStream=` 是清掉默认值。然后 `sudo systemctl daemon-reload && sudo systemctl restart ssh.socket`。
3. **另开窗口用新端口连通**，再关 22 的 ufw 规则和控制台规则。
4. 同步改：本机 ssh 别名里的 `Port`；`pin-hostkey.sh` 现在只认 22，换端口后 `known_hosts` 里要写成 `[wm-server]:<NEW_PORT>`，这一步要先改脚本；`90-wm-sshd.local` 里 `[sshd]` 下加 `port = <NEW_PORT>`；之后跑脚本时带 `WM_SSH_PORT=<NEW_PORT>`（让 ufw 放行和验收都用新端口）；网页终端的「终端连接（SSH）」要填新端口。
5. 不想用 socket 激活：官方帖给的退回做法是 `systemctl disable --now ssh.socket`、删 `/etc/systemd/system/ssh.service.d/00-socket.conf` 和 `/etc/systemd/system/ssh.socket.d/addresses.conf`、`systemctl daemon-reload`、`systemctl enable --now ssh.service`。退回以后 `sshd_config` 里的 `Port` 才直接生效（这一句是推论，没核实）。

## 10. 可调的变量和退出码

`wm-harden.sh`（设在命令前面，例如 `'WM_SSH_PORT=2222 bash ~/wm-init/wm-harden.sh --check'`；这些变量会被一起传给 `sudo` 之后的进程）：

| 变量 / 选项 | 作用 | 默认 |
|---|---|---|
| `WM_SSH_PORT` | 防火墙要放行的 ssh 端口 | 22 |
| `WM_CHRONY_KEEP_POOLS` | 1 = 保留 Ubuntu 默认的 NTP pool | 0 |
| `WM_JOURNAL_MAX_USE` / `WM_JOURNAL_KEEP_FREE` / `WM_JOURNAL_RETENTION` | journald 的大小上限 / 保留的空闲空间 / 保留时间 | 1G / 2G / 90day |
| `WM_FAIL2BAN_IGNOREIP` | 额外永不封禁的地址，空格分隔（共用的代理出口地址放这里会让它的失败登录也不再被计数，要权衡） | 空 |
| `WM_ALLOW_OTHER_OS` | 1 = 不是 Ubuntu 24.04 也不拦 | 0 |
| `--no-snapshots` | 不拍前后快照 | 关 |
| `--rebaseline` | 把当前主 `sshd_config` 记为新基线（升级 `openssh-server` 包后可能需要） | 关 |
| `--security-updates-now` | 只和 `--apply` 一起用：加固做完后当场装积压的安全更新（第 5 节 ④），从不重启。不带它，积压的更新由下一次 `apt-daily-upgrade` 装 | 关 |

`wm-snapshot.sh`：不带参数或 `--stdout` = 只读、打印到标准输出；`--save 标签` = 必须 root，存到 `/var/lib/wm-ops/`；环境变量 `WM_SNAPSHOT_MAX_HASH_BYTES`（超过这个大小的文件只列不算哈希，默认 256 MiB）。

退出码：`wm-harden.sh` 和 `wm-verify.sh`：`0` 成功（`--check` 时表示没有要改的）；`10` `--check` 发现有要改的；`1` 出错（`wm-verify`：有 FAIL）；`2` 用法错；`3` 出于安全拒绝（例如 `ubuntu` 没有可用密钥——那样关掉密码登录会把你锁在外面）。`wm-snapshot.sh`：`0` 正常；`2` 用法错，或 `--save` 没用 root；`3` 某一块撞上了密钥或密码哈希的特征被扣下。`wm-ssh.sh`：ssh（也就是远端命令）的退出码；`--put` 里复制或哈希核对没通过是 `1`；`2` 拒绝。

脚本在服务器上留下的东西：`/usr/local/sbin/wm-*`、`/var/lib/wm-ops/`（快照和 `sshd_config` 基线，只有 root 能读）、`/var/log/wm-ops/`、`/var/backups/wm-ops/`，以及上面各项的配置文件。

## 11. 已知限制和没核实的

- **加固脚本和验收脚本没在 Ubuntu 24.04 上跑过。** 本机只做了 `bash -n` 和几段文本处理的单独测试（chrony 行的处理、监听端口归类、地址打码、失败计数、`write_file` 的比较和 `--check` 不写日志、不建锁文件的开关）。第一次上机请务必先 `--check`。
- **`--security-updates-now` 没跑过**：它用 `systemd-run` 起临时单元 `wm-security-updates` 来装，用 `unattended-upgrade --dry-run -d` 输出里的 "Packages that will be upgraded:" 那一行来数"会装几个、装哪些"——这一行的写法、`systemd-run --wait` 把服务退出码带回来的行为，都是按文档和记忆写的，**没核实**；找不到这一行时它会如实打印"没有要装的"和最后几行输出，完整输出在 `harden.log`。
- **`wm-ssh.sh --put` 的真机没试**：只在本机用假的 ssh / scp 测过流程（见第 5 节 ②）。`scp.exe` 从 OpenSSH 9.0 起默认走 SFTP 协议，服务器的 SFTP 子系统基线里在（`sshd -T` 有 `subsystem sftp`）；加固不动它。
- **快照脚本的只读用法在服务器上跑通了**：11-14 跑了三遍（18:09、18:12、18:15，脚本在中间补过几项），最后一遍退出码 0、没有权限报错，里面的 awk、sed 在服务器自带的版本下结果正常。`--save` 的用法（要 root、要写 `/var/lib/wm-ops/`）**没跑过**。
- **11-14 基线里解决了的几个"没核实"**（这台机器上的实测，不是官方文档）：D1-a 自动更新默认开没开——定时器开着，但来源被限成只有 noble 本体，等于没开；D1-b `sshd_config.d/` 里有没有 cloud-init 放的打开密码登录的文件——有，`50-cloud-init.conf`，并且赢了主文件；D3-a 腾讯云 NTP 域名在服务器上能不能用——能，chrony 已在用，同步正常；D2 `ubuntu` 是否免密 sudo、有没有 `lighthouse`——是，有。
- **密码状态已查**（11-15，`passwd -S`，只留状态字母）：`root` 锁定、`lighthouse` 锁定、`ubuntu` **有可用密码（P）**——所以加固前 sshd 开着密码登录的这段时间，`ubuntu` 可以被爆破，详见 `差异.md`。
- **仍然没核实**：216 个待升级的包里有多少是安全更新；轻量控制台防火墙（云侧）现在放行了什么，服务器上看不到；离线绑定密钥落在哪个用户（4a②）；`nft list ruleset` 会不会让内核加载模块（上面第 3 节）。
- **fail2ban 在 24.04 上能不能匹配到登录失败**：24.04 的 sshd 单元叫 `ssh.service`，而过滤器写的是 `_SYSTEMD_UNIT=sshd.service + _COMM=sshd`，我推断靠 `_COMM=sshd` 匹配，没装上去试过。**fail2ban 和 ufw 的配合**：fail2ban 默认用 iptables 的 multiport 动作，Ubuntu 24.04 上 `iptables` 是 nft 后端，一般能和 ufw 共存，没核实。
- **fail2ban 的 `ignoreip`**：不设就用它自带的回环地址默认值；`wm-verify` 用回环地址做一次"密码登录会被拒"的探测，靠这个默认值才不会被封。这一次探测只连服务器自己（localhost）：先把本机的主机公钥取到一个临时文件、用完就删，探测用正常的主机指纹检查，没有放宽的选项（这一段没在服务器上跑过）。对话连服务器一律经 `wm-ssh.sh`。
- **主机安全基础版只检测爆破、不拦截**（B4-x2）；拦截靠 fail2ban。
- 脚本里没有任何密钥、IP、账号编号、桶名、域名（除了官方的 `tencentyun.com` 时间服务器）。需要的地方写占位符；脚本里长得像地址的字符串只有回环地址和占位符。
- 这次不归档；以后要把东西放进知识库只用 `archive_only_extra.py`（先加 `--dry-run`），`state/` 不进。
