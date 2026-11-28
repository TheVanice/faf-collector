"""Tells the user when a newer collector release exists.

The SessionStart hook calls notice(): it reads a cached answer from ~/.faf/update-check.json
and asks GitHub for the latest release tag at most once a day. The request carries no key,
repo or session; only the default client identity. A failed lookup is cached too, so an
offline machine does not retry at every session start.
"""
import json
import time
from pathlib import Path
from urllib.request import urlopen

import faf_settings

RELEASES_URL = "https://github.com/TheVanice/faf-collector/releases"  # never read from the environment
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 5
CACHE_NAME = "update-check.json"


def version_key(version):
    """(1, 2, 0) for "v1.2.0" or "1.2.0"; None when it is not a dotted number."""
    try:
        return tuple(int(p) for p in str(version).lstrip("v").split("."))
    except ValueError:
        return None


def is_newer(latest, current):
    a, b = version_key(latest), version_key(current)
    return a is not None and b is not None and a > b


def _fetch_latest_tag():
    """Latest release tag without the 'v', or None. .../releases/latest redirects to .../tag/vX.Y.Z."""
    try:
        with urlopen(f"{RELEASES_URL}/latest", timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            final = response.geturl()
    except (OSError, ValueError):
        return None
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    return tag[1:] if tag.startswith("v") and version_key(tag) else None


def cache_path():
    return Path.home() / ".faf" / CACHE_NAME


def latest_known(now=None, refresh=True):
    """The latest release from the cache, looked up again when the cache is a day old (refresh=True).
    None when unknown. refresh=False only reads the cache: it never touches the network."""
    now = time.time() if now is None else now
    path = cache_path()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not refresh or now - float(cached["checked"]) < CHECK_INTERVAL_SECONDS:
            return cached.get("latest")
    except (OSError, ValueError, KeyError, TypeError):
        if not refresh:
            return None
    latest = _fetch_latest_tag()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": now, "latest": latest}), encoding="utf-8")
    except OSError:
        pass  # a read-only home only costs one lookup per session
    return latest


def notice(current=None, now=None):
    """The message for the user when a newer release is in the cache, else None. Reads the cache only.
    Never raises."""
    current = faf_settings.COLLECTOR_VERSION if current is None else current
    try:
        latest = latest_known(now, refresh=False)
    except Exception:
        return None
    if not latest or not is_newer(latest, current):
        return None
    return (f"Fleet Analytics collector {latest} is available (this project runs {current}). "
            "Run  faf upgrade  and then  faf init  in this project.")
