param(
    [string]$ModelProfile,
    [int]$Port = 48717,
    [switch]$Setup,
    [switch]$EnableOpenHands
)
$ErrorActionPreference = 'Stop'
$rootDir = $PSScriptRoot
Set-Location -LiteralPath $rootDir
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:UV_CACHE_DIR = Join-Path $rootDir '.lb/cache'
$env:TEMP = Join-Path $rootDir '.lb/tmp'
$env:TMP = $env:TEMP
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:UV_LINK_MODE = 'copy'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $rootDir '.lb/browsers'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$venv = Join-Path $rootDir '.lb/env'
$interpreter = Join-Path $venv 'Scripts/python.exe'
if ($Setup -or !(Test-Path -LiteralPath $interpreter)) {
    if (!(Test-Path -LiteralPath $interpreter)) {
        uv venv --python 3.12 $venv
        if ($LASTEXITCODE -ne 0) { throw '无法建立 Python 3.12 环境' }
    }
    uv pip sync --python $interpreter --require-hashes '工程依赖/coding-win.lock.txt'
    if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成' }
    & $interpreter -B -X utf8 'src/lightbrain/host/sandbox.py' install-bubblewrap
    if ($LASTEXITCODE -ne 0) { throw '验收沙箱安装未完成' }
}
if ($ModelProfile) { $env:LB_MODEL_ENV = (Resolve-Path -LiteralPath $ModelProfile).Path }
elseif (!$env:LB_MODEL_ENV -and (Test-Path -LiteralPath '.env')) { $env:LB_MODEL_ENV = (Resolve-Path '.env').Path }
if ($EnableOpenHands) {
    $env:LB_ENABLE_OPENHANDS = '1'
    $env:LB_OPENHANDS_BACKEND = 'bubblewrap'
    $env:PYTHONPATH = Join-Path $rootDir 'src'
    & $interpreter -B -m lightbrain.host.openhands_setup
    if ($LASTEXITCODE -ne 0) { throw 'OpenHands 执行环境未就绪' }
}
& $interpreter -B -X utf8 lb.py serve --with-worker --url "http://127.0.0.1:$Port"
exit $LASTEXITCODE
