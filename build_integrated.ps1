param(
    [Parameter(Mandatory = $false)]
    [string]$SourceExe = "",
    [Parameter(Mandatory = $false)]
    [string]$InstallDirectory = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

if (-not $SourceExe) {
    throw "项目内不再保留原版启动器。请用 -SourceExe 指定其他项目中的原版 EXE。"
}
$sourceExe = [IO.Path]::GetFullPath($SourceExe)
$sourceInternal = Join-Path (Split-Path -Parent $sourceExe) "_internal"
$outputRoot = Join-Path $root "dist\大肥鱼桌宠AI整合版"
$outputExe = Join-Path $outputRoot "大肥鱼桌宠.exe"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $sourceExe)) {
    throw "找不到原程序：$sourceExe"
}
if (-not (Test-Path -LiteralPath $sourceInternal)) {
    throw "找不到原程序运行目录：$sourceInternal"
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "首次构建：创建最小 Python 环境..."
    python -m venv (Join-Path $root ".venv")
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
}

$sitePackages = Join-Path $root ".venv\Lib\site-packages"
function Install-PythonPackage([string]$PackageName, [string]$InternalDirectory) {
    $sourcePackage = Join-Path $sitePackages $PackageName
    $targetPackage = Join-Path $InternalDirectory $PackageName
    if (-not (Test-Path -LiteralPath $sourcePackage)) {
        throw "缺少构建依赖：$sourcePackage"
    }
    if (Test-Path -LiteralPath $targetPackage) {
        Remove-Item -LiteralPath $targetPackage -Recurse -Force
    }
    Copy-Item -LiteralPath $sourcePackage -Destination $targetPackage -Recurse -Force
    Get-ChildItem -LiteralPath $targetPackage -Directory -Recurse -Force |
        Where-Object Name -eq "__pycache__" |
        Remove-Item -Recurse -Force
}

$rootFull = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
$outputFull = [IO.Path]::GetFullPath($outputRoot)
if (-not $outputFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "输出目录不在项目工作区内：$outputFull"
}
if (Test-Path -LiteralPath $outputFull) {
    Remove-Item -LiteralPath $outputFull -Recurse -Force
}
New-Item -ItemType Directory -Path $outputFull -Force | Out-Null
Copy-Item -LiteralPath $sourceInternal -Destination (Join-Path $outputFull "_internal") -Recurse

# The original distribution did not import SQLite, so bundle CPython's
# compatible extension and its DLL alongside the original runtime.
Copy-Item -LiteralPath "D:\Python\DLLs\_sqlite3.pyd" -Destination (Join-Path $outputFull "_internal")
Copy-Item -LiteralPath "D:\Python\DLLs\sqlite3.dll" -Destination (Join-Path $outputFull "_internal")
Install-PythonPackage "uiautomation" (Join-Path $outputFull "_internal")
Install-PythonPackage "comtypes" (Join-Path $outputFull "_internal")

& $venvPython ".\tools\patch_original_archive.py" `
    --source $sourceExe `
    --output $outputExe `
    --project-root $root `
    --entry ".\tools\dafeiyu_ai_entry.py"

Copy-Item -LiteralPath ".\README.md" -Destination (Join-Path $outputFull "使用说明.md")
Copy-Item -LiteralPath ".\当前版本功能与更新说明.txt" -Destination $outputFull
Copy-Item -LiteralPath ".\引用说明.txt" -Destination $outputFull
Copy-Item -LiteralPath ".\stickers" -Destination (Join-Path $outputFull "stickers") -Recurse
if ($InstallDirectory) {
    $installRoot = [IO.Path]::GetFullPath($InstallDirectory)
    $installExe = Join-Path $installRoot "大肥鱼桌宠.exe"
    $installInternal = Join-Path $installRoot "_internal"
    Copy-Item -LiteralPath $outputExe -Destination $installExe -Force
    Copy-Item -LiteralPath "D:\Python\DLLs\_sqlite3.pyd" -Destination $installInternal -Force
    Copy-Item -LiteralPath "D:\Python\DLLs\sqlite3.dll" -Destination $installInternal -Force
    Install-PythonPackage "uiautomation" $installInternal
    Install-PythonPackage "comtypes" $installInternal
    Copy-Item -LiteralPath ".\README.md" -Destination (Join-Path $installRoot "使用说明.md") -Force
    Copy-Item -LiteralPath ".\当前版本功能与更新说明.txt" -Destination $installRoot -Force
    Copy-Item -LiteralPath ".\引用说明.txt" -Destination $installRoot -Force
    Copy-Item -LiteralPath ".\stickers" -Destination (Join-Path $installRoot "stickers") -Recurse -Force
}

Write-Host ""
Write-Host "整合版构建完成：$outputExe"
if ($InstallDirectory) {
    Write-Host "独立安装目录已更新：$installRoot"
}
