#!/bin/sh
# Installs the `faf` command of the Fleet Analytics collector (macOS, Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.sh | sh
#
# Downloads a release, checks its SHA-256, copies it to ~/.faf/cli/<version> and links
# ~/.local/bin/faf. Needs Python 3.9+ as `python3` (the hooks call it).
# FAF_VERSION=v1.0.0 installs that version. Remove with: faf uninstall
set -eu

RELEASES="${FAF_RELEASES_URL:-https://github.com/TheVanice/faf-collector/releases}"
RELEASES="${RELEASES%/}"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "On Windows, run in PowerShell: irm https://raw.githubusercontent.com/TheVanice/faf-collector/main/install.ps1 | iex" >&2
    exit 1 ;;
esac

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.9 or newer is required as python3 on PATH." >&2; exit 1; }

TAG="${FAF_VERSION:-}"
if [ -z "$TAG" ]; then
  # .../releases/latest redirects to .../releases/tag/vX.Y.Z
  TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$RELEASES/latest")
  TAG="${TAG%/}"; TAG="${TAG##*/}"
fi
case "$TAG" in v*) ;; [0-9]*) TAG="v$TAG" ;; *) echo "Could not find the latest release at $RELEASES" >&2; exit 1 ;; esac

NAME="faf-collector-$TAG.zip"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading faf $TAG"
curl -fsSL -o "$TMP/$NAME" "$RELEASES/download/$TAG/$NAME"
curl -fsSL -o "$TMP/$NAME.sha256" "$RELEASES/download/$TAG/$NAME.sha256"
EXPECTED=$(cut -d' ' -f1 < "$TMP/$NAME.sha256" | tr 'A-F' 'a-f')
ACTUAL=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$TMP/$NAME")
[ "$EXPECTED" = "$ACTUAL" ] || { echo "Checksum mismatch for $NAME: expected $EXPECTED, got $ACTUAL" >&2; exit 1; }
"$PY" -m zipfile -e "$TMP/$NAME" "$TMP/unpacked"
"$PY" "$TMP/unpacked/faf/faf_cli.py" self-install --python "$PY"
