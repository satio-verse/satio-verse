# M2 本机接口自检: 起 8794 的 mock 服务 (同 selftest.ps1, 不碰 8790 和真实库), 再起一个开发版桌宠 (普通模式, 本机接口开在 8795, 页面静音),
# 用 api_test.py 测 OpenAI 兼容接口 (官方 openai SDK)、MCP (新旧两版协议) 和 stdio 转发, 最后正常关掉这个测试桌宠.
# 测试期间屏幕右下角会出现第二个桌宠 (不出声), 托盘里也会多一个图标, 测完就没了.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,     # mock 服务
  [int]$ApiPort = 8795   # 测试桌宠的本机接口 (用户的桌宠默认 8793, 不和它抢)
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
New-Item -ItemType Directory -Force $outDir | Out-Null
foreach ($p in @($Port, $ApiPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'server.out.log') -RedirectStandardError (Join-Path $outDir 'server.err.log')
$pet = $null
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 server.err.log' }
  # 设置用一个空目录 (默认预设): 用户在操作面板里选了云端预设的话, 读真设置会让自检去调真实的云端模型
  $settings = Join-Path $outDir 'api-settings'
  Remove-Item $settings -Recurse -Force -ErrorAction SilentlyContinue
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--no-autohide', '--api-port', "$ApiPort", '--query', 'mute=1',
    '--settings-dir', "`"$settings`"") -PassThru
  python (Join-Path $PSScriptRoot 'api_test.py') --port $ApiPort --exe $exe
  $code = $LASTEXITCODE
} finally {
  if ($pet -and -not $pet.HasExited) {
    python (Join-Path $PSScriptRoot 'api_test.py') --close $pet.Id
    if (-not $pet.WaitForExit(10000)) { $pet.Kill() }
  }
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
exit $code
