"""Helpers shared by provider modules."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Interpreter the committed hooks call when the installer is not told otherwise.
DEFAULT_PYTHON = "python" if sys.platform == "win32" else "python3"


# ---------------------------------------------------------------- time

def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- transcripts

def read_lines(path, offset):
    """Yield (start_offset, obj) for complete JSON lines from offset on."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        pos = offset
        for raw in fh:
            start = pos
            pos += len(raw)
            if not raw.endswith(b"\n"):
                break  # still being written
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield start, obj


def main_model(records):
    """Model with the most output tokens among the main conversation's calls.

    Subagent calls (record["sub"]) count for tokens but not for the model, so a
    turn of an Opus conversation whose Haiku helpers wrote more is still Opus.
    Falls back to all calls when the turn has no main-conversation call.
    """
    main = [r for r in records if not r.get("sub")] or records
    per_model = {}
    for r in main:
        per_model[r["model"]] = per_model.get(r["model"], 0) + r["out"]
    return max(per_model.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_model else None


def usage_by_model(records):
    """Token and call counts per model over all of a turn's calls, subagents included.

    The step's model names the main conversation; this breakdown is what the backend prices,
    so a Sonnet turn whose Opus subagents did most of the work is charged at Opus rates.
    Short keys keep the event small; sorted by model so a resend is byte-identical.
    """
    per_model = {}
    for r in records:
        u = per_model.setdefault(r["model"], {"model": r["model"], "in": 0, "out": 0, "cr": 0,
                                              "cw": 0, "cw1h": 0, "calls": 0})
        for k in ("in", "out", "cr", "cw", "cw1h"):
            u[k] += r.get(k, 0)
        u["calls"] += 1
    return [per_model[m] for m in sorted(per_model)]


# ---------------------------------------------------------------- subagents

MAX_SUBAGENTS = 200  # entries per event; the backend refuses a longer list
# A name such as "Explore" or "plugin:code-reviewer": no spaces or paths, so never a sentence.
_AGENT_TYPE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def agent_type_name(value):
    """value when it is a short plain name, which may be sent as an agent type, else None."""
    return value if isinstance(value, str) and _AGENT_TYPE.fullmatch(value) else None


def count_item(target, kind, key, payload=None):
    """Count a model call ("msg"), tool call ("tool") or failed one ("err", "deny") in a turn
    or in a subagent's record; a key counts once."""
    if kind == "msg":
        target["msgs"].setdefault(key, payload)
    else:
        target["tools" if kind == "tool" else kind].add(key)


def subagent_record(turn, agent_id, ts):
    """The record of this subagent's share of the turn, its time span widened to ts."""
    record = turn["subagents"].setdefault(
        agent_id, {"start": ts, "end": ts, "msgs": {}, "tools": set(), "err": set(), "deny": set()})
    record["start"], record["end"] = min(record["start"], ts), max(record["end"], ts)
    return record


def subagent_entries(records, agent_types):
    """The `subagents` list of a step event from a turn's records, or None when it has none.

    Earliest start first; those beyond MAX_SUBAGENTS are left out (the turn's totals still
    count them). agent_types: {agent id: type name} of the agents whose type is known.
    """
    entries = []
    for agent_id, r in sorted(records.items(), key=lambda kv: (kv[1]["start"], kv[0]))[:MAX_SUBAGENTS]:
        msgs = list(r["msgs"].values())
        entry = {"agent_id": agent_id, "model": main_model(msgs) or "unknown",
                 "ts_start": iso(r["start"]), "ts_end": iso(r["end"]),
                 **{k: sum(m.get(k, 0) for m in msgs) for k in ("in", "out", "cr", "cw", "cw1h")},
                 "calls": len(msgs), "tool_calls": len(r["tools"]),
                 "tool_errors": len(r["err"]), "tool_denials": len(r["deny"])}
        if agent_types.get(agent_id):
            entry["agent_type"] = agent_types[agent_id]
        entries.append(entry)
    return entries or None


# "Task:", "TaskId:", "Task ID =", "task-id:", "task_id:" in front of a session name is not part of it.
TASK_PREFIX = re.compile(r"^task[\s_-]*(?:id)?\s*[:=]\s*", re.IGNORECASE)
MAX_TASK_NAME = 120


def task_from_title(title):
    """The task name a session name gives: its first line without a "Task:" prefix, else None."""
    lines = (title or "").strip().splitlines()
    name = TASK_PREFIX.sub("", lines[0].strip()) if lines else ""
    return name[:MAX_TASK_NAME].rstrip() or None


def boundary(branch, session_id, ctx, worktree=None, task=None):
    """Task boundary, by importance: the branch, the worktree, the task named by the session,
    else <branch>@<session prefix> on the trunk or <detached>@<session prefix> (one session, one task)."""
    on_trunk = branch in (None, "", "HEAD") or branch == getattr(ctx, "default_branch", None)
    if branch and not on_trunk:
        return branch
    named = worktree or task
    if named:
        return named
    return f"{branch if branch not in (None, '', 'HEAD') else 'detached'}@{session_id[:8]}"


# ---------------------------------------------------------------- per-session state

def state_path(ctx, provider_name, session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return ctx.state_dir / f"{provider_name}-{safe}.json"


def load_state(path, session_id=None):
    state = {}
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("session_id", session_id)
    state.setdefault("offset", 0)
    state.setdefault("pins", [])
    if not state["pins"] and state.get("session_version"):
        state["pins"] = [["", state["session_version"]]]  # state written by an older collector
    return state


def save_state(path, state):
    keep = ("session_id", "transcript", "size", "offset", "pins", "meta", "task")
    path.write_text(json.dumps({k: state.get(k) for k in keep}), encoding="utf-8")


def retired_path(state_dir, provider_name):
    return Path(state_dir) / f"retired-{provider_name}.json"


def retired_sessions(state_dir, provider_name):
    """Ids of sessions whose cursor expired: read before, so never backfilled again."""
    try:
        data = json.loads(retired_path(state_dir, provider_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def retire_state(path, provider_name, session_id):
    """Delete an expired cursor and remember its session as read."""
    if session_id:
        ids = retired_sessions(path.parent, provider_name) | {session_id}
        retired_path(path.parent, provider_name).write_text(json.dumps(sorted(ids)), encoding="utf-8")
    path.unlink()


def pin(state, ts, version):
    """Record that the agent loaded `version` at `ts` ('' = since the session began)."""
    if not state["pins"] or state["pins"][-1][1] != version:
        state["pins"].append([ts, version])


def version_at(pins, start):
    version = pins[0][1]
    for ts, pinned in pins:
        at = parse_ts(ts)
        if at is None or at <= start:
            version = pinned
    return version
