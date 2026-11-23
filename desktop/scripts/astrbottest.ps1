# AstrBot 接 wm-guest 的自检 (方案 docs/roadmap/astrbot-wm-guest.md 第 8 节): 起 8794 的 mock 服务 (临时库, 不碰 8790 和真实库)、
# 一个开发版测试桌宠 (本机接口 8795, 设置在 build\selftest\astrbot-settings), 用 astrbot_lock.py 把 AstrBot 的模型临时指到测试桌宠,
# 再由 astrbot_test.py 起 AstrBot (启动器装的实例, WebUI 6185 只听本机) 用它的 HTTP API 说话. 测完把 AstrBot 指回 8793 (你的桌宠).
# AstrBot 要先在启动器里停掉. 测试期间屏幕右下角会多一个桌宠 (不出声), 测完就关.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,      # mock 服务
  [int]$ApiPort = 8795,   # 测试桌宠的本机接口
  [int]$DashPort = 6185,  # AstrBot 的 WebUI 和 HTTP API
  [string]$Core = ''      # AstrBot 实例的 core 目录; 不填就找启动器目录里唯一的那个实例
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
New-Item -ItemType Directory -Force $outDir | Out-Null
if (-not $Core) {
  $found = @(Get-ChildItem (Join-Path $env:USERPROFILE '.astrbot_launcher\instances\*\core\data\cmd_config.json') -ErrorAction SilentlyContinue)
  if ($found.Count -ne 1) { throw "启动器目录里有 $($found.Count) 个 AstrBot 实例, 用 -Core 指定一个" }
  $Core = Split-Path -Parent (Split-Path -Parent $found[0].FullName)
}
foreach ($p in @($Port, $ApiPort, $DashPort)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用 (AstrBot 要先在启动器里停掉)" }
}
$utf8 = New-Object System.Text.UTF8Encoding $false  # 不带 BOM (宿主的 JSON 解析不认 BOM)
# 清空上次的目录: 有时被别的程序占着删不掉 (以前静默失败, 拿着上次的日志测), 重试几次, 还删不掉就停
function Clear-Dir([string]$dir) {
  for ($i = 0; $i -lt 10 -and (Test-Path $dir); $i++) {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $dir) { Start-Sleep -Milliseconds 500 }
  }
  if (Test-Path $dir) { throw "清不掉 $dir (有程序占着?)" }
}
# 测试桌宠的设置: 默认预设, 不配 QQ (wm-guest 用 config 里的 channels.qq 当成关着的)
$settings = Join-Path $outDir 'astrbot-settings'
Clear-Dir $settings
New-Item -ItemType Directory -Force $settings | Out-Null
[IO.File]::WriteAllText((Join-Path $settings 'pet-settings.json'), '{"version":1}', $utf8)
# 临时记忆库 (每次清空): 主人的"住处"故意写成 mock 某句回复里的词, 用来测外发检查拦下
$vault = Join-Path $outDir 'astrbot-vault'
Clear-Dir $vault
New-Item -ItemType Directory -Force (Join-Path $vault '小流\记忆') | Out-Null
[IO.File]::WriteAllText((Join-Path $vault '小流\记忆\关于用户.md'),
  "# 关于用户`n`n- 用户住在替你开心 (2026-10-01) #事实 #auto-memory`n", $utf8)

$data = Join-Path $Core 'data'
python (Join-Path $PSScriptRoot 'astrbot_lock.py') --data $data --api-port $ApiPort
if ($LASTEXITCODE -ne 0) { throw 'AstrBot 没锁上, 不测' }
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', "memory.vault=$($vault -replace '\\', '/')", '--set', 'memory.embed.engine=none',
  '--set', 'memory.graph_notes=false', '--set', 'llm.prewarm=false',
  '--set', 'channels.qq.private.min_gap_s=0', '--set', 'channels.qq.private.per_hour=9',
  '--set', "trace.log_dir=$(($outDir + '\astrbot-logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'astrbot-server.out.log') -RedirectStandardError (Join-Path $outDir 'astrbot-server.err.log')
$pet = $null
$code = 1
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 astrbot-server.err.log' }
  # 故意不加 --no-autohide: 你开着全屏程序时测试桌宠会自己藏起来, 正好测到"桌宠隐藏时 wm-guest 不卡"
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--api-port', "$ApiPort",
    '--query', 'mute=1', '--settings-dir', "`"$settings`"") -PassThru
  python (Join-Path $PSScriptRoot 'astrbot_test.py') --core $Core --dash-port $DashPort --port $ApiPort --server $Port `
    --vault $vault --out (Join-Path $outDir 'astrbot.out.log') --pet $pet.Id
  $code = $LASTEXITCODE
} finally {
  if ($pet -and -not $pet.HasExited -and -not $pet.WaitForExit(10000)) { $pet.Kill() }
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
  # 指回你的桌宠 (本机接口默认 8793)
  python (Join-Path $PSScriptRoot 'astrbot_lock.py') --data $data
}
exit $code
