"""Instruction version: a content hash of the instruction files of one repo.

Both the version and the uploaded snapshot come from instruction_snapshot(),
so they cannot disagree. change_shape() compares two snapshots without paths:
per changed file its kind, status, changed line counts and size.
"""
import difflib
import hashlib
import json
import os
import posixpath
import re
import subprocess
from pathlib import Path

MAX_CONTENT_BYTES = 200_000
MAX_TOTAL_CONTENT_BYTES = 5_000_000
MAX_IMPORT_DEPTH = 5
SKIP_DIRS = {".git", "node_modules", ".faf"}
KINDS = ("memory", "rule", "skill", "agent", "command", "prompt", "import", "other")
MEMORY_NAMES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md", "copilot-instructions.md"}
KIND_PREFIXES = (
    (".claude/rules/", "rule"), (".github/instructions/", "rule"),
    (".claude/skills/", "skill"), (".github/skills/", "skill"),
    (".claude/agents/", "agent"), (".github/agents/", "agent"),
    (".claude/commands/", "command"), (".github/prompts/", "prompt"),
)

# instruction_files of .faf/config.json: the default setup, edited by the user. Globs: "**/" matches
# any number of folders (none included), "**" any path, "*" any name within one folder, "?" one character.
DEFAULT_INSTRUCTION_FILES = [
    "**/CLAUDE.md", "**/AGENTS.md", "GEMINI.md",
    ".claude/rules/**/*.md", ".claude/skills/**", ".claude/agents/**/*.md",
    ".claude/commands/**/*.md", ".claude/output-styles/**/*.md",
    ".github/copilot-instructions.md", ".github/instructions/**/*.instructions.md",
    ".github/agents/**/*.md", ".github/prompts/**/*.prompt.md", ".github/skills/**",
]

_CODE_FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_IMPORT = re.compile(r"(?:^|(?<=\s))@([^\s`]+)")


def read_config(root):
    path = Path(root) / ".faf" / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def candidate_paths(root):
    """Git-visible files (tracked, plus untracked and not ignored), as POSIX paths.

    Files that are tracked but deleted on disk, and symlinks that resolve
    outside the repo, are dropped. Falls back to a
    folder walk when git is not available.
    """
    root = Path(root)
    paths = None
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=30,
        )
        if out.returncode == 0:
            paths = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    except Exception:
        paths = None
    if paths is None:
        paths = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                rel = Path(dirpath, name).relative_to(root).as_posix()
                paths.append(rel)
    result = []
    real_root = root.resolve()
    for p in paths:
        p = p.replace("\\", "/")
        if p.split("/", 1)[0] == ".faf":
            continue
        if (root / p).is_file() and _inside(root / p, real_root):
            result.append(p)
    return sorted(set(result))


def _inside(path, real_root):
    """True when path, symlinks resolved, stays under real_root: a link out of the repo is never read."""
    try:
        path.resolve().relative_to(real_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def import_targets(text, rel):
    """Repo-relative paths that `@path` imports in text point to, resolved from rel's folder.

    Imports inside code blocks and code spans don't count, and paths that leave
    the repo (absolute, ~/..., ../ above the root) are ignored.
    """
    text = _CODE_SPAN.sub("", _CODE_FENCE.sub("", text))
    targets = []
    for match in _IMPORT.finditer(text):
        target = match.group(1).rstrip(".,;:!?)]}>\"'")
        if not target or target.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", target):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
        if resolved == ".." or resolved.startswith("../"):
            continue
        targets.append(resolved)
    return targets


def glob_regex(pattern):
    """Compiled regex for one instruction_files glob (see DEFAULT_INSTRUCTION_FILES)."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            c = pattern[i]
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
    return re.compile("".join(out))


def _follow_imports(root, start, allowed, follows):
    """Git-visible files imported (transitively, up to MAX_IMPORT_DEPTH hops) by the start files."""
    found = set()
    frontier = [p for p in start if follows(p)]
    for _ in range(MAX_IMPORT_DEPTH):
        nxt = []
        for rel in frontier:
            try:
                text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for target in import_targets(text, rel):
                if target in allowed and target not in found and target not in start:
                    found.add(target)
                    nxt.append(target)
        frontier = nxt
        if not frontier:
            break
    return found


def file_kind(path, imported=False):
    """memory, rule, skill, agent, command, prompt, import or other."""
    if imported:
        return "import"
    if posixpath.basename(path) in MEMORY_NAMES:
        return "memory"
    for prefix, kind in KIND_PREFIXES:
        if path.startswith(prefix):
            return kind
    return "other"


def instruction_snapshot(root, provider_module, config=None):
    """Return (version, files). version is 'none' and files is [] when there are no instruction files.

    Each file: path, sha256, size (bytes), kind, and content (text, or None for
    binary files and files over MAX_CONTENT_BYTES). Content stays on this machine
    unless the user consented to the upload (see upload_files()).
    """
    root = Path(root)
    config = read_config(root) if config is None else config
    paths = candidate_paths(root)
    selected = set()
    for pattern in config.get("instruction_files", DEFAULT_INSTRUCTION_FILES):
        rx = glob_regex(pattern)
        selected.update(p for p in paths if rx.fullmatch(p))
    imported = set()
    follows = getattr(provider_module, "follows_imports", None)
    if follows:
        imported = _follow_imports(root, selected, set(paths), follows)
        selected |= imported
    selected = sorted(p for p in selected if p.split("/", 1)[0] != ".faf")

    digest = hashlib.sha256()
    files = []
    for rel in selected:
        try:
            raw = (root / rel).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue  # deleted since it was listed
        digest.update(rel.encode("utf-8") + b"\0" + raw + b"\0")
        content = None
        if len(raw) <= MAX_CONTENT_BYTES and b"\0" not in raw[:8192]:
            try:
                content = raw.decode("utf-8")  # exact bytes only: the server checks content against sha256
            except UnicodeDecodeError:
                content = None
        files.append({
            "path": rel,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "kind": file_kind(rel, rel in imported),
            "content": content,
        })
    if not files:
        return "none", []
    return digest.hexdigest()[:12], files


def upload_files(files):
    """The `files` of an instruction_version event when the upload is on: path, sha256 and
    content, with content left out once the version passes MAX_TOTAL_CONTENT_BYTES."""
    budget = MAX_TOTAL_CONTENT_BYTES
    out = []
    for f in files:
        content = f.get("content")
        if content is not None:
            size = len(content.encode("utf-8"))
            if size > budget:
                content = None
            else:
                budget -= size
        out.append({"path": f["path"], "sha256": f["sha256"], "content": content})
    return out


def local_snapshot(version, files):
    """What the state file keeps to diff the next version against."""
    return {"version": version, "files": {f["path"]: {k: f.get(k) for k in ("sha256", "size", "kind", "content")}
                                          for f in files}}


def _lines(text):
    return text.splitlines() if text else []


def change_shape(previous, files):
    """Changed files of `files` against `previous` (a local_snapshot()['files'] dict, or None
    for the first version on this machine), without paths. Sorted by path, so stable."""
    previous = previous or {}
    current = {f["path"]: f for f in files}
    changes = []
    for path in sorted(set(previous) | set(current)):
        old, new = previous.get(path) or {}, current.get(path) or {}
        if old and new and old.get("sha256") == new.get("sha256"):
            continue
        if new and not old:
            status, added, removed = "added", len(_lines(new.get("content"))), 0
        elif old and not new:
            status, added, removed = "removed", 0, len(_lines(old.get("content")))
        else:
            status, added, removed = "changed", 0, 0
            if old.get("content") is not None and new.get("content") is not None:
                matcher = difflib.SequenceMatcher(None, _lines(old["content"]), _lines(new["content"]),
                                                  autojunk=False)
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag in ("replace", "delete"):
                        removed += i2 - i1
                    if tag in ("replace", "insert"):
                        added += j2 - j1
        source = new or old
        kind = source.get("kind") if source.get("kind") in KINDS else file_kind(path)
        changes.append({"kind": kind, "status": status, "lines_added": added, "lines_removed": removed,
                        "size": int(source.get("size") or 0)})
    return changes


def instruction_version(root, provider_module):
    return instruction_snapshot(root, provider_module)[0]


def git_dirty(root, paths):
    """True when any of the given paths differs from HEAD or is untracked."""
    if not paths:
        return False
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception:
        return False
    return out.returncode == 0 and bool(out.stdout.strip())
