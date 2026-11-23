# 操控电脑 (Windows-MCP) 的端到端自检: 假云端 (fake_cloud.py, 8797) + 8794 的 mock 服务 (不碰 8790 和真实库) + 开发版桌宠 (接口开在 8795, 页面静音)
# + 两个测试窗口 (guard_target.cpp: 普通的"守门测试窗口", 和复制成 Weixin.exe 冒充聊天软件的"微信测试-张三").
# 设置放在 build\selftest\computer-settings (不碰用户真正的设置). 服务端每轮的工具调用次数调到 40 (默认 4, 操控电脑不够).
# 默认用 Windows-MCP 的替身 (mcp_computer_stub.py, 不真点): 测四档、这一轮都允许、守门 (发送按钮、密码框、聊天软件、按编号点、运行框、
# 打开命令提示符、按路径启动)、截图不发云端、聊天窗口的标题换掉、停整轮 (对话栏的停、全局快捷键)、一轮的上限、退出后替身一起结束.
# 第一次弹确认框时截一张页面 (build\selftest\computer-ask.png).
# -Real: 换成真的 Windows-MCP (desktop\third_party\windows-mcp), 会真的动鼠标、在测试窗口的输入框里打一行字.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,       # mock 服务
  [int]$ApiPort = 8795,    # 测试桌宠的本机接口
  [int]$CloudPort = 8797,  # 假云端
  [switch]$Real
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
$settingsDir = Join-Path $outDir 'computer-settings'
$cloudLog = Join-Path $outDir 'computer-cloud.jsonl'
$windowsMcp = Join-Path $desktop 'third_party\windows-mcp\.venv\Scripts\windows-mcp.exe'
foreach ($p in @($Port, $ApiPort, $CloudPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
if ($Real -and -not (Test-Path $windowsMcp)) { throw "没装 Windows-MCP: $windowsMcp" }
Remove-Item $settingsDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $cloudLog, (Join-Path $outDir 'computer-ask.png'), (Join-Path $outDir 'target.json'), (Join-Path $outDir 'chat.json') -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $settingsDir | Out-Null

# 测试窗口: 用 MinGW 编一次, 再复制一份叫 Weixin.exe (守门按程序名认聊天软件)
$mg = 'E:\Program Files (x86)\CPEditor\mingw64\bin'
$target = Join-Path $outDir 'guard_target.exe'
& "$mg\g++.exe" -municode -mwindows -static -O1 -o $target (Join-Path $PSScriptRoot 'guard_target.cpp') -luser32 -lgdi32
if ($LASTEXITCODE) { throw '测试窗口编译失败' }
$chatDir = Join-Path $outDir 'chat'
New-Item -ItemType Directory -Force $chatDir | Out-Null
Copy-Item $target (Join-Path $chatDir 'Weixin.exe') -Force

$prepare = @((Join-Path $PSScriptRoot 'computer_test.py'), '--prepare', $settingsDir, '--cloud-port', "$CloudPort")
if ($Real) { $prepare += @('--real', $windowsMcp) }
python @prepare
$cloud = Start-Process python -ArgumentList @("`"$(Join-Path $PSScriptRoot 'fake_cloud.py')`"", '--port', "$CloudPort", '--log', "`"$cloudLog`"") -PassThru -WindowStyle Hidden `
  -RedirectStandardError (Join-Path $outDir 'computer-cloud.err.log')
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false', '--set', 'llm.cloud.max_tool_calls=40',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'computer-server.out.log') -RedirectStandardError (Join-Path $outDir 'computer-server.err.log')
$w1 = Start-Process $target -ArgumentList @('"守门测试窗口"', "`"$(Join-Path $outDir 'target.json')`"", '120', '120') -PassThru
$w2 = Start-Process (Join-Path $chatDir 'Weixin.exe') -ArgumentList @('"微信测试-张三"', "`"$(Join-Path $outDir 'chat.json')`"", '600', '120') -PassThru
$code = 1
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 computer-server.err.log' }
  $answers = if ($Real) { 'once,once,once' } else { 'once,deny,turn,turn,turn,once,turn,turn' }
  $query = "mute=1&toolanswers=$answers&asktimeout=5&toolstop=3500"
  $petLog = Join-Path $desktop 'build\dev\wmpet.log'
  $logFrom = if (Test-Path $petLog) { (Get-Item $petLog).Length } else { 0 }
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--api-port', "$ApiPort", '--query', "`"$query`"",
    '--settings-dir', "`"$settingsDir`"", '--no-autohide', '--ask-shot', "`"$(Join-Path $outDir 'computer-ask.png')`"") -PassThru
  $run = @((Join-Path $PSScriptRoot 'computer_test.py'), '--run', '--settings-dir', $settingsDir, '--pid', "$($pet.Id)", '--cloud-log', $cloudLog,
    '--api-port', "$ApiPort", '--out', $outDir, '--log-from', "$logFrom")
  if ($Real) { $run += '--real' }
  $ErrorActionPreference = 'Continue'  # python 往 stderr 写东西 (出错的回溯) 时不要当成 PowerShell 的错误中断
  python @run 2>&1 | ForEach-Object { "$_" }
  $code = $LASTEXITCODE
  if (-not $pet.WaitForExit(15000)) { $pet.Kill() }
} finally {
  foreach ($p in @($server, $cloud, $w1, $w2)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}
exit $code
