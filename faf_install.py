"""Install the Fleet Analytics collector into a git repository.

    python faf_install.py --target DIR [--providers all|claude-code,copilot] [--dry-run] [--yes]
                          [--upload-instructions yes|no] [--python-command NAME] [--uninstall]

Shows what it will change and what is sent, then asks to confirm (--yes when not
run interactively; --dry-run only shows). Safe to run again: config is kept, and
provider hooks are replaced rather than duplicated.
"""
import argparse
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required.")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faf_log  # noqa: E402
import faf_migrate  # noqa: E402
import faf_settings  # noqa: E402
from faf_event import ensure_gitignore, git_repo_slug  # noqa: E402
from faf_instructions import DEFAULT_INSTRUCTION_FILES, instruction_snapshot, read_config  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.common import DEFAULT_PYTHON  # noqa: E402

# Everything the installed collector needs, and nothing else (no .git, tests or caches).
COLLECTOR_FILES = (
    "faf_hook.py", "faf_post.py", "faf_event.py", "faf_instructions.py", "faf_settings.py",
    "faf_log.py", "faf_install.py", "faf_migrate.py", "faf_update.py",
    "providers/__init__.py", "providers/common.py", "providers/claude_code.py", "providers/copilot.py",
    "README.md", "LICENSE",
)
DEFAULT_CONFIG = {"upload_instruction_content": True, "instruction_files": list(DEFAULT_INSTRUCTION_FILES)}
LOCAL_IGNORE = "/.faf/local/"
# Files each provider's install creates or changes; the user commits them.
COMMIT_FILES = {"claude-code": ".claude/settings.json", "copilot": ".github/hooks/faf.json"}
DATA_SENT = (
    "one event per agent turn: repo name (owner/name), branch or session name, session id, start and end",
    "  time, instruction version hash, model, effort, permission mode, agent version, client,",
    "  token and model call counts (also per model, subagents included), tool call counts,",
    "  tool errors and denials, status",
    "per subagent of a turn: an opaque agent id, agent type name, model, start and end time, token,",
    "  model call, tool call, tool error and denial counts",
    "per new instruction version: its hash, the previous hash, commit, dirty flag, and per changed",
    "  file its kind, added/removed/changed, line counts and size (no paths)",
    "instruction file paths and text only if you allow the instruction upload for this repo",
    "update check: at most one request a day to the public release page, no key, repo or session",
    "never: prompts (except what a session name repeats of them: Copilot's can be the first line",
    "  of your prompt), answers, code, tool arguments or output",
)
log = faf_log.LOGGER


def is_git_root(path):
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    return out.returncode == 0 and Path(out.stdout.strip()).resolve() == path.resolve()


def interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def choose_providers(arg):
    if not arg and interactive():
        print("Available providers: " + ", ".join(PROVIDERS))
        arg = input("Providers to install [all]: ") or "all"
    elif not arg:
        raise SystemExit("--providers is required when not run interactively. "
                         f"Valid values: all, {', '.join(PROVIDERS)}")
    names = [n.strip() for n in arg.split(",") if n.strip()]
    if names == ["all"]:
        names = list(PROVIDERS)
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        raise SystemExit(f"unknown provider(s): {', '.join(unknown)}. Valid: {', '.join(PROVIDERS)}")
    return [PROVIDERS[n] for n in names]


def collector_destination(target):
    """(folder the hooks run from, whether it is copied)."""
    dest = target / ".faf" / "collector"
    if HERE.resolve() == dest.resolve():
        return dest, False
    if HERE.resolve().is_relative_to(target.resolve()):
        return HERE, False  # the collector's own repo: run the source, a copy would only go stale
    return dest, True


def copy_collector(dest):
    """Copy COLLECTOR_FILES into dest, replacing an earlier copy."""
    if dest.exists():
        shutil.rmtree(dest)
    for rel in COLLECTOR_FILES:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / rel, out)


def ensure_config(target):
    path = target / ".faf" / "config.json"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    return True


def check_python(command):
    """Warn when the hook's python command is missing or too old on this machine."""
    if not shutil.which(command):
        return f"warning: '{command}' is not on PATH; hooks will fail until it is (or use --python-command)"
    out = subprocess.run([command, "-c", "import sys; print(sys.version_info >= (3, 9))"],
                         capture_output=True, text=True)
    if out.stdout.strip() != "True":
        return f"warning: '{command}' is not Python 3.9 or newer"
    return None


def show_plan(target, providers, dest, copied, repo):
    """Console only: what will change, what is sent, and which files an upload would send."""
    rel = lambda p: p.resolve().relative_to(target).as_posix() if p.resolve().is_relative_to(target) else str(p)
    print(f"Fleet Analytics collector v{faf_settings.COLLECTOR_VERSION} for {target}")
    print("Files:")
    if copied:
        print(f"  {rel(dest)}/  (copy of the collector: {len(COLLECTOR_FILES)} files)")
    else:
        print(f"  {rel(dest)}/  (used in place)")
    print("  .faf/config.json  (created if missing)")
    for p in providers:
        print(f"  {COMMIT_FILES.get(p.NAME, '?')}  ({p.NAME} hooks)")
    print(f"  .gitignore  ({LOCAL_IGNORE})")
    print(f"  {faf_settings.settings_path()}  (your API key and upload choice, if you enter them)")
    print("Hooks:")
    for p in providers:
        events = getattr(p, "EVENTS", {})
        print(f"  {p.NAME}: {', '.join(events)}")
    print(f"Sends to: {faf_settings.endpoint_host()} (HTTPS)")
    print("Data sent:")
    for line in DATA_SENT:
        print(f"  {line}")
    config = read_config(target)
    paths = sorted({f["path"] for p in providers for f in instruction_snapshot(target, p, config=config)[1]})
    print(f"Instruction files an upload would send for {repo} ({len(paths)}):")
    for path in paths or ["(none)"]:
        print(f"  {path}")
    print("Migration (old hooks and data, so nothing is lost):")
    for line in faf_migrate.plan(target, providers) or ["(nothing to migrate)"]:
        print(f"  {line}")


def confirm(question, default=False):
    """The answer as a bool; None when there is no input to read."""
    try:
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        return None
    return default if not answer else answer in ("y", "yes")


def ask_upload(repo, choice, config):
    """Whether the upload is on. A stored choice changes only through the flag; a new one is
    stored only when the user answers the prompt (default No)."""
    if choice is not None:
        faf_settings.set_upload_consent(repo, choice == "yes")
    elif faf_settings.upload_consent(repo) is None and interactive():
        # isatty() is also true for NUL on Windows, so the prompt may get no answer.
        answer = confirm("Upload the text of these instruction files (enables the file diff in the dashboard)?")
        if answer is not None:
            faf_settings.set_upload_consent(repo, answer)
    return faf_settings.upload_enabled(repo, config)


def ask_key():
    """Prompt for the API key when none is configured. Returns True when one is set."""
    if faf_settings.has_key():
        return True
    if interactive():
        key = getpass.getpass("API key (input hidden, empty to skip): ").strip()
        while key and not faf_settings.valid_key(key):
            key = getpass.getpass("That is not an API key (faf_...). Try again, or empty to skip: ").strip()
        if key:
            faf_settings.store_key(key)
            return True
    print(f"No API key configured: nothing is recorded yet. Fleet Analytics is invite only; "
          f"request an invite at {faf_settings.INVITE_URL}")
    print(f"Then put the key into {faf_settings.settings_path()} as \"api_key\", "
          f"or set {faf_settings.KEY_ENV}.")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="faf_install")
    parser.add_argument("--target", default=".")
    parser.add_argument("--providers", default=None)
    parser.add_argument("--python-command", default=None,
                        help="interpreter the committed hooks call (default: Claude Code python3, then "
                             "python; Copilot python3 on Linux/macOS, python on Windows)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    parser.add_argument("--yes", action="store_true", help="install without asking to confirm")
    parser.add_argument("--upload-instructions", choices=("yes", "no"), default=None,
                        help="allow or refuse the instruction text upload for this repo")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not is_git_root(target):
        raise SystemExit(f"{target} is not the root of a git repository.")
    providers = choose_providers(args.providers)

    if args.uninstall:
        faf_log.setup(target, console=True, event="install")
        try:
            for provider in providers:
                for line in provider.uninstall(target):
                    log.info(line)
        finally:
            faf_log.close()
        return 0

    repo = git_repo_slug(target)
    dest, copied = collector_destination(target)
    show_plan(target, providers, dest, copied, repo)
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0
    if not args.yes:
        if not interactive():
            raise SystemExit("Not run interactively: pass --yes to install (or --dry-run to look).")
        if not confirm("Install?"):
            print("Nothing changed.")
            return 1

    (target / ".faf" / "local").mkdir(parents=True, exist_ok=True)
    faf_log.setup(target, console=True, event="install")
    try:
        log.info(f"installing v{faf_settings.COLLECTOR_VERSION}")
        if copied:
            copy_collector(dest)
            log.info(".faf/collector/ copied")
        if ensure_config(target):
            log.info(".faf/config.json created")
        if ensure_gitignore(target, LOCAL_IGNORE):
            log.info(".gitignore changed")
        hook_script = (dest / "faf_hook.py").resolve().relative_to(target).as_posix()
        for provider in providers:
            for line in provider.install(target, args.python_command, hook_script):
                log.info(line)
        warning = check_python(args.python_command or DEFAULT_PYTHON)
        if warning:
            log.info(warning)
        upload = ask_upload(repo, args.upload_instructions, read_config(target))
        ask_key()
        for line in faf_migrate.migrate(target, providers, faf_migrate.context):
            log.info(f"migration: {line}")
        host = faf_settings.endpoint_host()
        log.info(f"sends to {host}, instruction upload {'on' if upload else 'off'}")
        if not upload:
            log.info(faf_settings.upload_reminder(repo, read_config(target)))
        log.info("installed")
    finally:
        faf_log.close()

    files = ([".faf/collector/"] if copied else []) + [".faf/config.json"]
    files += [COMMIT_FILES[p.NAME] for p in providers if p.NAME in COMMIT_FILES] + [".gitignore"]
    print("Next steps:")
    print(f"  1. Commit {', '.join(files)}.")
    print("  2. Start a new agent session; events are sent at the end of each turn.")
    print("  3. Watch .faf/local/collector.log for 'sent ... events'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
