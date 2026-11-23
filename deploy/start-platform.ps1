param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$PublicUrl,
    [string]$Python = 'python',
    [int]$Port = 8792
)
$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$PrivateRoot = Join-Path $DataRoot 'private'
$ReceiptPath = Join-Path $PrivateRoot 'receipt-secret.txt'
$AuthorsPath = Join-Path $PrivateRoot 'authors.json'
if (!(Test-Path -LiteralPath $ReceiptPath) -or !(Test-Path -LiteralPath $AuthorsPath)) {
    throw '请先用 tools/platform_admin.py 初始化 private 目录与作者令牌。'
}
$env:WM_RECEIPT_SECRET = (Get-Content -LiteralPath $ReceiptPath -Raw).Trim()
try {
    & $Python (Join-Path $PackageRoot 'tools/community_server.py') --home (Join-Path $DataRoot 'store') --public-url $PublicUrl --authors $AuthorsPath --port $Port
    if ($LASTEXITCODE -ne 0) { throw "服务退出，代码 $LASTEXITCODE" }
} finally {
    Remove-Item Env:WM_RECEIPT_SECRET -ErrorAction SilentlyContinue
}
