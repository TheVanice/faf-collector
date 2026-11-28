"""GitHub Copilot adapter (Copilot CLI, VS Code agent sessions, Copilot app).

Hooks (committed in .github/hooks/faf.json, so every clone and worktree has them):
  sessionStart  pins the instruction version Copilot just loaded
  agentStop     end of a turn: collect
  sessionEnd    end of a session: collect

Copilot sends camelCase payloads without the event name, so the hook command
passes it with --event. The adapter reads the session log
<COPILOT_HOME or ~/.copilot>/session-state/<sessionId>/events.jsonl and emits one
StepEvent per turn. A turn starts with a top-level user.message (steering
messages sent while the agent works join the running turn). Token usage comes
from session.usage_record, which Copilot writes once per model call, plus
session.compaction_complete for context compactions. Checked against 21 real
sessions: the sums equal Copilot's own session.shutdown totals.

As with Claude Code, the last turn is emitted again on the next call and other
sessions of this repo whose log grew are re-collected.
"""
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import faf_log
from faf_event import StepEvent, git_branch, git_worktrees, make_event_id, utcnow_iso

from .claude_code import _has_faf_hooks, _load_settings, _without_faf, _write_settings
from .common import (
    agent_type_name,
    boundary,
    count_item,
    iso,
    load_state,
    main_model,
    parse_ts,
    pin,
    read_lines,
    retire_state,
    save_state,
    state_path,
    subagent_entries,
    subagent_record,
    task_from_title,
    usage_by_model,
    version_at,
)

NAME = "copilot"
LABEL = "Copilot"
PROJECT_DIR_ENV = None  # the repo comes from the payload's cwd
HOOKS_FILE = Path(".github") / "hooks" / "faf.json"
DEFAULT_HOOK_SCRIPT = ".faf/collector/faf_hook.py"
# Copilot event -> (event name used inside the collector, timeout seconds).
EVENTS = {
    "sessionStart": ("SessionStart", 30),
    "agentStop": ("Stop", 30),
    "sessionEnd": ("SessionEnd", 15),
}
STATE_TTL_SECONDS = 30 * 24 * 3600
MAX_SWEEP = 5
# Lines written by the agent while it works; they move the end of a turn.
END_TYPES = ("assistant.message", "assistant.turn_end", "tool.execution_complete",
             "session.usage_record", "abort")
# Lines with an agentId that show a subagent working; they make up its time span in a turn.
AGENT_TYPES = (*END_TYPES, "subagent.started", "subagent.completed", "user.message", "assistant.turn_start",
               "tool.execution_start", "session.compaction_complete")
# ---------------------------------------------------------------- install

def hook_entry(python, hook_script, event, timeout):
    """bash runs on Linux/macOS (python3), powershell on Windows (python)."""
    args = f"{hook_script} --provider {NAME} --event {event}"
    return {
        "type": "command",
        "bash": f"{python or 'python3'} {args}",
        "powershell": f"{python or 'python'} {args}",
        "cwd": ".",
        "timeoutSec": timeout,
    }


def install(root, python=None, hook_script=DEFAULT_HOOK_SCRIPT):
    """hook_script: path of faf_hook.py relative to the repo root.

    python: interpreter for both platforms; None means python3 on Linux/macOS
    and python on Windows.
    """
    path = Path(root) / HOOKS_FILE
    data = _load_settings(path)  # keep hooks the repo already has in this file
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    for event, (_, timeout) in EVENTS.items():
        hooks[event] = _without_faf(hooks.get(event, [])) + [hook_entry(python, hook_script, event, timeout)]
    _write_settings(path, data)
    return [f"Copilot: registered {', '.join(EVENTS)} hooks in {HOOKS_FILE.as_posix()} (commit this file)"]


def installed(root):
    """Whether this repo has faf hooks for Copilot."""
    return _has_faf_hooks(Path(root) / HOOKS_FILE)


def uninstall(root):
    """Removes only faf's entries; the file goes only when nothing else is left in it."""
    path = Path(root) / HOOKS_FILE
    data = _load_settings(path)
    hooks = data.get("hooks", {})
    changed = False
    for event in EVENTS:
        if event in hooks:
            kept = _without_faf(hooks[event])
            changed |= len(kept) != len(hooks[event])
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not changed:
        return ["Copilot: nothing to remove"]
    if hooks or set(data) - {"version", "hooks"}:
        _write_settings(path, data)
        return [f"Copilot: removed faf hooks from {HOOKS_FILE.as_posix()}"]
    path.unlink()
    try:
        path.parent.rmdir()  # only when empty
    except OSError:
        pass
    return [f"Copilot: removed {HOOKS_FILE.as_posix()}"]


# ---------------------------------------------------------------- hook input

def normalize(hook_input, event=None):
    """Copilot's camelCase payload -> the snake_case names the collector uses."""
    out = dict(hook_input)
    out["session_id"] = hook_input.get("sessionId") or hook_input.get("session_id")
    transcript = hook_input.get("transcriptPath") or hook_input.get("transcript_path")
    if transcript:
        out["transcript_path"] = transcript
    name = event or hook_input.get("hook_event_name")
    out["hook_event_name"] = EVENTS[name][0] if name in EVENTS else name
    out["agent_id"] = hook_input.get("agentId") or hook_input.get("agent_id")
    return out


def skip_reason(hook_input):
    if hook_input.get("agent_id"):
        return "subagent stop; its usage is collected with the main turn"
    if os.environ.get("COPILOT_AGENT_PROMPT"):
        return "Copilot cloud agent session (no local transcript to keep)"
    return None


# ---------------------------------------------------------------- session log

def _session_root(hook_input):
    transcript = hook_input.get("transcript_path")
    if transcript and Path(transcript).name == "events.jsonl":
        return Path(transcript).parent.parent
    home = os.environ.get("COPILOT_HOME")
    return (Path(home) if home else Path.home() / ".copilot") / "session-state"


def _transcript(hook_input, session_id):
    path = _session_root(hook_input) / session_id / "events.jsonl"
    given = hook_input.get("transcript_path")
    if given and Path(given).name == "events.jsonl" and Path(given).parent.name == session_id:
        path = Path(given)
    return path


def _workspace(transcript):
    """Top-level `key: value` lines of workspace.yaml next to events.jsonl."""
    try:
        text = (transcript.parent / "workspace.yaml").read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(?m)^(\w+):[ \t]*(.*)$", text)}


def _scalar(value):
    """A workspace.yaml value without its quotes; double-quoted ones carry JSON-style escapes."""
    value = (value or "").strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value.strip("\"'")


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_record(obj):
    data = obj.get("data") or {}
    compaction = obj.get("type") == "session.compaction_complete"
    u = (data.get("compactionTokensUsed") if compaction else data.get("usage")) or {}
    total_in, cr, cw = _int(u.get("inputTokens")), _int(u.get("cacheReadTokens")), _int(u.get("cacheWriteTokens"))
    return {
        # Subagent and compaction calls count for tokens, not for the turn's model.
        "sub": compaction or bool(obj.get("agentId")) or u.get("initiator") == "sub-agent",
        "model": u.get("model") or "unknown",
        # inputTokens includes cache reads and writes; outputTokens includes reasoning.
        "in": max(total_in - cr - cw, 0),
        "out": _int(u.get("outputTokens")),
        "cr": cr,
        "cw": cw,
        "effort": u.get("reasoningEffort"),
    }


def _update_meta(meta, obj):
    kind, data = obj.get("type"), obj.get("data") or {}
    if kind in ("session.start", "session.resume"):
        context = data.get("context") or {}
        if context.get("branch"):
            meta["branch"] = context["branch"]
        if data.get("copilotVersion"):
            meta["client_version"] = data["copilotVersion"]
        if data.get("reasoningEffort"):
            meta["effort"] = data["reasoningEffort"]
    elif kind == "session.model_change" and data.get("reasoningEffort"):
        meta["effort"] = data["reasoningEffort"]
    elif kind == "subagent.started" and obj.get("agentId"):
        # Only the type name: the line also holds the task's description, which is never sent.
        name = agent_type_name(data.get("agentName"))
        if name:
            meta.setdefault("agents", {})[_agent_id(obj["agentId"])] = name


def _agent_id(value):
    """An opaque id for a subagent: Copilot's own agentId is never sent as it is."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:32]


def _count(turn, agent_id, ts, kind, key, payload=None):
    """Count in the turn and, when a subagent made the call, also in that agent's record."""
    count_item(turn, kind, key, payload)
    if agent_id and ts:
        count_item(subagent_record(turn, agent_id, ts), kind, key, payload)


def _starts_turn(obj):
    if obj.get("type") != "user.message" or obj.get("agentId"):
        return False
    return (obj.get("data") or {}).get("delivery") != "steering"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _worktree_in(arguments, worktrees):
    """Branch of the worktree a tool call's paths or command point into, else None."""
    for text in _strings(arguments):
        text = text.replace("\\", "/").lower()
        for path, name in worktrees.items():
            if re.search(re.escape(path) + r"(?![\w.-])", text):
                return name
    return None


def _build_turns(lines, meta, worktrees=None):
    """worktrees: {path: branch} of the repo's linked worktrees. Once a tool call works inside
    one, that worktree is the turn's worktree for the rest of the session."""
    worktrees = worktrees or {}
    turns, cur = [], None
    for offset, obj in lines:
        kind, data = obj.get("type"), obj.get("data") or {}
        ts = parse_ts(obj.get("timestamp"))
        _update_meta(meta, obj)
        if _starts_turn(obj):
            start = ts or datetime.now(timezone.utc)
            cur = {
                "key": obj.get("id") or data.get("messageId"), "offset": offset,
                "start": start, "end": start,
                "branch": meta.get("branch"), "worktree": meta.get("worktree"),
                "trigger": "system" if data.get("source") else "human",
                "permission_mode": None, "client_version": meta.get("client_version"),
                "default_effort": meta.get("effort"), "efforts": Counter(),
                "msgs": {}, "tools": set(), "err": set(), "deny": set(), "interrupted": False,
                "subagents": {},
            }
            turns.append(cur)
            continue
        if cur is None:
            continue
        if ts and ts > cur["end"] and kind in END_TYPES:
            cur["end"] = ts
        agent = _agent_id(obj["agentId"]) if obj.get("agentId") else None
        if agent and ts and kind in AGENT_TYPES:
            subagent_record(cur, agent, ts)
        if kind == "session.usage_record" or (
                kind == "session.compaction_complete" and isinstance(data.get("compactionTokensUsed"), dict)):
            record = _usage_record(obj)
            if record["effort"] and not record["sub"]:
                cur["efforts"][record["effort"]] += 1
            _count(cur, agent, ts, "msg", obj.get("id") or str(offset), record)
        elif kind == "assistant.turn_start":
            cur["permission_mode"] = cur["permission_mode"] or data.get("agentMode")
        elif kind == "tool.execution_start" and data.get("toolCallId"):
            _count(cur, agent, ts, "tool", data["toolCallId"])
            worktree = _worktree_in(data.get("arguments"), worktrees)
            if worktree:
                meta["worktree"] = cur["worktree"] = worktree
        elif kind == "tool.execution_complete" and data.get("success") is False:
            code = (data.get("error") or {}).get("code")
            _count(cur, agent, ts, "deny" if code == "denied" else "err", data.get("toolCallId") or obj.get("id"))
        elif kind == "abort":
            cur["interrupted"] = True
    return turns


# ---------------------------------------------------------------- collect

def _to_event(turn, session_id, ctx, fallback_branch, entrypoint, agent_types, task=None):
    msgs = list(turn["msgs"].values())
    effort = turn["efforts"].most_common(1)[0][0] if turn["efforts"] else turn["default_effort"]
    event = StepEvent(
        event_id=make_event_id(NAME, session_id, turn["key"]),
        provider=NAME,
        repo=ctx.repo,
        boundary_ref=boundary(turn["branch"] or fallback_branch, session_id, ctx,
                              worktree=turn["worktree"], task=task),
        session_id=session_id,
        ts_start=iso(turn["start"]),
        ts_end=iso(turn["end"]),
        instruction_version=turn["version"],
        trigger=turn["trigger"],
        model=main_model(msgs),
        effort=effort,
        permission_mode=turn["permission_mode"],
        client_version=turn["client_version"],
        entrypoint=entrypoint,
        tokens_in=sum(m["in"] for m in msgs),
        tokens_out=sum(m["out"] for m in msgs),
        tokens_cache_read=sum(m["cr"] for m in msgs),
        tokens_cache_write=sum(m["cw"] for m in msgs),
        llm_calls=len(msgs),
        usage=usage_by_model(msgs),
        subagents=subagent_entries(turn["subagents"], agent_types),
        tool_calls=len(turn["tools"]),
        tool_errors=len(turn["err"]),
        tool_denials=len(turn["deny"]),
        status="interrupted" if turn["interrupted"] else "ok",
    )
    return event.to_json()


def _collect_session(session_id, transcript, state, ctx):
    if not state["pins"]:
        pin(state, "", ctx.instruction_version)  # session began before the collector ran
    state["transcript"] = str(transcript)
    if not transcript.is_file():
        return []
    state["size"] = transcript.stat().st_size  # taken before reading: growth during the read is swept later
    workspace = _workspace(transcript)
    task = task_from_title(_scalar(workspace.get("name"))) or state.get("task")
    offset, meta = state["offset"], dict(state.get("meta") or {})
    if task != state.get("task") and offset:
        offset, meta = 0, {}  # the name came late: send the earlier turns again, now under it
    state["task"] = task
    turns = _build_turns(read_lines(transcript, offset), meta, git_worktrees(ctx.root))
    if not turns:
        state["meta"] = meta
        return []
    entrypoint = workspace.get("last_client_name") or workspace.get("client_name") or None
    fallback_branch = git_branch(ctx.root)
    events = []
    for turn in turns:
        turn["version"] = version_at(state["pins"], turn["start"])
        if turn["msgs"]:  # a turn without a model call is not an agent step
            events.append(_to_event(turn, session_id, ctx, fallback_branch, entrypoint,
                                    meta.get("agents") or {}, task))
    # Re-read from the last turn next time; keep what was known before it started.
    state["offset"] = turns[-1]["offset"]
    # The last turn is read again, so keep the meta as it was when that turn started.
    # An agent's type is named once, when it starts, and it may work on in later turns.
    state["meta"] = {"branch": turns[-1]["branch"], "worktree": turns[-1]["worktree"],
                     "client_version": turns[-1]["client_version"],
                     "effort": turns[-1]["default_effort"], "agents": meta.get("agents") or {}}
    return events


def _sweep(ctx, exclude):
    """Collect other sessions whose log grew since they were last read."""
    events, swept, now = [], 0, time.time()
    for path in sorted(ctx.state_dir.glob(f"{NAME}-*.json")):
        state = load_state(path)
        session_id = state.get("session_id")
        if not session_id or session_id == exclude:
            continue
        transcript = Path(state["transcript"]) if state.get("transcript") else None
        try:
            stale = now - path.stat().st_mtime > STATE_TTL_SECONDS
            if transcript is None or stale:
                retire_state(path, NAME, session_id)
                continue
            if not transcript.is_file():
                continue  # session started, log not written yet
            grew = transcript.stat().st_size != state.get("size")
        except OSError:
            continue
        if grew and swept < MAX_SWEEP:
            swept += 1
            events += _collect_session(session_id, transcript, state, ctx)
            save_state(path, state)
    return events


def _log_missing(hook_input):
    """Say that a turn or session end brought no session log; no path or session id in the line."""
    line = "Copilot session log not found: nothing recorded from this session"
    given = hook_input.get("transcript_path")
    if given and Path(given).name != "events.jsonl":
        line += " (the hook names a log format the collector does not read)"
    elif hook_input.get("hook_event_name") == "SessionEnd":
        line += " (expected if it had no prompt)"
    faf_log.LOGGER.info(line)


def collect(hook_input, ctx):
    session_id = hook_input.get("session_id")
    events = []
    if session_id and not hook_input.get("agent_id"):
        path = state_path(ctx, NAME, session_id)
        state = load_state(path, session_id)
        transcript = _transcript(hook_input, session_id)
        if hook_input.get("hook_event_name") == "SessionStart":
            pin(state, getattr(ctx, "started_at", None) or utcnow_iso(), ctx.instruction_version)
        elif not transcript.is_file():  # at session start the log may not be written yet
            _log_missing(hook_input)
        events += _collect_session(session_id, transcript, state, ctx)
        save_state(path, state)
    return events + _sweep(ctx, exclude=session_id)


# ---------------------------------------------------------------- migration

def _same_folder(a, b):
    return str(a or "").replace("\\", "/").rstrip("/").lower() == str(b).replace("\\", "/").rstrip("/").lower()


def sessions(root):
    """(session_id, events.jsonl) of every Copilot session whose git root is this repo."""
    base = _session_root({})
    found = []
    if base.is_dir():
        for folder in sorted(base.iterdir()):
            transcript = folder / "events.jsonl"
            if transcript.is_file():
                ws = _workspace(transcript)
                if _same_folder(_scalar(ws.get("git_root")) or _scalar(ws.get("cwd")), Path(root)):
                    found.append((folder.name, transcript))
    return found


def backfill(ctx, version, sessions_):
    """Turns of sessions this machine never read, pinned to `version` (theirs is not known)."""
    events = []
    for session_id, transcript in sessions_:
        path = state_path(ctx, NAME, session_id)
        if path.exists():
            continue
        state = load_state(path, session_id)
        pin(state, "", version)
        events += _collect_session(session_id, transcript, state, ctx)
        save_state(path, state)
    return events
