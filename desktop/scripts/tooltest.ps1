# 桌宠工具调用的端到端自检: 假云端 (fake_cloud.py, 8797) + 8794 的 mock 服务 (不碰 8790 和真实库) + 开发版桌宠 (接口开在 8795, 页面静音).
# 设置放在 build\selftest\tool-settings (不碰用户真正的设置): 当前预设用"自定义"提供方连假云端, 勾上旧版 MCP 替身 stub 和一个 skill.
# 依次测: 只读工具自动调、确认框的允许一次 / 拒绝 / 超时 / 总是允许、总是允许之后不再问、load_skill、
# 慢工具在对话栏里点"停"、慢工具超过这个服务器的调用超时 (5 秒)、之后连接照常能用; 再核对审计日志、交给模型的工具名、
# 退出后 MCP 服务器进程有没有一起结束. 第一次弹确认框时截一张页面 (build\selftest\tool-ask.png).
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,       # mock 服务
  [int]$ApiPort = 8795,    # 测试桌宠的本机接口
  [int]$CloudPort = 8797   # 假云端
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
$settingsDir = Join-Path $outDir 'tool-settings'
$cloudLog = Join-Path $outDir 'fake-cloud.jsonl'
foreach ($p in @($Port, $ApiPort, $CloudPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
Remove-Item $settingsDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $cloudLog, (Join-Path $outDir 'tool-ask.png') -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $settingsDir | Out-Null
python (Join-Path $PSScriptRoot 'tool_test.py') --prepare $settingsDir --cloud-port $CloudPort
$cloud = Start-Process python -ArgumentList @("`"$(Join-Path $PSScriptRoot 'fake_cloud.py')`"", '--port', "$CloudPort", '--log', "`"$cloudLog`"") -PassThru -WindowStyle Hidden
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')")
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
  $query = 'mute=1&toolanswers=once,deny,timeout,always&asktimeout=3&toolstop=1500'
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--no-autohide', '--api-port', "$ApiPort", '--query', "`"$query`"",
    '--settings-dir', "`"$settingsDir`"", '--ask-shot', "`"$(Join-Path $outDir 'tool-ask.png')`"") -PassThru
  python (Join-Path $PSScriptRoot 'tool_test.py') --run --settings-dir $settingsDir --pid $pet.Id --cloud-log $cloudLog --api-port $ApiPort
  $code = $LASTEXITCODE
  if (-not $pet.WaitForExit(15000)) { $pet.Kill() }
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $cloud.Id -Force -ErrorAction SilentlyContinue
}
exit $code
