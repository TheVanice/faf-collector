"""Migration that every install runs, so an upgrade never loses data or leaves an old hook.

1. Hooks of older collectors are removed (and a faf.v0 collector folder inside the repo).
2. The obsolete .faf/local/secrets.json is deleted (the key lives in ~/.faf/settings.json).
3. Rejected events are queued again; the backend may accept them now.
4. Queued instruction uploads follow the current consent (content dropped when it is off).
5. Sessions of the repo that this machine never read are collected from their transcripts.
   Their instruction version is not known any more, so their turns carry "unknown".
6. The user-level faf.v0 spool in ~/.faf (never sendable: v0 is retired) is archived to
   ~/.faf/legacy-v0/; step 5 has recorded the same turns from the transcripts.
7. Everything queued is sent.

plan() describes what migrate() would do, for the installer's confirmation and --dry-run.
"""
import json
import os
import shutil
import time
from pathlib import Path

import faf_log
import faf_settings
from faf_event import Spool, _read_jsonl, git_default_branch, git_repo_slug, local_dir, utcnow_iso
from faf_instructions import instruction_snapshot, read_config
from faf_post import flush, requeue_rejected
from providers.common import retired_sessions

UNKNOWN_VERSION = "unknown"
V0_FILES = ("spool.jsonl", "spool.jsonl.sending", "status.json", "state")
log = faf_log.LOGGER


def _legacy_collector_dir(root):
    """<root>/collector when it is a faf.v0 collector (not this collector's own source)."""
    folder = Path(root) / "collector"
    if (folder / "faf_collect.py").is_file() and (folder / "hooks" / "claude_hook.py").is_file() \
            and not (folder / "faf_hook.py").is_file():
        return folder
    return None


def _user_v0_files():
    home = faf_settings.settings_path().parent
    spool = home / "spool.jsonl"
    found = [home / n for n in V0_FILES if (home / n).exists()]
    if not found:
        return []
    for path in (spool, home / "spool.jsonl.sending"):
        if path.is_file():
            first = next((line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                          if line.strip()), "")
            if first and '"faf.v0"' not in first:
                return []  # not a v0 spool: leave it alone
    return found


def _unread_sessions(root, provider):
    finder = getattr(provider, "sessions", None)
    if finder is None:
        return []
    state = local_dir(root, create=False) / "state"
    retired = retired_sessions(state, provider.NAME)
    return [(sid, path) for sid, path in finder(root)
            if sid not in retired and not (state / f"{provider.NAME}-{sid}.json").exists()]


def plan(root, providers):
    root = Path(root)
    lines = []
    if _legacy_collector_dir(root):
        lines.append("remove the old faf.v0 collector: collector/ and its hooks")
    if (local_dir(root, create=False) / "secrets.json").exists():
        lines.append("delete .faf/local/secrets.json (old ingest key, no longer used)")
    if (local_dir(root, create=False) / "rejected.jsonl").exists():
        lines.append("queue rejected events again")
    for provider in providers:
        unread = _unread_sessions(root, provider)
        if unread:
            lines.append(f"{provider.NAME}: record {len(unread)} sessions never read on this machine "
                         f"(instruction version '{UNKNOWN_VERSION}')")
    if _user_v0_files():
        lines.append(f"archive the faf.v0 spool to {faf_settings.settings_path().parent / 'legacy-v0'}")
    return lines


def _apply_consent(root, upload):
    """Drop file paths and text from queued instruction uploads when the upload is off."""
    if upload:
        return 0
    stripped = 0
    for path in sorted(local_dir(root).glob("spool*.jsonl")):
        events = _read_jsonl(path) or []
        changed = False
        for ev in events:
            if isinstance(ev, dict) and ev.get("kind") == "instruction_version" and ev.get("files"):
                ev["files"] = []
                changed, stripped = True, stripped + 1
        if changed:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("".join(json.dumps(ev) + "\n" for ev in events), encoding="utf-8")
            os.replace(tmp, path)
    return stripped


def _archive_v0():
    files = _user_v0_files()
    if not files:
        return None
    target = faf_settings.settings_path().parent / "legacy-v0" / time.strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.move(str(path), str(target / path.name))
    return target


def migrate(root, providers, context_factory):
    """Run every step. context_factory(root, provider, version) builds the hook Context."""
    root = Path(root)
    lines = []

    legacy = _legacy_collector_dir(root)
    for provider in providers:
        remove = getattr(provider, "remove_legacy_hooks", None)
        for path in (remove(root) if remove else []):
            lines.append(f"removed old collector hooks from {path}")
    if legacy:
        shutil.rmtree(legacy)
        lines.append("removed the old faf.v0 collector folder collector/ (commit the deletion)")

    secrets = local_dir(root, create=False) / "secrets.json"
    if secrets.exists():
        secrets.unlink()
        lines.append("deleted .faf/local/secrets.json")

    if (local_dir(root, create=False) / "rejected.jsonl").exists():
        lines.append(f"queued {requeue_rejected(root)} rejected events again")

    from faf_hook import LockBusy, _lock  # the hook's lock: no hook run interleaves with this
    try:
        with _lock(local_dir(root) / "hook.lock"):
            lines += _migrate_data(root, providers, context_factory)
    except LockBusy:
        lines.append("a hook run is active: data not migrated now; run the installer again")
    return lines


def _migrate_data(root, providers, context_factory):
    lines = []
    config = read_config(root)
    repo = git_repo_slug(root)
    stripped = _apply_consent(root, faf_settings.upload_enabled(repo, config))
    if stripped:
        lines.append(f"removed instruction text from {stripped} queued uploads (upload is off)")

    spool = Spool(local_dir(root) / "spool.jsonl")
    (local_dir(root) / "state").mkdir(exist_ok=True)
    for provider in providers:
        unread = _unread_sessions(root, provider)
        if not unread or not hasattr(provider, "backfill"):
            continue
        version, _ = instruction_snapshot(root, provider, config=config)
        ctx = context_factory(root, provider, version)
        events = provider.backfill(ctx, UNKNOWN_VERSION, unread)
        for event in events:
            spool.append(event)
        lines.append(f"{provider.NAME}: recorded {len(events)} turns of {len(unread)} sessions never read")

    archived = _archive_v0()
    if archived:
        lines.append(f"archived the faf.v0 spool to {archived}")

    if faf_settings.has_key():
        result = flush(root)
        if result.error:
            lines.append(f"send failed ({result.error}), {result.queued} events stay queued")
        else:
            lines.append(f"sent {result.sent} events" + (f", {result.rejected} rejected" if result.rejected else ""))
    else:
        lines.append("events stay queued until an API key is set")
    return lines


def context(root, provider, version):
    """The hook's Context for a migration run (same fields the hook builds)."""
    from faf_hook import Context
    return Context(root=Path(root), repo=git_repo_slug(root), state_dir=local_dir(root) / "state",
                   instruction_version=version, default_branch=git_default_branch(root),
                   started_at=utcnow_iso())
