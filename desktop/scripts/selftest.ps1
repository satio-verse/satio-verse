# M0 自检: 起一个 8792 端口的 mock 服务 (LLM/TTS 用 mock, 不开 ASR、面部和记忆库; 不碰 8790 和真实库),
# 按几种 "宿主方式:点穿方式" 组合各跑一次 wmpet --selftest, 结果写到 desktop/build/selftest/results.jsonl.
# 自检时屏幕中间会出现桌宠和它下面的棋盘格 (自检自己画的), 每个组合十几秒; 截图只截这一块.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  # 桌宠自检专用 8794: 8792 是各会话测试常用的 mock 端口, 别的会话可能正在用
  [int]$Port = 8794,
  [string[]]$Configs = @('hwnd:layered', 'hwnd:toggle', 'hwnd:plain', 'comp:layered', 'comp:toggle'),
  [string[]]$MeasureConfigs = @('hwnd:layered'),
  [string]$Phases = 'hidden,0,15,30,60',
  [int]$MeasureSeconds = 10,
  [int]$MeasureFilm = 0,     # 测资源的每个阶段 (隐藏除外) 切过去时先连拍几秒再测: 看低帧率、隐藏后回来的样子; 帧在 film-measure-<序号>-<阶段>\
  [int]$MeasureFilmMs = 50,  # 上面那段多久截一张
  [string]$PageQuery = '',   # 例如 'tex=0': 贴图不限尺寸 (对比用)
  [string]$Asr = 'none',     # mock 服务的语音识别: none, 或 vad_sensevoice (语音自检用, CPU 上跑)
  [string[]]$ExtraArgs = @(), # 额外传给 wmpet 的参数, 例如 '--memory-level','low'
  [string[]]$ServerSet = @() # 额外的 mock 服务配置 (--set), 例如 'room.think.min_interval_s=0'
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'  # 开发版: 不碰用户正在用的 build\bin; 日志在 build\dev\wmpet.log
$outDir = Join-Path $desktop 'build\selftest'
New-Item -ItemType Directory -Force $outDir | Out-Null
$results = Join-Path $outDir 'results.jsonl'
Remove-Item $results -ErrorAction SilentlyContinue
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  throw "端口 $Port 已被占用, 不在别人的服务上测"
}

# 延迟日志和社交后果日志 (trace.log_dir) 也指到自检目录: 默认的 logs/ 是真实服务和阶段一评测在用的, 自检的对话不能混进去
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', $Asr, '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', 'memory.vault=', '--set', 'llm.prewarm=false',
  '--set', "trace.log_dir=$(($outDir + '\logs') -replace '\\', '/')",
  # 工作台数据也指到自检目录 (2026-11-07): 不设的话用真的 ~/.worldmodel/agent, 和 8790 抢持续任务调度器的锁 ("已有持续任务调度器使用此数据目录", 起不来)
  '--set', "agent.home=$(($outDir + '\agent') -replace '\\', '/')")
foreach ($s in $ServerSet) { $serverArgs += @('--set', $s) }
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'server.out.log') -RedirectStandardError (Join-Path $outDir 'server.err.log')
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 server.err.log' }
  foreach ($c in $Configs) {
    $mode, $ct = $c.Split(':')
    # 设置用一个空目录 (默认预设): 读用户真正的设置的话, 选了云端预设时自检的对话会发到真实的云端模型
    $a = @('--server', "http://127.0.0.1:$Port", '--host', $mode, '--ct', $ct, '--selftest', '--out', $results,
      '--settings-dir', "`"$(Join-Path $outDir 'selftest-settings')`"")
    if ($MeasureConfigs -contains $c) {
      $a += @('--measure', $Phases, '--measure-s', "$MeasureSeconds")
      if ($MeasureFilm -gt 0) { $a += @('--measure-film', "$MeasureFilm", '--measure-film-ms', "$MeasureFilmMs") }
    }
    if ($PageQuery) { $a += @('--query', $PageQuery) }
    if ($ExtraArgs) { $a += $ExtraArgs }
    $p = Start-Process $exe -ArgumentList $a -PassThru
    if (-not $p.WaitForExit(300000)) { $p.Kill(); Write-Warning "$c 超时" }
    Write-Host "$c 退出码 $($p.ExitCode)"
  }
} finally {
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  # 自检设置用完就清掉 (界面演示会在里面换人设预设, 留着的话下一次自检会按它换门面)
  Remove-Item (Join-Path $outDir 'selftest-settings') -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $results) { Get-Content $results -Encoding UTF8 }
