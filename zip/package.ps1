# 打便携包：后端 PyInstaller + 前端 dotnet publish，组装后压成 zip。
#
#   powershell -File zip\package.ps1                    # 默认版本号 0.1.0
#   powershell -File zip\package.ps1 -Version 0.2.0
#   powershell -File zip\package.ps1 -SkipBackend       # 后端源码没改过时才这么用；
#                                                        # 否则会把旧后端打进包（踩过一次）
#
# 产物：
#   release\rag-lite-<版本>\        解包后的目录（可直接双击 RagLite.exe）
#   release\rag-lite-<版本>.zip     便携包（整包拷走即可用，数据跟着 data\ 走）
#
# 注意：包里**不含** .env（密钥），只放 .env.example。

param(
    [string]$Version = "0.1.0",
    [switch]$SkipBackend,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8  # 让中文输出别乱码
$root = Split-Path -Parent $PSScriptRoot          # 仓库根
$releaseRoot = Join-Path $root "release"
$appDir = Join-Path $releaseRoot "rag-lite-$Version"
$publishDir = Join-Path $root "publish\frontend"
$backendDist = Join-Path $root "zip\dist_backend\raglite-backend"

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }

# 1) 后端：PyInstaller 打成 onedir
if (-not $SkipBackend) {
    Step "打包后端（PyInstaller）"
    & uv run --project (Join-Path $root "backend") python (Join-Path $root "zip\build_backend.py")
    if ($LASTEXITCODE -ne 0) { throw "后端打包失败（退出码 $LASTEXITCODE）" }
}
if (-not (Test-Path $backendDist)) { throw "没找到后端产物：$backendDist（先跑 zip\build_backend.py）" }

# 2) 前端：自包含发布（不裁剪——ViewLocator 用反射找视图，裁剪会出事）
Step "发布前端（self-contained win-x64）"
# 单文件 / R2R / 不裁剪 / 嵌入符号等发布选项都写在 frontend.csproj 里，这里不重复传
dotnet publish (Join-Path $root "frontend\frontend.csproj") -c Release -o $publishDir
if ($LASTEXITCODE -ne 0) { throw "前端发布失败（退出码 $LASTEXITCODE）" }

# 2.5) 清掉原生库的调试符号：SkiaSharp/HarfBuzzSharp 的 .pdb 加起来 100MB+，
#      对我们的排错没用（我们自己的符号已经嵌进 RagLite.exe），白占体积
$pdbs = Get-ChildItem $publishDir -Filter *.pdb -File -ErrorAction SilentlyContinue
if ($pdbs) {
    $pdbMb = [math]::Round(($pdbs | Measure-Object Length -Sum).Sum / 1MB, 1)
    $pdbs | Remove-Item -Force
    Write-Host ("已移除 {0} 个 .pdb（{1}MB）" -f $pdbs.Count, $pdbMb)
}

# 3) 组装便携目录
Step "组装 $appDir"
if (Test-Path $appDir) { Remove-Item $appDir -Recurse -Force }
New-Item -ItemType Directory -Path $appDir | Out-Null
Copy-Item (Join-Path $publishDir "*") $appDir -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $appDir "backend") | Out-Null
Copy-Item (Join-Path $backendDist "*") (Join-Path $appDir "backend") -Recurse -Force
Copy-Item (Join-Path $root "backend\.env.example") $appDir -Force
Copy-Item (Join-Path $root "zip\使用说明.md") $appDir -Force
New-Item -ItemType Directory -Path (Join-Path $appDir "data") | Out-Null

$files = (Get-ChildItem $appDir -Recurse -File)
$totalMb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host ("解包后：{0} 个文件，{1}MB" -f $files.Count, $totalMb)

if ($NoZip) { Write-Host "`n跳过压缩（-NoZip）"; exit 0 }

# 4) 压缩（用 ZipFile，比 Compress-Archive 快且省内存）
Step "压缩便携包"
$zip = Join-Path $releaseRoot "rag-lite-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $appDir, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)

$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "`n便携目录：$appDir"
Write-Host "便携压缩包：$zip（$zipMb MB）" -ForegroundColor Green
