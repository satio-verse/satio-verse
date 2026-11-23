# QQ 渠道 (6b) 和 wm-guest 自检: 起 8794 的 mock 服务 (不碰 8790 和真实库), 起一个开发版桌宠 (设置用 build\selftest\qq-settings,
# 里面配好 QQ: 她的号 20002、你的号 10000、白名单一个测试群、SnowLuma 目录是 build\selftest\qq-snowluma 里的替身配置),
# 再用 qq_test.py 起替身 OneBot (3911, 不连真 QQ) 测: 面板设置 -> 页面 -> 服务端 -> 替身, 草稿卡片自动回答 (qqanswers), 主人私聊,
# wm-guest 和访客 token. 测试期间屏幕右下角会多一个桌宠 (不出声), 测完就关.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [int]$Port = 8794,     # mock 服务
  [int]$ApiPort = 8795,  # 测试桌宠的本机接口
  [int]$OneBot = 3911    # 替身 OneBot
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$exe = Join-Path $desktop 'build\dev\bin\wmpet.exe'
$outDir = Join-Path $desktop 'build\selftest'
New-Item -ItemType Directory -Force $outDir | Out-Null
foreach ($p in @($Port, $ApiPort, $OneBot)) {
  if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { throw "端口 $p 已被占用" }
}
$token = $env:QQ_TOKEN
$utf8 = New-Object System.Text.UTF8Encoding $false  # 不带 BOM (宿主的 JSON 解析不认 BOM)
# 替身 SnowLuma 目录: 只放它的 OneBot 配置, 服务端每次连接时从这里读 token
$sl = Join-Path $outDir 'qq-snowluma'
Remove-Item $sl -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $sl 'config') | Out-Null
[IO.File]::WriteAllText((Join-Path $sl 'config\onebot_20002.json'),
  "{`"wsServers`":[{`"name`":`"ws-default`",`"host`":`"127.0.0.1`",`"port`":$OneBot,`"accessToken`":`"$token`"}],`"statusCommand`":{`"enabled`":false}}", $utf8)
# 测试桌宠的设置: 默认预设 (本地模型) + QQ 段
$settings = Join-Path $outDir 'qq-settings'
Remove-Item $settings -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $settings | Out-Null
$qq = @{ enabled = $true; snowluma_dir = ($sl -replace '\\', '/'); self_id = 20002; owner_id = 10000; names = @('小流'); persona = '';
  convs = @(@{ type = 'group'; id = 111; name = '测试群' }); block = @(); group_send = 'draft'; private_send = 'draft';
  owner_send = 'auto'; daily_tokens = 0; paused = $false
  # 她自己开口: 只查设置有没有经宿主、页面到服务端 (说说的时段在两小时后, 主动找你要隔 72 小时, 测试期间都不会真发)
  proactive = @{ enabled = $true; idle_h = 72; tick_s = 0.5 }
  qzone = @{ enabled = $true; per_day = 1; tick_s = 0.5; window = ((Get-Date).AddHours(2).ToString('HH:mm') + '-' + (Get-Date).AddHours(3).ToString('HH:mm')) } }
[IO.File]::WriteAllText((Join-Path $settings 'pet-settings.json'), (@{ version = 1; qq = $qq } | ConvertTo-Json -Depth 6), $utf8)

# 临时记忆库 (每次清空): 看 QQ 和 wm-guest 的渠道日志落没落盘, 不碰你真正的库
$vault = Join-Path $outDir 'qq-vault'
Remove-Item $vault -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $vault | Out-Null
$serverArgs = @('-m', 'wm', '--port', "$Port", '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'none',
  '--set', 'face.engine=none', '--set', "memory.vault=$($vault -replace '\\', '/')", '--set', 'memory.embed.engine=none',
  '--set', 'memory.graph_notes=false', '--set', 'llm.prewarm=false', '--set', 'channels.qq.group.min_gap_s=0',
  '--set', "channels.qq.endpoint=ws://127.0.0.1:$OneBot/",
  '--set', "trace.log_dir=$(($outDir + '\qq-logs') -replace '\\', '/')")
$server = Start-Process python -ArgumentList $serverArgs -WorkingDirectory $project -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $outDir 'qq-server.out.log') -RedirectStandardError (Join-Path $outDir 'qq-server.err.log')
$pet = $null
try {
  $up = $false
  for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 2 | Out-Null; $up = $true }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { throw 'mock 服务没起来, 见 qq-server.err.log' }
  # 草稿的自动回答: 发 / 改成"改过的话" / 不发 (查询参数里的中文先编码)
  $answers = 'send,edit:' + [Uri]::EscapeDataString('改过的话') + ',drop'
  # --no-autohide: 用户开着全屏程序时测试桌宠也不藏 (藏起来时页面的计时器被压, 草稿卡片的自动回答 20 秒内点不上)
  $pet = Start-Process $exe -ArgumentList @('--server', "http://127.0.0.1:$Port", '--api', '--no-autohide', '--api-port', "$ApiPort",
    '--query', "mute=1&qqanswers=$answers", '--settings-dir', "`"$settings`"", '--ask-shot', "`"$(Join-Path $outDir 'qq-draft.png')`"") -PassThru
  python (Join-Path $PSScriptRoot 'qq_test.py') --port $ApiPort --server $Port --onebot $OneBot --token $token --vault $vault `
    --log (Join-Path $desktop 'build\dev\wmpet.log') --pet $pet.Id
  $code = $LASTEXITCODE
} finally {
  if ($pet -and -not $pet.HasExited -and -not $pet.WaitForExit(10000)) { $pet.Kill() }
  Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
exit $code

