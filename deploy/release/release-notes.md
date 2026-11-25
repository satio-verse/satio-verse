## S.A.T.I.O Verse 0.3.1

第一个带完整外壳、3D 客厅、本机语音和体素原型的安装包。装好就是一个窗口，不用先配环境。

### 装了什么

- **六区外壳**：`/pet/satio.html`，三段启动（共鸣 / 织梦 / 界门）+ L0 世界、L1 底部胶囊、L2 左右抽屉、L3 能力阵列
- **3D 客厅**：three.js r180 + `@pixiv/three-vrm`，VRM 绫音、第一/第三人称、`?embed=1` 嵌入模式
- **语音**：GPT-SoVITS v5turbo（4 步、48 kHz），首包 1.0–1.4 s；ASR 是 SenseVoice；没有模型时自动退化到 mock
- **世界层**：体素街区原型（`tools/voxel_relay.py` 中继 + 浏览器 WebGL 客户端）、经济账本、跨体事件 schema
- **插件契约**：装前报告（`wmworld.json` 校验）、市场目录、`/api/update` 自更新检查

### 这版新东西

- `wm/update.py`：读远端 `update.json` 比对版本，`python -m wm.update download` 下载并校验 sha256
- `GET /api/update`：桌面壳可以直接问「有没有新版本」
- 安装包**带 Authenticode 自签名**（`CN=S.A.T.I.O Verse`，2029-11 到期），可自证完整性

### 下载

- 官网：<https://satio-verse.gitgraduate.com/releases/WorldModel-Setup-0.3.1.exe>（推荐，国内快）
- 本页 Assets：`WorldModel-Setup-0.3.1.exe`（GitHub 线路）
- 校验：`SHA256SUMS`、`satio-codesign.cer`；主文件 SHA-256 `2c050a734ff3ef9dce6fabbca8ff534bd016d904531a29f8f6efd9e4298452e5`，大小 1,855,417,520 B

### 要求

Windows 10 1809 及以上 / x64。检测到 NVIDIA 驱动会装 CUDA 组件（约 +1.1 GB），没有就用 CPU。1.7 GB 里大部分是本机模型和 Python 运行时。

### 说明

- 自签名**免不掉** SmartScreen 首次提示：点「更多信息 → 仍要运行」。要免提示得 CA 签发的 OV/EV 证书。
- 卸载走系统「应用和功能」，不动 `%USERPROFILE%\.worldmodel` 里的记忆和配置。
- 这是开源外壳 + 世界层；人格、声音权重、记忆目录不在仓库里。
