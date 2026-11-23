# 扩展中心 E1 的端到端自检 (见 docs/roadmap/extensions.md): 假云端 (fake_cloud.py) + mock 服务 (不碰 8790 和真实库) + 开发版桌宠.
# 设置放在 build\selftest\ext-settings (不碰用户真正的设置), 当前预设用"自定义"提供方连假云端, 内置工具都启用.
# 第一段 (小屋模式, --dry-open 不真开浏览器): 提醒 (到点、认不得的时间、列出和取消)、打开网址 (本机地址、javascript:、付款页不问就拦;
#   拒绝、允许一次)、小屋动作 (每次确认 -> 总是允许 -> 不再问; 不认识的事). 第一次弹确认框时截一张 (build\selftest\ext-ask.png).
# 第二段: 面板带 exttest=1 自己走一遍内容包 (预览、导入、重复导入、改过的包、../ 路径、不是内容包、没写许可、导出、卸载), 再核对导出的文件.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8814,       # mock 服务 (8794 常被别的会话的自检占着)
  [int]$ApiPort = 8815,    # 测试桌宠的本机接口
  [int]$CloudPort = 8817   # 假云端
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
$settingsDir = Join-Path $outDir 'ext-settings'
$cloudLog = Join-Path $outDir 'ext-cloud.jsonl'
$panelOut = Join-Path $outDir 'ext-panel.jsonl'
foreach ($p in @($Port, $ApiPort, $CloudPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
Remove-Item $settingsDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $cloudLog, $panelOut, (Join-Path $outDir 'ext-ask.png') -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $settingsDir | Out-Null
python (Join-Path $PSScriptRoot 'ext_test.py') --prepare $settingsDir --cloud-port $CloudPort
$cloud = Start-Process python -ArgumentList @("`"$(Join-Path $PSScriptRoot 'fake_cloud.py')`"", '--port', "$CloudPort", '--log', "`"$cloudLog`"") -PassThru -WindowStyle Hidden `
  -RedirectStandardError (Join-Path $outDir 'ext-cloud.err.log')
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false', '--set', 'llm.cloud.max_tool_calls=10',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'ext-server.out.log') -RedirectStandardError (Join-Path $outDir 'ext-server.err.log')
$code = 1
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 ext-server.err.log' }
  $petLog = Join-Path $desktop 'build\dev\wmpet.log'
  $logFrom = if (Test-Path $petLog) { (Get-Item $petLog).Length } else { 0 }
  $ErrorActionPreference = 'Continue'  # python 往 stderr 写东西 (出错的回溯) 时不要当成 PowerShell 的错误中断
  # 第一段: 内置工具
  $query = 'mute=1&room=1&toolanswers=deny,once,always&asktimeout=5'
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--api-port', "$ApiPort", '--query', "`"$query`"",
    '--settings-dir', "`"$settingsDir`"", '--dry-open', '--no-autohide', '--ask-shot', "`"$(Join-Path $outDir 'ext-ask.png')`"") -PassThru
  python (Join-Path $PSScriptRoot 'ext_test.py') --run --settings-dir $settingsDir --pid $pet.Id --cloud-log $cloudLog --api-port $ApiPort --log-from $logFrom 2>&1 |
    ForEach-Object { "$_" }
  $code = $LASTEXITCODE
  if (-not $pet.WaitForExit(15000)) { $pet.Kill() }
  # 第二段: 面板里的内容包
  $panel = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--query', 'mute=1', '--settings-dir', "`"$settingsDir`"",
    '--panel', '--panel-query', 'exttest=1', '--no-autohide', '--out', "`"$panelOut`"") -PassThru
  python (Join-Path $PSScriptRoot 'ext_test.py') --panel --settings-dir $settingsDir --out $panelOut --pid $panel.Id 2>&1 | ForEach-Object { "$_" }
  if ($LASTEXITCODE) { $code = $LASTEXITCODE }
  if (-not $panel.WaitForExit(15000)) { $panel.Kill() }
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $cloud.Id -Force -ErrorAction SilentlyContinue
}
exit $code
