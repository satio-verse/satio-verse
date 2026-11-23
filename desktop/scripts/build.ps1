# 构建桌宠宿主: 用 CPEditor 自带的 MinGW-w64 (gcc 13.2 + cmake + ninja). 路径写死, 以后装了 MSVC 也不会混用.
# 编到 desktop/build/dev/bin (自检用这一份, 日志和 WebView2 数据都在 build/dev 下, 不碰用户正在用的那份),
# 成功后发布到 desktop/build/bin (start-pet.cmd 用的那份). 用户的桌宠正在运行时 exe 被占用, 跳过发布并提示.
# (这个文件带 UTF-8 BOM: Windows PowerShell 5.1 读没有 BOM 的脚本会按 GBK 解码, 中文会乱)
param([string]$Config = 'Release', [switch]$NoPublish)
$ErrorActionPreference = 'Stop'
$mg = 'E:\Program Files (x86)\CPEditor\mingw64\bin'
$env:PATH = "$mg;$env:PATH"
$desktop = Split-Path -Parent $PSScriptRoot
$build = Join-Path $desktop 'build\cmake'
$m = $mg -replace '\\', '/'
& "$mg\cmake.exe" -S (Join-Path $desktop 'host') -B $build -G Ninja "-DCMAKE_BUILD_TYPE=$Config" `
  "-DCMAKE_CXX_COMPILER=$m/g++.exe" "-DCMAKE_RC_COMPILER=$m/windres.exe" "-DCMAKE_MAKE_PROGRAM=$m/ninja.exe"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& "$mg\cmake.exe" --build $build
if ($LASTEXITCODE) { exit $LASTEXITCODE }
if ($NoPublish) { exit 0 }

$dev = Join-Path $desktop 'build\dev\bin'
$bin = Join-Path $desktop 'build\bin'
New-Item -ItemType Directory -Force $bin | Out-Null
$running = Get-Process wmpet -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $bin 'wmpet.exe') }
if ($running) {
  Write-Host "桌宠正在运行 (build\bin\wmpet.exe), 没有发布新版本; 退出桌宠后再运行一次 build.ps1."
  exit 0
}
Copy-Item (Join-Path $dev 'wmpet.exe'), (Join-Path $dev 'WebView2Loader.dll') $bin -Force
Write-Host "已发布到 build\bin"
exit 0
