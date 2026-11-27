param(
    [Parameter(Mandatory)][string]$GameRoot,
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'STS2WebCoop'),
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$Port = 8088,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$GameRoot = (Resolve-Path -LiteralPath $GameRoot).Path
$references = Join-Path $GameRoot 'data_sts2_windows_x86_64'
$release = Get-Content -LiteralPath (Join-Path $GameRoot 'release_info.json') -Raw | ConvertFrom-Json
if ($release.version -notin @('v0.107.1', '0.107.1')) {
    throw "This bridge needs game version 0.107.1. Found $($release.version)."
}
foreach ($name in @('sts2.dll', 'GodotSharp.dll', '0Harmony.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $references $name) -PathType Leaf)) {
        throw "Missing game build reference: $name"
    }
}
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$StateRoot = (Resolve-Path -LiteralPath $StateRoot).Path
$venv = Join-Path $StateRoot 'venv'
$runtime = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE) { throw 'Unable to create the Python environment.' }
}
& $runtime -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE) { throw 'Unable to install the lobby dependencies.' }

$build = Join-Path $StateRoot 'bridge'
& dotnet build (Join-Path $PSScriptRoot 'Bridge\Bridge.csproj') -c Release -o $build "-p:GameReferenceDir=$references"
if ($LASTEXITCODE) { throw 'Unable to build the game bridge.' }

$env:WEB_BRIDGE_DLL = Join-Path $build 'Bridge.dll'
$env:GAME_ROOT = $GameRoot
$env:STATE_DIR = $StateRoot
$env:BIND_HOST = $BindAddress
$env:PORT = [string]$Port
$url = [UriBuilder]::new('http', $BindAddress, $Port).Uri.AbsoluteUri
Write-Host "Lobby: $url"
Write-Host "Access password file: $(Join-Path $StateRoot 'access-password.txt')"
& $runtime -u (Join-Path $PSScriptRoot 'app.py')
exit $LASTEXITCODE
