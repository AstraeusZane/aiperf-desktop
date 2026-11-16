# 构建期准备内置引擎（需联网；产物运行期完全离线）
#   src-tauri/resources/engine/
#     python/        便携 CPython（python-build-standalone）+ aiperf 及全部依赖
#     app.py         本地 Web 引擎
#     static/        WebUI
#     tokenizers/    内置 tokenizer（默认 Qwen）
param(
    [string]$PyMinor = "3.12",
    [string]$AiperfVersion = "0.13.0"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "src-tauri/resources/engine"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

# ---- 1. 下载便携 Python ----
$headers = @{ "User-Agent" = "aiperf-desktop-build" }
if ($env:GITHUB_TOKEN) { $headers.Authorization = "Bearer $env:GITHUB_TOKEN" }
$rel = Invoke-RestMethod -Headers $headers "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
$minor = [regex]::Escape($PyMinor)
$asset = $rel.assets |
    Where-Object { $_.name -match "^cpython-$minor\.\d+\+\d+-x86_64-pc-windows-msvc-install_only(_stripped)?\.tar\.gz$" } |
    Sort-Object { if ($_.name -like "*_stripped*") { 0 } else { 1 } } |
    Select-Object -First 1
if (-not $asset) { throw "未找到 Python $PyMinor 的 Windows 便携包（release $($rel.tag_name)）" }
Write-Host "Python 包: $($asset.name)"

$tmpDir = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$tgz = Join-Path $tmpDir "python-standalone.tar.gz"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tgz
tar -xzf $tgz -C $out
if ($LASTEXITCODE -ne 0) { throw "解压 Python 失败" }

$py = Join-Path $out "python/python.exe"
& $py --version

# ---- 2. 安装 aiperf（仅接受预编译 wheel 优先） ----
& $py -m pip install --no-cache-dir --disable-pip-version-check --prefer-binary "aiperf==$AiperfVersion"
if ($LASTEXITCODE -ne 0) { throw "pip 安装 aiperf 失败" }

& $py -c "import aiperf, transformers, zmq, crick; print('import OK: aiperf', __import__('importlib.metadata').metadata.version('aiperf'))"
if ($LASTEXITCODE -ne 0) { throw "aiperf 导入失败" }
& $py -m aiperf profile --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "python -m aiperf 无法运行" }

# ---- 3. 拷贝引擎文件 ----
Copy-Item (Join-Path $root "engine/app.py") $out
Copy-Item (Join-Path $root "engine/static") $out -Recurse
Copy-Item (Join-Path $root "engine/tokenizers") $out -Recurse

# ---- 4. 精简 ----
Get-ChildItem $out -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
$count = (Get-ChildItem $out -Recurse -File).Count
Write-Host ("引擎目录就绪: {0:N0} MB, {1} 个文件" -f $size, $count)
