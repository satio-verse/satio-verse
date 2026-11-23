# 内存实验: 每个情形跑一次 selftest.ps1 (hwnd:layered, 默认只测 15 帧这一阶段), 最后打印对比表.
# 情形写成 "名字|页面参数|宿主参数", 例如 "压缩变形|morph=compact|" 或 "内存目标低||--memory-level low".
# 每个情形的原始结果存成 desktop/build/selftest/exp-<名字>.jsonl.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param(
  [string[]]$Cases = @(
    '基线|tex=1024&morph=keep&free=0|',
    '只加载不渲染|render=0|',
    '变形压缩|morph=compact|',
    '变形压缩去法线|morph=compact-nonormal|',
    '加释放原图|morph=compact-nonormal&free=1|',
    '加贴图512|morph=compact-nonormal&free=1&tex=512|',
    '加内存目标低|morph=compact-nonormal&free=1|--memory-level low',
    '加关MSAA像素比1|morph=compact-nonormal&free=1&aa=0&pr=1|'
  ),
  [string]$Phases = '15',
  [int]$MeasureSeconds = 10
)
$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $desktop 'build\selftest'
$selftest = Join-Path $PSScriptRoot 'selftest.ps1'
$rows = @()
foreach ($case in $Cases) {
  $name, $query, $hostArgs = $case.Split('|')
  $extra = @()
  if ($hostArgs) { $extra = $hostArgs.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries) }
  & $selftest -Configs 'hwnd:layered' -MeasureConfigs 'hwnd:layered' -Phases $Phases -MeasureSeconds $MeasureSeconds `
    -PageQuery $query -ExtraArgs $extra | Out-Null
  $file = Join-Path $dir "exp-$name.jsonl"
  Copy-Item (Join-Path $dir 'results.jsonl') $file -Force
  $r = Get-Content $file -Encoding UTF8 | Select-Object -First 1 | ConvertFrom-Json
  foreach ($u in $r.usage) {
    $gpuProc = $u.detail | Sort-Object gpu_shared_mb -Descending | Select-Object -First 1
    $render = $u.detail | Where-Object { $_.pid -ne $gpuProc.pid -and $_.name -ne 'wmpet.exe' } |
      Sort-Object private_mb -Descending | Select-Object -First 1
    $rows += [pscustomobject]@{
      情形 = $name; 阶段 = $u.phase; 透明点穿 = $r.ok; CPU = $u.cpu_pct; 核显3D = $u.gpu_3d_pct
      私有MB = $u.private_mb; 核显共享MB = $u.shared_gpu_mb; GPU进程MB = $gpuProc.private_mb; 渲染进程MB = $render.private_mb
      页面 = $u.page_stats
    }
  }
}
$rows | Format-Table -AutoSize | Out-String -Width 300
