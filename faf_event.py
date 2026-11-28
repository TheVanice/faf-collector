"""faf.v1 event envelopes, git helpers and the local JSONL spool."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "faf.v1"


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(args, cwd):
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, encoding="utf-8", errors="replace",
            cwd=cwd, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_branch(cwd=None):
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return branch if branch and branch != "HEAD" else None


def git_default_branch(cwd=None):
    """The branch origin/HEAD points to; else a local main or master; else None."""
    ref = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd)
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    for name in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd):
            return name
    return None


def _norm_path(path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def git_worktrees(cwd=None) -> dict:
    """{normalized path: branch (folder name if detached)} of the worktrees other than the main one.

    Agents create named worktrees themselves, so the session's own branch never changes.
    """
    out = _git(["worktree", "list", "--porcelain"], cwd)
    result = {}
    for block in (out or "").replace("\r", "").split("\n\n")[1:]:  # first block: the main checkout
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path = fields.get("worktree")
        if path:
            branch = fields.get("branch", "").removeprefix("refs/heads/")
            result[_norm_path(path)] = branch or Path(path).name
    return result


def git_head(cwd=None):
    return _git(["rev-parse", "HEAD"], cwd)


def repo_root(cwd=None) -> Path:
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else Path(cwd or os.getcwd()).resolve()


def _remote_parts(url):
    """(host, path parts) of a git remote URL. User info, password and port are dropped."""
    url = url.replace("\\", "/")
    # A query string or fragment is never part of the path: it may hold a token.
    for mark in "?#":
        url = url.split(mark, 1)[0]
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    scheme_form = "://" in url
    rest = url.split("://", 1)[1] if scheme_form else url
    # The last "@" ends the user info, even when a password holds "/" or ":".
    rest = rest.rsplit("@", 1)[-1]
    if scheme_form:
        authority, _, path = rest.partition("/")
    else:
        authority, sep, path = rest.partition(":")  # scp form: [user@]host:path
        if not sep or "/" in authority:
            authority, path = "", rest
    host = authority.split(":", 1)[0]
    return host, [part for part in path.split("/") if part]


def git_repo_slug(root) -> str:
    """'Owner/Name' from the origin URL; folder name when there is no origin.

    The host is only used for a one-part path, and never with its user info, so a
    token in the remote URL cannot reach the event, the dashboard or the settings file.
    """
    url = _git(["remote", "get-url", "origin"], root)
    if url:
        host, parts = _remote_parts(url)
        if len(parts) >= 3 and parts[-2] == "_git":
            # Azure DevOps: <org>/<project>/_git/<repo>. The project is the owner.
            parts = parts[:-2] + parts[-1:]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
        if host and parts:
            return f"{host}/{parts[0]}"
    return Path(root).name


def local_dir(root, create=True) -> Path:
    """Per-machine state folder (git-ignored): <root>/.faf/local."""
    path = Path(root) / ".faf" / "local"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_gitignore(root, line):
    """Add line to <root>/.gitignore if it is not there yet. Safe to repeat."""
    path = Path(root) / ".gitignore"
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if line in [entry.strip() for entry in text.splitlines()]:
        return False
    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{line}\n", encoding="utf-8")
    return True


def make_event_id(provider, session_id, turn_key) -> str:
    raw = f"{provider}|{session_id}|{turn_key}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class StepEvent:
    """One agent turn: a prompt plus everything the agent does until it waits again."""
    event_id: str
    provider: str
    repo: str
    boundary_ref: str
    session_id: str
    ts_start: str
    ts_end: str
    instruction_version: str
    trigger: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    client_version: str | None = None
    entrypoint: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    tokens_cache_write_1h: int | None = None
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_errors: int | None = None
    tool_denials: int | None = None
    status: str | None = None
    # [{model, in, out, cr, cw, cw1h, calls}]: per-model split of the token and call counts
    usage: list | None = None
    # [{agent_id, agent_type?, model, ts_start, ts_end, in, out, cr, cw, cw1h, calls, tool_calls,
    #   tool_errors, tool_denials}]: each subagent's share of this turn, already in the totals
    subagents: list | None = None
    schema: str = field(default=SCHEMA)
    kind: str = field(default="step")

    def to_json(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class InstructionVersionEvent:
    """Registers an instruction version the first time a machine sees it.

    Nulls are kept: the backend needs to know that previous_version is null.
    files is [] unless the user consented to the instruction upload. changes is
    the change shape against the previous version (no paths); left out when unknown.
    """
    provider: str
    repo: str
    version: str
    previous_version: str | None
    ts: str
    head_sha: str | None = None
    dirty: bool = False
    files: list = field(default_factory=list)
    changes: list | None = None
    schema: str = field(default=SCHEMA)
    kind: str = field(default="instruction_version")

    def to_json(self):
        data = asdict(self)
        if data["changes"] is None:
            del data["changes"]
        return data


def _read_jsonl(path):
    """Return the parsed lines, or None if the file vanished. Bad lines are skipped."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return None
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


class Spool:
    """Append-only JSONL spool that survives offline runs.

    A flush renames the live file before reading it, so events appended during
    a flush land in a new file and are never deleted by mistake.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event):
        data = event.to_json() if hasattr(event, "to_json") else event
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(data) + "\n")

    def take_batches(self):
        """Return [(batch_path, events)] for every batch waiting to be sent.

        The caller deletes a batch file only after its POST succeeded. A batch
        that failed stays on disk and is returned again next time.
        """
        if self.path.exists():
            stamp = time.time_ns()
            # The clock can be coarse (Windows, Python < 3.13): never reuse the name of a waiting batch.
            while (target := self.path.with_name(f"spool.{stamp}.sending.jsonl")).exists():
                stamp += 1
            try:
                os.rename(self.path, target)  # unlike replace, does not overwrite on Windows
            except OSError:
                pass  # locked on Windows; the events stay in spool.jsonl
        batches = []
        for batch_path in sorted(self.path.parent.glob("spool.*.sending.jsonl")):
            events = _read_jsonl(batch_path)
            if events is not None:
                batches.append((batch_path, events))
        return batches

