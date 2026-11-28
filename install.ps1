# Installs the `faf` command of the Fleet Analytics collector (Windows).
#
#   irm https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.ps1 | iex
#
# Downloads a release, checks its signed SHA-256 (key in faf_verify.py), copies it to
# %USERPROFILE%\.faf\cli\<version> and puts %USERPROFILE%\.faf\bin on your user PATH. Needs Python 3.9+ as `python` (the hooks call it).
# $env:FAF_VERSION = "v1.0.0" installs that version. Remove with: faf uninstall
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$releases = if ($env:FAF_RELEASES_URL) { $env:FAF_RELEASES_URL.TrimEnd('/') } else { 'https://github.com/TheVanice/faf-collector/releases' }
if ($releases -notmatch '^https://[^/]' -and $releases -notmatch '^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)') {
    throw "FAF_RELEASES_URL must use https (http only for localhost): $releases"
}
if ($releases -ne 'https://github.com/TheVanice/faf-collector/releases') { Write-Host "Using releases from $releases (FAF_RELEASES_URL)" }

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

# faf_verify.py, verbatim: the release key signs the checksum, so a replaced zip is refused.
$fafVerify = @'
"""Check a release zip against its signed checksum (standard library only).

release.yml signs the zip's .sha256 file (sha256sum output: hash, two spaces, zip name) with an
Ed25519 key held as a secret of the public repository; PUBLIC_KEY is its public half. A release
counts only if the signature over the checksum file is valid, the file names this zip, and the
zip has that hash. install.sh and install.ps1 embed this file verbatim (a test keeps them equal).

    python faf_verify.py <zip> <zip.sha256> <zip.sha256.sig>
"""
import base64
import hashlib
import os
import sys

# openssl pkey -pubout of the release signing key (SubjectPublicKeyInfo, Ed25519).
PUBLIC_KEY = "MCowBQYDK2VwAyEAW4zZWhbxvoXB2CMjCnbDNo2A53vLrYD5yj/8pTlke0M="
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")

# Ed25519 verification after RFC 8032, section 6 (points in extended coordinates).
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, point):
    out = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            out = _add(out, point)
        point = _add(point, point)
        s >>= 1
    return out


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    return _P - x if (x & 1) != sign else x


def _decompress(raw):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = 15112221349535400772501151409588531511454012693041857206046113283949847762202  # _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def ed25519_verify(public, message, signature):
    """True if signature is a valid Ed25519 signature of message under the 32-byte public key."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(k, a)))


def public_key_bytes(pem_base64=None):
    der = base64.b64decode(PUBLIC_KEY if pem_base64 is None else pem_base64)
    if len(der) != 44 or der[:12] != _SPKI_PREFIX:
        raise ValueError("not an Ed25519 public key")
    return der[12:]


def check_release(name, data, checksum, signature, pem_base64=None):
    """None if the release is good, else the reason it is not."""
    if not signature:
        return f"{name} has no signature (.sha256.sig)"
    if not ed25519_verify(public_key_bytes(pem_base64), checksum, signature):
        return f"the signature of {name}.sha256 is not valid"
    fields = checksum.decode("ascii", "replace").split()
    if len(fields) != 2 or fields[1].lstrip("*") != name:
        return f"the signed checksum is not for {name}"
    actual = hashlib.sha256(data).hexdigest()
    if fields[0].lower() != actual:
        return f"checksum mismatch for {name}: expected {fields[0].lower()}, got {actual}"
    return None


def main(argv):
    if len(argv) != 3:
        print("usage: faf_verify.py <zip> <zip.sha256> <zip.sha256.sig>", file=sys.stderr)
        return 2
    contents = []
    for path in argv:
        with open(path, "rb") as f:
            contents.append(f.read())
    problem = check_release(os.path.basename(argv[0]), *contents)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'@

$name = "faf-collector-$tag.zip"
$temp = Join-Path ([IO.Path]::GetTempPath()) ("faf-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    Write-Host "Downloading faf $tag"
    $zip = Join-Path $temp $name
    Invoke-WebRequest -UseBasicParsing -Uri "$releases/download/$tag/$name" -OutFile $zip
    Invoke-WebRequest -UseBasicParsing -Uri "$releases/download/$tag/$name.sha256" -OutFile "$zip.sha256"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$releases/download/$tag/$name.sha256.sig" -OutFile "$zip.sha256.sig"
    } catch { throw "$name has no signature (.sha256.sig). Nothing installed." }
    $verify = Join-Path $temp 'faf_verify.py'
    [IO.File]::WriteAllText($verify, $fafVerify)
    & $py $verify $zip "$zip.sha256" "$zip.sha256.sig"
    if ($LASTEXITCODE -ne 0) { throw "Nothing installed." }
    Expand-Archive -Path $zip -DestinationPath (Join-Path $temp 'unpacked')
    & $py (Join-Path $temp 'unpacked\faf\faf_cli.py') self-install --python $py
    if ($LASTEXITCODE -ne 0) { throw "faf self-install failed" }
    $bin = Join-Path $env:USERPROFILE '.faf\bin'
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$env:Path;$bin" }  # this session too
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
