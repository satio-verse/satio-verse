# QQ 任务单 (主人在 QQ 上让她操作电脑) 在宿主和页面这一段的自检: 假云端 (8797) + 8794 的 mock 服务 (不碰 8790 和真实库) + 开发版桌宠 + Windows-MCP 的替身
# + 两个测试窗口 (同 computertest.ps1). 页面的 qqtask= 参数自己发一张任务单 (不用真连 QQ), 依次测三种:
#   owner (主人说的事: 读屏、动手不再问, 守门照旧, 开着聊天窗口时截图不留) / self (她自己想看: 只放行读屏, 动手还问) /
#   shot (关掉聊天窗口再来: 屏幕上还有聊天、命令行这类窗口就不留截图) / shotany (带 --task-shot-any 跳过那道闸门: 测截图暂存和回报). 服务端那一半在 tests/test_workbench.py.
# 设置放在 build\selftest\computer-settings (同 computertest.ps1, 不碰用户真正的设置).
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,
  [int]$ApiPort = 8795,
  [int]$CloudPort = 8797
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
$settingsDir = Join-Path $outDir 'computer-settings'
$cloudLog = Join-Path $outDir 'qqtask-cloud.jsonl'
foreach ($p in @($Port, $ApiPort, $CloudPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
Remove-Item $settingsDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $cloudLog, (Join-Path $outDir 'target.json'), (Join-Path $outDir 'chat.json') -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $settingsDir | Out-Null
New-Item -ItemType Directory -Force $outDir | Out-Null

$mg = 'E:\Program Files (x86)\CPEditor\mingw64\bin'
$target = Join-Path $outDir 'guard_target.exe'
& "$mg\g++.exe" -municode -mwindows -static -O1 -o $target (Join-Path $PSScriptRoot 'guard_target.cpp') -luser32 -lgdi32
if ($LASTEXITCODE) { throw '测试窗口编译失败' }
$chatDir = Join-Path $outDir 'chat'
New-Item -ItemType Directory -Force $chatDir | Out-Null
Copy-Item $target (Join-Path $chatDir 'Weixin.exe') -Force

python (Join-Path $PSScriptRoot 'computer_test.py') --prepare $settingsDir --cloud-port "$CloudPort" --power  # 高风险档打开
$cloud = Start-Process python -ArgumentList @("`"$(Join-Path $PSScriptRoot 'fake_cloud.py')`"", '--port', "$CloudPort", '--log', "`"$cloudLog`"") -PassThru -WindowStyle Hidden `
  -RedirectStandardError (Join-Path $outDir 'qqtask-cloud.err.log')
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false', '--set', 'llm.cloud.max_tool_calls=40',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'qqtask-server.out.log') -RedirectStandardError (Join-Path $outDir 'qqtask-server.err.log')
$w1 = Start-Process $target -ArgumentList @('"守门测试窗口"', "`"$(Join-Path $outDir 'target.json')`"", '120', '120') -PassThru
$w2 = Start-Process (Join-Path $chatDir 'Weixin.exe') -ArgumentList @('"微信测试-张三"', "`"$(Join-Path $outDir 'chat.json')`"", '600', '120') -PassThru
$code = 0
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 qqtask-server.err.log' }
  $petLog = Join-Path $desktop 'build\dev\wmpet.log'
  foreach ($mode in @('owner', 'self', 'shot', 'shotany', 'power', 'power2')) {
    if ($mode -eq 'shot') { Stop-Process -Id $w2.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }  # 关掉冒充聊天软件的窗口
    Remove-Item (Join-Path $settingsDir 'pet-tool-audit.jsonl'), (Join-Path $settingsDir 'stub-log.jsonl') -ErrorAction SilentlyContinue
    $query = (python (Join-Path $PSScriptRoot 'qqtask_test.py') --query $mode --out $outDir | Out-String).Trim()
    $logFrom = if (Test-Path $petLog) { (Get-Item $petLog).Length } else { 0 }
    $petArgs = @('--server', "http://127.0.0.1:$Port", '--query', "`"$query`"", '--settings-dir', "`"$settingsDir`"", '--no-autohide')
    if ($mode -eq 'shotany') { $petArgs += '--task-shot-any' }  # 跳过"屏幕上有聊天、命令行就不留截图"那道闸门, 测暂存和回报
    $pet = Start-Process $exe -ArgumentList $petArgs -PassThru
    $ErrorActionPreference = 'Continue'
    python (Join-Path $PSScriptRoot 'qqtask_test.py') --run $mode --settings-dir $settingsDir --pid "$($pet.Id)" --out $outDir --log-from "$logFrom" --cloud-log $cloudLog 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE) { $code = 1 }
    $ErrorActionPreference = 'Stop'
    if (-not $pet.WaitForExit(15000)) { $pet.Kill() }
  }
} finally {
  foreach ($p in @($server, $cloud, $w1, $w2)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
}
exit $code
