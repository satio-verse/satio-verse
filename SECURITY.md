# 安全策略

## 报告漏洞

请不要在公开 issue 里贴可利用细节。发邮件到 `VicTanora001@Outlook.com`，或用 GitHub 的
「Report a vulnerability」（Security → Advisories）私密报告。我们会在 7 天内回复，
并在修复后于 CHANGELOG / Release Notes 里致谢（如果你愿意）。

## 这个项目的安全边界（重要）

- **AI 在本机跑，密钥不上传。** 本仓库不包含任何 API key、模型权重、角色人格与记忆库。
- **插件默认最小权限。** 世界插件在 `wmworld.json` 里声明权限，安装时只能拿到四档之一：
  `text` / `panel` / `code` / `process`；宿主默认只给 `text`，其它档需要用户显式授权。
- **安装前先看报告。** 市场安装走「装前报告」（`/api/...` 的 `community_inspect`），
  报告不过关直接拒装。不要绕过它。
- **不要直接运行陌生人给的脚本。** 插件包只应通过安装流程导入，而不是手动解压到 `plugins/`。

## 支持范围

受支持的是 `main` 分支最新 Release。旧版本请先升级再报问题。
