# 操作面板和宿主 MCP 客户端的自检: 起 8794 的 mock 服务 (同 selftest.ps1, 不碰 8790 和真实库), 设置放在 build\selftest\panel-settings
# (不碰用户真正的设置). 起一个开发版桌宠 (本机接口开在 8795, 页面静音), 自动打开面板跑面板页面自己的自检,
# 再用 panel_test.py 从外面查 (设置文件、pet_state、MCP 客户端的新旧两版), 最后把面板的每一页截成 PNG.
# 测试期间屏幕上会出现第二个桌宠和设置窗口, 测完自己关掉.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,     # mock 服务
  [int]$ApiPort = 8795,  # 测试桌宠的本机接口
  [int]$StubPort = 8796, # 旧版 MCP 替身 (HTTP)
  [int]$BalPort = 8798,  # 假的 DeepSeek 余额接口 (账号与余额页)
  [int]$IlinkPort = 8809 # 假的微信 iLink (IM 机器人页的扫码)
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
$settingsDir = Join-Path $outDir 'panel-settings'
$out = Join-Path $outDir 'panel-results.jsonl'
foreach ($p in @($Port, $ApiPort, $StubPort, $BalPort, $IlinkPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
Remove-Item $settingsDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $out -ErrorAction SilentlyContinue
# 一个示例 skill, 面板的 Skill 页能看到它
$skill = Join-Path $settingsDir 'skills\bedtime-story'
New-Item -ItemType Directory -Force $skill | Out-Null
Set-Content (Join-Path $skill 'SKILL.md') -Encoding UTF8 -Value @'
---
name: bedtime-story
description: 用户要听睡前故事时用: 语速放慢, 故事温和, 结尾道晚安
---

# 睡前故事
1. 先问想听什么主题.
'@
# 账号与余额页: 两周的假账本 + 假的余额接口 (http.server 发一个 JSON 文件; key 用测试环境变量, 不用你的 DEEPSEEK_API_KEY)
$balDir = Join-Path $outDir 'fake-balance'
Remove-Item (Join-Path $outDir 'logs\billing') -Recurse -Force -ErrorAction SilentlyContinue
python (Join-Path $PSScriptRoot 'billing_seed.py') (Join-Path $outDir 'logs\billing') --balance-dir $balDir
$bal = Start-Process python -ArgumentList @('-m', 'http.server', "$BalPort", '--bind', '127.0.0.1', '--directory', "`"$balDir`"") `
  -PassThru -WindowStyle Hidden
$env:WM_SELFTEST_DS_KEY = 'sk-selftest-balance-not-real'
# IM 机器人页: 假的 iLink (给二维码, 一直"等扫码"); 测试服务端没开记忆库, 本来就不收微信, 绑好的账号放在临时目录也碰不到
$ilink = Start-Process python -ArgumentList @((Join-Path $PSScriptRoot 'fake_ilink.py'), '--port', "$IlinkPort") -PassThru -WindowStyle Hidden
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')",
  '--set', "billing.balance_url=http://127.0.0.1:$BalPort/user/balance", '--set', 'billing.env_key=WM_SELFTEST_DS_KEY',
  '--set', "channels.weixin.base_url=http://127.0.0.1:$IlinkPort", '--set', "channels.weixin.home=$(($outDir + '\weixin') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'server.out.log') -RedirectStandardError (Join-Path $outDir 'server.err.log')
$code = 1
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 server.err.log' }
  $query = 'selftest=1&mcpcmd=' + [uri]::EscapeDataString($exe) + '&mcpargs=' + [uri]::EscapeDataString("--mcp-stdio --api-port $ApiPort")
  $common = @('--server', "http://127.0.0.1:$Port", '--query', 'mute=1', '--no-autohide', '--settings-dir', "`"$settingsDir`"")
  $pet = Start-Process $exe -ArgumentList ($common + @('--api', '--api-port', "$ApiPort", '--panel', '--panel-query', "`"$query`"", '--out', "`"$out`"")) -PassThru
  python (Join-Path $PSScriptRoot 'panel_test.py') --exe $exe --pid $pet.Id --settings-dir $settingsDir --out $out --api-port $ApiPort --stub-port $StubPort
  $code = $LASTEXITCODE
  if (-not $pet.WaitForExit(15000)) { $pet.Kill() }
  # 截图: 用同一份 (自检改过的) 设置, 依次截面板的每一页
  $shot = Start-Process $exe -ArgumentList ($common + @('--panel-shot', "`"$(Join-Path $outDir 'panel')`"")) -PassThru
  if (-not $shot.WaitForExit(90000)) { $shot.Kill(); Write-Warning '截图超时' }
  Get-ChildItem (Join-Path $outDir 'panel-*.png') | ForEach-Object { "截图 $($_.Name) $($_.Length) 字节" }
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $bal.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $ilink.Id -Force -ErrorAction SilentlyContinue
}
exit $code
