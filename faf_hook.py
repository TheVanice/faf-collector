"""Entry point that the agent's hooks call.

    python faf_hook.py --provider claude-code                  (hook JSON on stdin)
    python faf_hook.py --provider copilot --event agentStop    (Copilot omits the event name)

    python faf_hook.py --provider claude-code --notice    (synchronous SessionStart entry: prints the
                                                            update notice (systemMessage) and the
                                                            upload reminder (additionalContext), if any)

Contract: writes to stdout only for --notice, and then only the hook output JSON; always exits 0.
Every run writes a few lines
to .faf/local/collector.log (counts, HTTP status, duration; never data); watch it
with `Get-Content .faf\\local\\collector.log -Wait` or `tail -F`. Nothing is
recorded until an API key is configured; the transcript keeps the history,
so the first configured run sends every turn from the start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import faf_log
import faf_settings
import faf_update
from faf_event import (
    InstructionVersionEvent,
    Spool,
    git_default_branch,
    git_head,
    git_repo_slug,
    local_dir,
    repo_root,
    utcnow_iso,
)
from faf_instructions import (
    change_shape,
    git_dirty,
    instruction_snapshot,
    local_snapshot,
    read_config,
    upload_files,
)
from faf_post import flush, report
from providers import PROVIDERS

LOCK_WAIT_SECONDS = 8
LOCK_STALE_SECONDS = 60  # longer than any hook may run, so an older lock was left by a killed run
FLUSH_TIMEOUT_SECONDS = 4
LOG_MAX_BYTES = faf_log.LOG_MAX_BYTES
log = faf_log.LOGGER


class LockBusy(Exception):
    """Another hook run of this repo holds the lock."""


@dataclass
class Context:
    root: Path
    repo: str
    state_dir: Path
    instruction_version: str
    default_branch: str | None = None
    started_at: str | None = None


def project_root(hook_input, provider=None):
    # CLAUDE_PROJECT_DIR stays the session's project when Claude cds elsewhere; cwd does not.
    # Other agents do not set it, and a value inherited from a Claude terminal must not win there.
    env = getattr(provider, "PROJECT_DIR_ENV", "CLAUDE_PROJECT_DIR") if provider else "CLAUDE_PROJECT_DIR"
    return repo_root((os.environ.get(env) if env else None) or hook_input.get("cwd") or os.getcwd())


@contextmanager
def _lock(path):
    """Serialise hook runs of one repo, e.g. Stop and SessionEnd firing together."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockBusy() from None
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _load_json(path, default):
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else default
        except ValueError:
            pass
    return default


def _register_version(ctx, provider, spool, files, upload):
    """Spool the instruction version once per machine, and again with content when the
    user opts in later. Returns the number of changed files of a new version, else None.

    Marking it registered at spool time is safe: the spool survives failed sends, and
    events the backend rejects are kept in rejected.jsonl.
    """
    path = ctx.state_dir / f"versions-{provider.NAME}.json"
    snap_path = ctx.state_dir / f"instructions-{provider.NAME}.json"
    state = _load_json(path, {})
    snapshot = _load_json(snap_path, {})
    registered = state.get("registered") or state.get("sent") or []
    uploaded = state.get("uploaded") or []
    version, current = ctx.instruction_version, state.get("current")
    new_changes = None

    def event(previous, changes):
        return InstructionVersionEvent(
            provider=provider.NAME, repo=ctx.repo, version=version, previous_version=previous,
            ts=utcnow_iso(), head_sha=git_head(ctx.root),
            dirty=git_dirty(ctx.root, [f["path"] for f in files]),
            files=upload_files(files) if upload else [], changes=changes,
        )

    if version not in registered:
        if current is None:
            changes = change_shape(None, files)  # first version on this machine: every file is new
        elif snapshot.get("version") == current:
            changes = change_shape(snapshot.get("files"), files)
        else:
            changes = None  # the previous version's files were not kept (older collector)
        spool.append(event(current, changes))
        registered.append(version)
        if upload:
            uploaded.append(version)
        state["previous"], state["changes"] = current, changes
        new_changes = len(changes or [])
    elif upload and version not in uploaded:
        # Opted in after this version was registered: send it again with its content.
        same = current == version
        spool.append(event(state.get("previous") if same else None, state.get("changes") if same else None))
        uploaded.append(version)
    elif current != version:
        state["previous"], state["changes"] = None, None
    if snapshot.get("version") != version:
        snap_path.write_text(json.dumps(local_snapshot(version, files)), encoding="utf-8")
    state.update(current=version, registered=registered, uploaded=uploaded)
    path.write_text(json.dumps(state), encoding="utf-8")
    return new_changes


def run(provider_name, hook_input):
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise ValueError("unknown provider")
    skip = getattr(provider, "skip_reason", None)
    reason = skip(hook_input) if skip else None
    if reason:
        log.info(f"ignored: {reason}")
        return
    root = project_root(hook_input, provider)
    if not (root / ".faf").is_dir():
        return  # the collector is not installed in this repo
    if hook_input.get("hook_event_name") == "SessionStart":
        faf_update.latest_known()  # looks up the newest release at most once a day; --notice shows it
    endpoint, _, problem = faf_settings.resolve()
    if problem:
        log.info(f"not recording: {problem}")
        return
    local = local_dir(root)
    state_dir = local / "state"
    state_dir.mkdir(exist_ok=True)

    with _lock(local / "hook.lock"):
        config = read_config(root)
        version, files = instruction_snapshot(root, provider, config=config)
        repo = git_repo_slug(root)
        upload = faf_settings.upload_enabled(repo, config)
        if hook_input.get("hook_event_name") == "SessionStart":
            log.info(f"v{faf_settings.COLLECTOR_VERSION} active, instruction upload {'on' if upload else 'off'}")
            if not upload:
                log.info(faf_settings.upload_reminder(repo, config))
        ctx = Context(root=root, repo=repo, state_dir=state_dir, instruction_version=version,
                      default_branch=git_default_branch(root), started_at=utcnow_iso())
        spool = Spool(local / "spool.jsonl")
        changed = _register_version(ctx, provider, spool, files, upload)
        if changed is not None:
            log.info(f"new instruction version queued ({changed} changed files)")
        steps = provider.collect(hook_input, ctx)
        for step in steps:
            spool.append(step)
        if steps:
            log.info(f"recorded {len(steps)} turns")
        result = flush(root, retries=0, timeout=FLUSH_TIMEOUT_SECONDS)
    report(result, endpoint)


def notice(provider_name, hook_input):
    """The hook's JSON output, or ''. The update notice goes to systemMessage (shown to the user by
    the CLI); the upload reminder goes to additionalContext, so Claude passes it on in its first reply
    (the VS Code extension does not show a SessionStart systemMessage).

    Reads the cache only, so the synchronous SessionStart entry that calls it stays fast.
    Copilot runs the same command, so calls that are not Claude Code's are left silent.
    """
    provider = PROVIDERS.get(provider_name)
    skip = getattr(provider, "skip_reason", None)
    if skip and skip(hook_input):
        return ""
    output = {}
    update = faf_update.notice()
    if update:
        output["systemMessage"] = update
    reminder = _upload_reminder(project_root(hook_input, provider))
    if reminder:
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": f"Fleet Analytics collector: {reminder} "
                                 "Tell the user this once, in one short line at the start of your first reply.",
        }
    return json.dumps(output) + "\n" if output else ""


def _upload_reminder(root):
    """The upload reminder for a repo that records but does not upload instruction text, else None."""
    try:
        if not (root / ".faf").is_dir() or faf_settings.resolve()[2]:
            return None  # not installed, or nothing is recorded without a key
        return faf_settings.upload_reminder(git_repo_slug(root), read_config(root))
    except Exception:
        return None  # the notice must never break the session start


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(prog="faf_hook", add_help=False)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--event", default=None)  # for agents whose payload has no event name
    parser.add_argument("--notice", action="store_true")  # print the update notice instead of recording
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    hook_input, provider = {}, PROVIDERS.get(args.provider)
    try:
        # Hook JSON is UTF-8; sys.stdin would decode it with the locale (cp1252 on Windows).
        raw = stdin.read() if stdin is not None else sys.stdin.buffer.read().decode("utf-8", "replace")
        if not raw.strip():
            return 0  # every agent sends JSON: another interpreter of the hook command already read it
        hook_input = json.loads(raw)
        if not isinstance(hook_input, dict):
            hook_input = {}
        normalize = getattr(provider, "normalize", None)
        if normalize:
            hook_input = normalize(hook_input, args.event)
        elif args.event:
            hook_input.setdefault("hook_event_name", args.event)
        if args.notice:
            sys.stdout.write(notice(args.provider, hook_input))
            return 0
        faf_log.setup(project_root(hook_input, provider), event=hook_input.get("hook_event_name") or "manual")
        run(args.provider, hook_input)
    except LockBusy:
        log.info("skipped: another hook run is active")
    except Exception as exc:
        try:
            if not log.handlers:
                faf_log.setup(project_root(hook_input, provider), event=hook_input.get("hook_event_name"))
            log.info(faf_log.crash_line(exc))
        except Exception:  # noqa: S110 - logging the crash failed too; a hook must never break the agent
            pass
    finally:
        faf_log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
