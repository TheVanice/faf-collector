"""User-level settings: API key, endpoint and instruction-upload consent.

Stored in ~/.faf/settings.json:

    {"api_key": "faf_...", "endpoint": "https://...", "instruction_upload": {"owner/repo": true}}

FAF_API_KEY overrides api_key. Nothing else in the environment or in a repo can
change where the key goes: a repo can set environment variables for hooks, so
the key is sent only to the built-in endpoint, or to an `endpoint` stored in the
same settings file the key came from.
"""
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

COLLECTOR_VERSION = "1.1.0"
BUILTIN_ENDPOINT = "https://faf-func-34wezaqh.azurewebsites.net/api/ingest"
INVITE_URL = "https://faf-dash-34wezaqh.icypond-d9c1a18f.westeurope.azurecontainerapps.io/"
KEY_ENV = "FAF_API_KEY"
KEY_PATTERN = re.compile(r"^faf_[A-Za-z0-9_-]{43}$")
LOOPBACK_HOSTS = {"localhost"}


def settings_path():
    return Path.home() / ".faf" / "settings.json"


def load():
    """The settings file as a dict ({} when missing). Raises ValueError when unreadable."""
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save(data):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def valid_key(key):
    return isinstance(key, str) and bool(KEY_PATTERN.match(key))


def endpoint_problem(url):
    """None when url may receive the key: https, or http to a loopback host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "endpoint is not a URL"
    if parts.scheme == "https" and parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname:
        host = parts.hostname
        try:
            if ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            if host in LOOPBACK_HOSTS:
                return None
    return "endpoint must use https"


def resolve():
    """(endpoint, key, problem). problem is None when sending is possible."""
    try:
        data = load()
    except ValueError:
        return None, None, "settings file unreadable"
    env_key = os.environ.get(KEY_ENV, "").strip()
    if env_key:
        key, endpoint = env_key, BUILTIN_ENDPOINT  # a key from the environment goes to the built-in endpoint only
    else:
        key = data.get("api_key")
        endpoint = data.get("endpoint") or BUILTIN_ENDPOINT
    if not key:
        return None, None, "no API key"
    if not valid_key(key):
        return None, None, "API key malformed"
    if not isinstance(endpoint, str) or endpoint_problem(endpoint):
        return None, None, "endpoint must use https"
    return endpoint, key, None


def has_key():
    return resolve()[2] is None


def store_key(key):
    data = load()
    data["api_key"] = key
    save(data)


def upload_consent(repo):
    """True, False, or None (never asked) for this repo slug."""
    try:
        value = (load().get("instruction_upload") or {}).get(repo)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def set_upload_consent(repo, allowed):
    data = load()
    consents = data.get("instruction_upload")
    if not isinstance(consents, dict):
        consents = {}
    consents[repo] = bool(allowed)
    data["instruction_upload"] = consents
    save(data)


def upload_enabled(repo, config):
    """The user consented for this repo, and the repo's config does not disable it."""
    return upload_consent(repo) is True and (config or {}).get("upload_instruction_content") is not False


def endpoint_host():
    endpoint = resolve()[0] or BUILTIN_ENDPOINT
    return urlsplit(endpoint).netloc
