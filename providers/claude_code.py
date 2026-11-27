"""Claude Code adapter.

Hooks (committed in .claude/settings.json, so every clone and worktree has them):
  SessionStart  pins the instruction version Claude Code just loaded
  Stop          end of a turn: collect
  SessionEnd    end of a session: collect (synchronous, so it is not killed on exit)

The adapter reads the session transcript (JSONL) and emits one StepEvent per
turn. The last turn is always emitted again on the next call, because the
transcript may still be missing lines when the hook runs; the backend upserts
by event_id, so repeats are harmless. Every call also re-collects other
sessions of this repo whose transcript grew since they were last read, which
recovers turns from hooks that were killed or ran too early.
"""
import json
import os
import re
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from faf_event import StepEvent, git_branch, make_event_id, utcnow_iso
from .common import (
    DEFAULT_PYTHON, boundary, iso as _iso, load_state as _load_state, main_model, match, usage_by_model,
    parse_ts as _parse_ts, pin as _pin, read_lines as _read_lines, save_state as _save_state,
    retire_state, state_path, task_from_title, version_at as _version_at,
)

NAME = "claude-code"
MARKER = "faf_hook.py"
DEFAULT_HOOK_SCRIPT = ".faf/collector/faf_hook.py"
# event -> (async, timeout seconds). SessionEnd is synchronous: async hooks are
# killed when Claude Code exits, and its default budget is only 1.5 s.
EVENTS = {
    "SessionStart": (True, 30),
    "Stop": (True, 30),
    "SessionEnd": (False, 15),
}
LEGACY_EVENTS = ("Stop", "SessionEnd")
# The update notice is shown only by a synchronous hook (an async hook's systemMessage reaches
# Claude on the next turn, not the user). It reads a cache and prints in well under a second.
NOTICE_EVENT = "SessionStart"
NOTICE_TIMEOUT = 5
INTERRUPT_PREFIX = "[Request interrupted by user"
NOT_PROMPT_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>")
SYNTHETIC_MODEL = "<synthetic>"
# Failed tool results that are a permission, hook or user block, not an agent mistake.
DENIAL_PREFIXES = (
    "Permission for this action was denied", "Permission to use", "The user doesn't want to proceed",
    "The server-side auto mode classifier", "Operation cancelled by user", "PreToolUse:",
    "[Request interrupted",
)
STATE_TTL_SECONDS = 30 * 24 * 3600
MAX_SWEEP = 5
PATTERNS = [
    r"(.*/)?CLAUDE\.md",
    r"\.claude/rules/.*\.md",
    r"\.claude/skills/.*",
    r"\.claude/agents/.*\.md",
    r"\.claude/commands/.*\.md",
    r"\.claude/output-styles/.*\.md",
]
_MEMORY_FILE = re.compile(r"(.*/)?(CLAUDE|AGENTS)\.md|\.claude/rules/.*\.md")


# ---------------------------------------------------------------- instructions

def instruction_files(paths):
    selected = match(paths, PATTERNS)
    # Claude Code reads AGENTS.md only when there is no CLAUDE.md.
    if not any(p in ("CLAUDE.md", ".claude/CLAUDE.md") for p in paths):
        selected += [p for p in paths if p == "AGENTS.md"]
    return sorted(set(selected))


def follows_imports(path):
    """Memory files are the ones whose @path imports Claude Code loads."""
    return bool(_MEMORY_FILE.fullmatch(path))


# ---------------------------------------------------------------- install

def _shared_settings(root):
    return Path(root) / ".claude" / "settings.json"


def _local_settings(root):
    return Path(root) / ".claude" / "settings.local.json"


def _load_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise RuntimeError(
            f"{path} is not valid JSON. Fix or remove it, then run the installer again."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return data


def _write_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _without_faf(groups):
    return [g for g in groups if MARKER not in json.dumps(g)]


def hook_command(python, hook_script, extra=""):
    """Shell form, valid in bash, pwsh and Windows PowerShell.

    GitHub Copilot also runs the hooks in .claude/settings.json. It ignores `args`
    (so an exec form would start a bare interpreter that runs the hook JSON from
    stdin as code) and leaves $CLAUDE_PROJECT_DIR empty, so there this command
    fails to find the file without reading stdin. `; exit 0` keeps that failure
    from reading as exit code 2, which means "block" in Claude-style hooks.
    """
    args = f" {extra}" if extra else ""
    return f'{python} "$CLAUDE_PROJECT_DIR/{hook_script}" --provider {NAME}{args}; exit 0'


def _hook_group(python, hook_script, is_async, timeout, extra=""):
    hook = {"type": "command", "command": hook_command(python, hook_script, extra), "timeout": timeout}
    if is_async:
        hook["async"] = True
    return {"hooks": [hook]}


def _remove_faf(path, events):
    """Drop faf hook groups from a settings file. Returns True when the file changed."""
    if not path.exists():
        return False
    data = _load_settings(path)
    hooks = data.get("hooks", {})
    changed = False
    for event in events:
        if event in hooks:
            groups = _without_faf(hooks[event])
            changed |= len(groups) != len(hooks[event])
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
    if "hooks" in data and not hooks:
        del data["hooks"]
    if changed:
        _write_settings(path, data)
    return changed


def install(root, python, hook_script=DEFAULT_HOOK_SCRIPT):
    """hook_script: path of faf_hook.py relative to the repo root."""
    root = Path(root)
    python = python or DEFAULT_PYTHON
    path = _shared_settings(root)
    data = _load_settings(path)
    hooks = data.setdefault("hooks", {})
    for event, (is_async, timeout) in EVENTS.items():
        groups = _without_faf(hooks.get(event, []))
        groups.append(_hook_group(python, hook_script, is_async, timeout))
        if event == NOTICE_EVENT:
            groups.append(_hook_group(python, hook_script, False, NOTICE_TIMEOUT, extra="--notice"))
        hooks[event] = groups
    _write_settings(path, data)
    lines = [f"Claude Code: registered {', '.join(EVENTS)} hooks in .claude/settings.json (commit this file)"]
    if _remove_faf(_local_settings(root), LEGACY_EVENTS):
        lines.append("Claude Code: removed old per-machine hooks from .claude/settings.local.json")
    return lines


def uninstall(root):
    root = Path(root)
    removed = [p.relative_to(root).as_posix() for p, events in ((_shared_settings(root), EVENTS),
                                        (_local_settings(root), LEGACY_EVENTS))
               if _remove_faf(p, events)]
    if not removed:
        return ["Claude Code: nothing to remove"]
    return [f"Claude Code: removed faf hooks from {', '.join(removed)}"]


def skip_reason(hook_input):
    """Why this call is not one to collect, or None.

    Claude Code sends transcript_path on every hook event. Copilot runs the same
    hooks (it reads .claude/settings.json) with its own payloads; those carry
    camelCase sessionId or a Copilot events.jsonl transcript.
    """
    transcript = hook_input.get("transcript_path")
    if "sessionId" in hook_input or not transcript or Path(transcript).name == "events.jsonl":
        return "not a Claude Code hook call (no Claude Code transcript)"
    return None


# ---------------------------------------------------------------- transcript

def _blocks(msg):
    content = msg.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(obj):
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return next((str(b.get("text", "")) for b in _blocks(obj.get("message") or {})
                 if b.get("type") == "text"), "")


def _is_interrupt(obj):
    return _text(obj).startswith(INTERRUPT_PREFIX)


def _is_prompt(obj):
    """A message that starts a turn: a human prompt or an injected one (task notification).

    Tool results, interrupt markers, compact summaries, transcript-only lines and
    local command output do not start a turn.
    """
    if obj.get("type") != "user":
        return False
    if any(obj.get(flag) for flag in ("isSidechain", "isMeta", "isCompactSummary",
                                      "isVisibleInTranscriptOnly")):
        return False
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list) and any(b.get("type") == "tool_result" for b in _blocks(obj["message"])):
        return False
    if not isinstance(content, (str, list)):
        return False
    text = _text(obj)
    return not (text.startswith(INTERRUPT_PREFIX) or text.startswith(NOT_PROMPT_PREFIXES))


def _trigger(obj):
    """'human' when a person sent the prompt, 'system' when Claude Code injected it."""
    kind = (obj.get("origin") or {}).get("kind") or obj.get("turnOrigin")
    if kind:
        return "human" if kind == "human" else "system"
    return "system" if _text(obj).startswith("<task-notification>") else "human"


def _usage_record(msg, sub=False):
    usage = msg.get("usage") or {}
    return {
        "sub": sub,
        "model": msg.get("model") or "unknown",
        "in": int(usage.get("input_tokens") or 0),
        "out": int(usage.get("output_tokens") or 0),
        "cr": int(usage.get("cache_read_input_tokens") or 0),
        "cw": int(usage.get("cache_creation_input_tokens") or 0),
        # 1-hour cache writes cost 2x input instead of 1.25x.
        "cw1h": int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0),
    }


def _failed_tools(obj):
    """[(tool_use_id, 'err' | 'deny')] for the failed tool results on a user line."""
    found = []
    for block in _blocks(obj.get("message") or {}):
        if block.get("type") == "tool_result" and block.get("is_error") is True:
            content = block.get("content")
            text = content if isinstance(content, str) else " ".join(
                str(b.get("text", "")) for b in content or [] if isinstance(b, dict))
            kind = "deny" if text.lstrip().startswith(DENIAL_PREFIXES) else "err"
            found.append((block.get("tool_use_id") or obj.get("uuid"), kind))
    return found


def _build_turns(lines):
    turns = []
    cur = None
    for offset, obj in lines:
        ts = _parse_ts(obj.get("timestamp"))
        if _is_prompt(obj):
            key = obj.get("promptId") or obj.get("uuid")
            if cur is None or key != cur["key"]:
                start = ts or datetime.now(timezone.utc)
                cur = {
                    "key": key, "offset": offset, "start": start, "end": start,
                    "branch": obj.get("gitBranch"), "trigger": _trigger(obj),
                    "permission_mode": obj.get("permissionMode"),
                    "client_version": obj.get("version"), "entrypoint": obj.get("entrypoint"),
                    "efforts": Counter(), "msgs": {}, "tools": set(), "err": set(),
                    "deny": set(), "interrupted": False,
                }
                turns.append(cur)
        if cur is None:
            continue
        kind = obj.get("type")
        msg = obj.get("message") or {}
        # Only the agent's own lines move the end; queue, attachment, api-request
        # and meta lines are written later and would stretch the turn.
        if ts and ts > cur["end"] and (kind == "assistant" or (
                kind == "user" and not obj.get("isMeta") and not obj.get("isVisibleInTranscriptOnly"))):
            cur["end"] = ts
        if kind == "assistant":
            if obj.get("effort"):
                cur["efforts"][obj["effort"]] += 1
            mid = msg.get("id")
            if mid and msg.get("model") != SYNTHETIC_MODEL:
                cur["msgs"].setdefault(mid, _usage_record(msg))
            for block in _blocks(msg):
                if block.get("type") == "tool_use" and block.get("id"):
                    cur["tools"].add(block["id"])
        elif kind == "user":
            if _is_interrupt(obj):
                cur["interrupted"] = True
            for key, failure in _failed_tools(obj):
                cur[failure].add(key)
    return turns


def _subagent_items(transcript, session_id):
    """(timestamp, kind, key, payload) for every subagent line of this session."""
    folder = transcript.parent / session_id / "subagents"
    items = []
    if not folder.is_dir():
        return items
    seen_msgs, seen_tools, seen_fails = set(), set(), set()
    for path in sorted(folder.glob("agent-*.jsonl")):
        for _, obj in _read_lines(path, 0):
            ts = _parse_ts(obj.get("timestamp"))
            if ts is None:
                continue
            msg = obj.get("message") or {}
            if obj.get("type") == "assistant":
                mid = msg.get("id")
                if mid and msg.get("model") != SYNTHETIC_MODEL and mid not in seen_msgs:
                    seen_msgs.add(mid)
                    items.append((ts, "msg", mid, _usage_record(msg, sub=True)))
                for block in _blocks(msg):
                    if block.get("type") == "tool_use" and block.get("id") and block["id"] not in seen_tools:
                        seen_tools.add(block["id"])
                        items.append((ts, "tool", block["id"], None))
            elif obj.get("type") == "user":
                for key, failure in _failed_tools(obj):
                    if key not in seen_fails:
                        seen_fails.add(key)
                        items.append((ts, failure, key, None))
    return items


# ---------------------------------------------------------------- state

def _state_path(ctx, session_id):
    return state_path(ctx, NAME, session_id)


def _is_transcript(path, session_id):
    # Transcripts live at <config dir>/projects/<project>/<session_id>.jsonl.
    return (path.name == f"{session_id}.jsonl" and path.parent.parent.name == "projects"
            and path.is_file())


# ---------------------------------------------------------------- collect

def _session_titles(lines):
    """(latest /rename title, latest auto title) in these lines, as task names or None."""
    names = {"custom-title": None, "ai-title": None}
    for _, obj in lines:
        kind = obj.get("type")
        if kind in names:
            title = obj.get("customTitle") or obj.get("aiTitle") or obj.get("title")
            names[kind] = task_from_title(title) or names[kind]
    return names["custom-title"], names["ai-title"]


def _boundary(turn, session_id, ctx, fallback_branch, task=None):
    return boundary(turn["branch"] or fallback_branch, session_id, ctx, task=task)


def _to_event(turn, session_id, ctx, fallback_branch, task=None):
    msgs = list(turn["msgs"].values())
    model = main_model(msgs)
    effort = turn["efforts"].most_common(1)[0][0] if turn["efforts"] else None
    event = StepEvent(
        event_id=make_event_id(NAME, session_id, turn["key"]),
        provider=NAME,
        repo=ctx.repo,
        boundary_ref=_boundary(turn, session_id, ctx, fallback_branch, task),
        session_id=session_id,
        ts_start=_iso(turn["start"]),
        ts_end=_iso(turn["end"]),
        instruction_version=turn["version"],
        trigger=turn["trigger"],
        model=model,
        effort=effort,
        permission_mode=turn["permission_mode"],
        client_version=turn["client_version"],
        entrypoint=turn["entrypoint"],
        tokens_in=sum(m["in"] for m in msgs),
        tokens_out=sum(m["out"] for m in msgs),
        tokens_cache_read=sum(m["cr"] for m in msgs),
        tokens_cache_write=sum(m["cw"] for m in msgs),
        tokens_cache_write_1h=sum(m.get("cw1h", 0) for m in msgs),
        llm_calls=len(msgs),
        usage=usage_by_model(msgs),
        tool_calls=len(turn["tools"]),
        tool_errors=len(turn["err"]),
        tool_denials=len(turn["deny"]),
        status="interrupted" if turn["interrupted"] else "ok",
    )
    return event.to_json()


def _collect_session(session_id, transcript, state, ctx):
    if not state["pins"]:
        _pin(state, "", ctx.instruction_version)  # session began before the collector ran
    state["transcript"] = str(transcript)
    state["size"] = transcript.stat().st_size  # taken before reading: growth during the read is swept later
    lines = list(_read_lines(transcript, state["offset"]))
    # A /rename wins over auto titles, which Claude Code keeps writing after it.
    renamed, auto = _session_titles(lines)
    renamed = renamed or (state.get("meta") or {}).get("renamed")
    task = renamed or auto or state.get("task")
    if task != state.get("task") and state["offset"]:
        # The name came late or changed: send the earlier turns again, now under it.
        lines = list(_read_lines(transcript, 0))
        all_renamed, all_auto = _session_titles(lines)
        renamed = all_renamed or renamed
        task = renamed or all_auto or task
    state["task"], state["meta"] = task, {"renamed": renamed}
    turns = _build_turns(lines)
    if not turns:
        return []

    starts = [t["start"] for t in turns]
    for ts, kind, key, payload in _subagent_items(transcript, session_id):
        i = bisect_right(starts, ts) - 1  # window: [start_i, start_i+1)
        if i < 0:
            continue
        turn = turns[i]
        if kind == "msg":
            turn["msgs"].setdefault(key, payload)
        elif kind == "tool":
            turn["tools"].add(key)
        else:
            turn[kind].add(key)

    fallback_branch = git_branch(ctx.root)
    events = []
    for turn in turns:
        turn["version"] = _version_at(state["pins"], turn["start"])
        if turn["msgs"]:  # a turn without a model call is not an agent step
            events.append(_to_event(turn, session_id, ctx, fallback_branch, task))
    state["offset"] = turns[-1]["offset"]
    return events


def _sweep(ctx, exclude):
    """Collect other sessions whose transcript grew since they were last read."""
    events, swept, now = [], 0, time.time()
    for path in sorted(ctx.state_dir.glob(f"{NAME}-*.json")):
        state = _load_state(path)
        session_id = state.get("session_id")
        if not session_id or session_id == exclude:
            continue
        transcript = Path(state["transcript"]) if state.get("transcript") else None
        try:
            stale = now - path.stat().st_mtime > STATE_TTL_SECONDS
            if transcript is None or not transcript.is_file() or stale:
                retire_state(path, NAME, session_id)
                continue
            grew = transcript.stat().st_size != state.get("size")
        except OSError:
            continue
        if grew and swept < MAX_SWEEP:
            swept += 1
            events += _collect_session(session_id, transcript, state, ctx)
            _save_state(path, state)
    return events


def collect(hook_input, ctx):
    session_id = hook_input.get("session_id")
    transcript_arg = hook_input.get("transcript_path")
    events = []
    if session_id and transcript_arg and _is_transcript(Path(transcript_arg), session_id):
        state_path = _state_path(ctx, session_id)
        state = _load_state(state_path, session_id)
        if hook_input.get("hook_event_name") == "SessionStart":
            _pin(state, getattr(ctx, "started_at", None) or utcnow_iso(), ctx.instruction_version)
        events += _collect_session(session_id, Path(transcript_arg), state, ctx)
        _save_state(state_path, state)
    return events + _sweep(ctx, exclude=session_id)


# ---------------------------------------------------------------- migration

# Hooks of older collectors that an install replaces (path fragments of their commands).
LEGACY_HOOK_MARKERS = ("collector/hooks/claude_hook.py",)  # faf.v0, kept inside the repo


def _without_legacy(groups):
    return [g for g in groups if not any(m in json.dumps(g).replace("\\\\", "/") for m in LEGACY_HOOK_MARKERS)]


def remove_legacy_hooks(root):
    """Drop hooks of older collectors from both settings files. Returns the files changed."""
    root = Path(root)
    changed = []
    for path in (_shared_settings(root), _local_settings(root)):
        if not path.exists():
            continue
        data = _load_settings(path)
        hooks = data.get("hooks", {})
        before = json.dumps(hooks)
        for event in list(hooks):
            groups = _without_legacy(hooks[event])
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
        if json.dumps(hooks) != before:
            if not hooks:
                data.pop("hooks", None)
            _write_settings(path, data)
            changed.append(path.relative_to(root).as_posix())
    return changed


def sessions(root):
    """(session_id, transcript) of every Claude Code session started in this repo's folder."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    want = re.sub(r"[^A-Za-z0-9]", "-", str(Path(root))).lower()
    found = []
    if base.is_dir():
        for folder in base.iterdir():
            if folder.is_dir() and folder.name.lower() == want:
                found += [(p.stem, p) for p in sorted(folder.glob("*.jsonl"))]
    return found


def backfill(ctx, version, sessions_):
    """Turns of sessions this machine never read, pinned to `version` (theirs is not known)."""
    events = []
    for session_id, transcript in sessions_:
        path = _state_path(ctx, session_id)
        if path.exists() or not _is_transcript(transcript, session_id):
            continue
        state = _load_state(path, session_id)
        _pin(state, "", version)
        events += _collect_session(session_id, transcript, state, ctx)
        _save_state(path, state)
    return events
