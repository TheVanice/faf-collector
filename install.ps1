# Installs the `faf` command of the Fleet Analytics collector (Windows).
#
#   irm https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.ps1 | iex
#
# Downloads a release, checks its SHA-256, copies it to %USERPROFILE%\.faf\cli\<version> and puts
# %USERPROFILE%\.faf\bin on your user PATH. Needs Python 3.9+ as `python` (the hooks call it).
# $env:FAF_VERSION = "v1.0.0" installs that version. Remove with: faf uninstall
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$releases = if ($env:FAF_RELEASES_URL) { $env:FAF_RELEASES_URL.TrimEnd('/') } else { 'https://github.com/TheVanice/faf-collector/releases' }

$py = $null
foreach ($c in 'python', 'python3') {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        & $c -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $c; break }
    }
}
if (-not $py) { throw "Python 3.9 or newer is required as 'python' on PATH: https://www.python.org/downloads/" }

$tag = $env:FAF_VERSION
if (-not $tag) {
    # .../releases/latest redirects to .../releases/tag/vX.Y.Z
    $tag = & $py -c "import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1] + '/latest', timeout=60).geturl().rstrip('/').rsplit('/', 1)[-1])" $releases
    if ($LASTEXITCODE -ne 0 -or $tag -notlike 'v*') { throw "Could not find the latest release at $releases" }
}
if ($tag -notlike 'v*') { $tag = "v$tag" }

$name = "faf-collector-$tag.zip"
$temp = Join-Path ([IO.Path]::GetTempPath()) ("faf-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    Write-Host "Downloading faf $tag"
    $zip = Join-Path $temp $name
    Invoke-WebRequest -UseBasicParsing -Uri "$releases/download/$tag/$name" -OutFile $zip
    Invoke-WebRequest -UseBasicParsing -Uri "$releases/download/$tag/$name.sha256" -OutFile "$zip.sha256"
    $expected = ((Get-Content "$zip.sha256" -Raw).Trim() -split '\s+')[0].ToLower()
    $actual = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($expected -ne $actual) { throw "Checksum mismatch for ${name}: expected $expected, got $actual" }
    Expand-Archive -Path $zip -DestinationPath (Join-Path $temp 'unpacked')
    & $py (Join-Path $temp 'unpacked\faf\faf_cli.py') self-install --python $py
    if ($LASTEXITCODE -ne 0) { throw "faf self-install failed" }
    $bin = Join-Path $env:USERPROFILE '.faf\bin'
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }  # this session too
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
