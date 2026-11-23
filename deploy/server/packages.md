# 要装的软件清单（草稿）

读取日期：**2026-11-14**。第 0 批清单里的五样，**11-14 晚的只读基线显示只缺 `fail2ban`，其余四个已经装着**；后面几批先列着、**不装**，等到对应批次再请你批。

**怎么读这张表**

- **来源**：`Ubuntu 24.04 (noble) 官方仓库` 指 Ubuntu 的 `main` 或 `universe` 组件。服务器上的 apt 实际走腾讯云的内网镜像 `mirrors.tencentyun.com`（11-14 只读基线里 `apt-cache policy` 的结果，含 noble、noble-updates、noble-backports、noble-security）。`fail2ban`、`certbot`、`python3-venv` 等在 `universe` 组件里；有些 universe 的包在页面上带 `[security]` 版本（例如 `python3-aiohttp`、`python3-venv`），但 universe 里的安全补丁能不能及时到、自动更新能不能覆盖，没核实。
- **大小**：`下载 / 装后`。Ubuntu 的包来自 packages.ubuntu.com 的 noble 页面（架构 amd64；纯脚本类的包架构是 `all`），单位 kB 是页面原样；版本号是页面当天显示的（带 `[security]` 的是安全更新那一版）。PyPI 和 GitHub 的写发布页上标的文件大小。
- **许可**：Ubuntu 的包来自该包版权文件（`changelogs.ubuntu.com/.../copyright`）；PyPI 来自项目页的"License expression"；coscli 来自仓库的 `LICENSE`。
- 查不到的写"没核实"，没有估算。

## 第 0 批（接手加固，`wm-harden.sh --apply` 会装缺的那几个）

| 名称 | 来源 | 大小（下载 / 装后） | 许可 | 为什么装 | 批 |
|---|---|---|---|---|---|
| `fail2ban` 1.0.2-3ubuntu0.1（11-14 基线里的候选版本） | noble-updates 仓库，universe（腾讯云镜像） | 478.3 kB / 2,075.0 kB（all）是 packages.ubuntu.com 上 1.0.2-3 的数字，**ubuntu0.1 这一版的大小没核实**。fail2ban 有三个"推荐依赖"（noble 的 `debian/control`：`nftables` 或 `iptables`、`whois`、`python3-pyinotify`）。**脚本装的时候带 `--no-install-recommends`，所以 `whois` 和 `python3-pyinotify` 不会装**（我们用 `backend = systemd`，用不上这两个，它们也不在批准清单里）；`nftables` 在基线里本来就装着。`--check` 里的 `apt-get -s` 模拟也带这个选项，`Inst` 开头的行就是实际会装的东西 | GPL-2.0 或更高版本（版权文件：GPL 第 2 版或更高；是 1.0.2-3 的，ubuntu0.1 的没核实） | 登录失败封禁（方案页：主机安全基础版只检测爆破、不拦截） | 0，**11-14 基线：没装，是第 0 批唯一要装的** |
| `python3-pyasyncore` 1.0.2-2 | noble 仓库，main（腾讯云镜像） | 9.9 kB / 43.0 kB（all，packages.ubuntu.com，11-15 读） | PSFL-2（Ubuntu 版权文件 `python-pyasyncore_1.0.2-2/copyright`，版权人 Simon Robinson 2023、Sam Rushing 1996；协调对话 11-15 核过） | fail2ban 的**硬依赖**：Python 3.12 去掉了 asyncore 模块，fail2ban 靠它补上。11-15 的 `--check` 模拟里才发现，用户 11-15 批了"一起装" | 0，**用户已批** |
| `python3-systemd` 235-1build4 | noble 仓库，main | 41.4 kB / 211.0 kB（amd64） | LGPL-2.1+ | fail2ban 读 systemd 日志（`backend = systemd`）要它。它是 fail2ban 的硬依赖（noble 的 `debian/control` 写在 `Depends:` 里），装 fail2ban 时 apt 会自动带上；这里单列只是为了看清大小。**11-14 基线：已装（235-1build4）** | 0，已装 |
| `chrony` 4.5-1ubuntu4 | noble 仓库，main | 308.7 kB / 638.0 kB（amd64） | GPL-2.0（版权文件：主体 GPL-2，个别文件 GPL-2+ 或 RSA-MD） | 时间同步，用腾讯云的 `time1` 到 `time5.tencentyun.com`（核对笔记 D3-a）。证书、日志、COS 签名都依赖时间准。**11-14 基线：已装（4.5-1ubuntu4.2），在跑、已同步，配置里已经是这五个源** | 0，已装 |
| `ufw` 0.36.2-6 | noble 仓库，main | 165.1 kB / 849.0 kB（all） | GPL-3（版权文件：主体 GPL-3，个别文件 BSD-3-clause） | 服务器防火墙，只开 22。**11-14 基线：已装（0.36.2-6），服务开机启动，但没启用、没规则** | 0，已装 |
| `unattended-upgrades` 2.9.1+nmu4ubuntu1 | noble 仓库，main | 50.0 kB / 412.0 kB（all） | GPL-2+ | 自动装安全更新。Ubuntu 官方文档说它默认已装、装好就开了。**11-14 基线：已装，定时器开着，但腾讯云镜像把允许的来源限成只有 noble 本体，所以一条安全更新也不会自动装**（要靠 `52wm-unattended-upgrades` 改过来） | 0，已装 |

**第 0 批合计**：**只装 `fail2ban` 一个包**（它的硬依赖 `python3-systemd` 基线里已经装着，不重装），不带推荐依赖：下载约 478 kB、装后约 2.1 MB（是 packages.ubuntu.com 上 1.0.2-3 的数字，ubuntu0.1 的没核实）。已经装着的四个不重装。（五样都缺的话是下载约 1,043.5 kB、装后约 4,185.0 kB，这个数没算它们依赖的库。）

**会被换掉的包**（**这台机器上不会发生**：11-14 基线里 chrony 已装、`systemd-timesyncd` 没装，所以没有东西要换；下面留着是因为换机器或重装系统时用得上）：装 `chrony` 时，`chrony` 声明了与任何"时间守护进程"冲突（Launchpad 上 noble 的 `debian/control`：`Conflicts: time-daemon`），而 `systemd-timesyncd` 提供这个角色，所以 apt 会把 `systemd-timesyncd`（255.4-1ubuntu8.18，下载 34.5 kB、装后 204.0 kB）一并卸掉。脚本的 `--check` 会用 `apt-get -s` 模拟，`Remv` 开头的行就是被卸的包。它的许可没核实（属于 systemd 源码包，版权文件没读）。万一没被卸，脚本只停用它，不 mask、不 purge。

**脚本还会用到的、系统自带的工具（不装，上机确认）**：`bash`、`coreutils`、`grep`、`sed`、`awk`、`findutils`、`diffutils`、`iproute2`（`ss`）、`procps`（`pgrep`）、`util-linux`（`flock`）、`sudo`、`openssh-client`（`ssh`、`ssh-keygen`）、`openssh-server`（`sshd`）、`systemd`（`systemctl`、`journalctl`）、`logrotate`、`passwd`/`adduser`（`useradd`、`adduser`）。

## 后面几批（先列着，不装）

| 名称 | 来源 | 大小（下载 / 装后） | 许可 | 为什么装 | 批 |
|---|---|---|---|---|---|
| `coscli` v1.0.9 | 腾讯云官方的 GitHub 发布页 `tencentyun/coscli`，2026-08-25 发布；腾讯云文档里 COSCLI 的那一页（`436/63669`，更新 2026-10-15）只写了命令、版本差异和授权，**没写下载地址和支持的系统、架构**，所以以发布页为准（文档里怎么说，没核实）。校验：对照发布页里的 `sha256sum.log` | `coscli-v1.0.9-linux-amd64` 13.6 MB（单个可执行文件，装后同样大小）；`linux-arm64` 13 MB | Apache-2.0（仓库 `LICENSE`） | 服务器上传、下载、列桶，密钥配在服务器上（方案页）。建议放 `/usr/local/bin/coscli`，这一点没定 | 1 |
| `nginx` 1.24.0-2ubuntu7.19 + `nginx-common` | noble 仓库，main（`[security]` 那一版） | nginx 512.6 kB / 1,323.0 kB（amd64）；nginx-common 44.3 kB / 241.0 kB（all） | BSD-2-clause（版权文件里个别文件另有 Expat、GPL-2+、public-domain） | 反向代理，证书、更新清单、插件目录的对外入口（方案页第 2 批）。**要先备案** | 2 |
| `certbot` 2.9.0-1 | noble 仓库，universe | certbot 87.1 kB / 131.0 kB（all）；它依赖的 `python3-certbot` 260.4 kB / 1,564.0 kB（all）、`python3-acme` 47.4 kB / 257.0 kB（all）；另有 `python3-josepy`、`python3-configobj`、`python3-configargparse`、`python3-parsedatetime`、`python3-rfc3339`、`python3-distro`、`python3-tz`、`python3-requests` 等，**大小没逐个核实**。可选插件 `python3-certbot-nginx` 64.4 kB / 367.0 kB（all） | Apache-2.0（版权文件：Let's Encrypt 客户端，Apache 2.0；内含的 nginxparser 部分是 MIT） | 申请和续期证书；续期演练是第 2 批的验收项 | 2 |
| `python3-venv` 3.12.3-0ubuntu2.1，连带 `python3.12-venv`、`python3-pip-whl`、`python3-setuptools-whl` | noble 仓库，universe | python3-venv 1.0 kB / 6.0 kB（amd64，元包）；python3.12-venv 5.6 kB / 28.0 kB；python3-pip-whl 24.0+dfsg-1ubuntu1.3，1,666.6 kB / 1,809.0 kB（all）；python3-setuptools-whl 68.1.2-2ubuntu1.2，699.5 kB / 875.0 kB（all）；四样下载合计约 2,372.7 kB（约 2.4 MB） | Python 本体是 PSF 许可（`python3.12` 版权文件开头写"包含 Python 许可和内嵌软件的许可"）；pip、setuptools 的许可没核实 | 给社区平台服务建 venv（`deploy/requirements-platform.txt`） | 2 |
| `aiohttp` 3.14.4（PyPI） | PyPI 官方发布页 `pypi.org/project/aiohttp/3.14.4/`，2026-10-05 上传；版本和 `deploy/requirements-platform.txt` 里固定的一致 | `cp312` + `manylinux` x86_64 的 wheel：1.9 MB。它依赖的包 pip 会一起拉，**大小没核实** | Apache-2.0 AND MIT（项目页 License expression） | 社区平台服务（`tools/community_server.py`）要它。项目页写 Python ≥ 3.10，24.04 自带 3.12，在范围内；项目文件里写的是"在 Python 3.10 环境测过"，3.12 上测没测过，没核实。apt 里的 `python3-aiohttp` 是 3.9.1-1ubuntu0.1（276.1 kB / 1,369.0 kB，universe），比固定的版本旧，不用 | 2 |
| `cryptography` 49.0.0（PyPI） | PyPI 官方发布页 `pypi.org/project/cryptography/49.0.0/`，2026-06-12 上传；版本和 `deploy/requirements-signatures.txt` 里固定的一致 | `cp311-abi3` + `manylinux_2_34` x86_64 的 wheel：4.7 MB（abi3，Python 3.12 能用；Ubuntu 24.04 的 glibc 够新）。依赖（如 cffi）大小没核实 | Apache-2.0 OR BSD-3-Clause（项目页） | 发布包的 Ed25519 签名校验（方案页：只在更新通道开启时才要）。项目页写 Python ≥ 3.9。apt 里的 `python3-cryptography` 是 41.0.7-4ubuntu0.4（796.0 kB / 3,244.0 kB，main），比固定版本旧，不用。**装它要你批准**（方案页"等你定的"第 6 条） | 2，**需批准** |

## 上机后怎么确认来源和版本

```bash
apt-cache policy fail2ban python3-systemd chrony ufw unattended-upgrades
dpkg-query -W -f='${binary:Package} ${Version} ${Status}\n' fail2ban python3-systemd chrony ufw unattended-upgrades
```

第一条看每个包从哪个镜像来、有没有 `-security` 的更新版；第二条看已经装了没有。11-14 的只读基线里（`state/20261114-1815-baseline/baseline.txt` 的 `21-apt-sources` 块）已经有第一条的结果。`wm-harden.sh --check` 还会用 `apt-get -s` 模拟一遍，告诉你要装什么、要卸什么。

## 出处（读取日期 2026-11-14）

- Ubuntu 包页（noble）：`https://packages.ubuntu.com/noble/<包名>`，包括 fail2ban、python3-systemd、chrony、ufw、unattended-upgrades、systemd-timesyncd、nginx、nginx-common、certbot、python3-certbot、python3-acme、python3-certbot-nginx、python3-venv、python3.12-venv、python3-pip-whl、python3-setuptools-whl、python3-aiohttp、python3-cryptography
- Ubuntu 版权文件：`https://changelogs.ubuntu.com/changelogs/pool/<组件>/<首字母>/<源码包>/<源码包>_<版本>/copyright`（用的版本就是上表里写的版本；certbot 对应源码包 `python-certbot` 2.9.0-1）
- chrony 的冲突声明和默认配置：Launchpad 上 noble 的 `debian/control` 与 `debian/chrony.conf`（`git.launchpad.net/ubuntu/+source/chrony`，分支 `applied/ubuntu/noble`）
- 自动更新默认值：Ubuntu Server 文档《Automatic updates》（`ubuntu.com/server/docs/how-to/software/automatic-updates/`）
- apt 配置里 `#clear` 的写法：Ubuntu 手册页 `apt.conf(5)`（noble）
- fail2ban 的 `sshd` 过滤器和 `jail.conf` 默认值：上游 `fail2ban/fail2ban` 仓库 1.0.2 版的 `config/filter.d/sshd.conf`、`config/jail.conf`
- coscli：`github.com/tencentyun/coscli` 的发布页 v1.0.9 和 `LICENSE`；腾讯云文档里 COSCLI 的页面（`cloud.tencent.com/document/product/436/63669`，页面标注更新 2026-10-15）
- fail2ban 的依赖关系：Launchpad 上 noble 的 `debian/control`（`git.launchpad.net/ubuntu/+source/fail2ban`，分支 `applied/ubuntu/noble`）
- PyPI：`pypi.org/project/aiohttp/3.14.4/`、`pypi.org/project/cryptography/49.0.0/`
- 服务器上已装的版本、apt 源和候选版本：11-14 只读基线（`state/20261114-1815-baseline/baseline.txt` 的 `20-packages`、`21-apt-sources` 块；`state/` 不进知识库）
