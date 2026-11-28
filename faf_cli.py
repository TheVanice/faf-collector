"""The `faf` command: install the collector once per machine, then `faf init` per project.

    faf install [--init] [--providers ...] [--yes]  set up this machine (API key); --init also runs `faf init` here
    faf init [DIR] [--providers ...] add the hooks to a git repository (options as faf_install.py)
    faf uninit [DIR] [--providers]   remove the hooks from a repository
    faf upgrade [VERSION] [--check]  install the latest (or given) release of the faf command
    faf uninstall [--yes]            remove the faf command (projects and ~/.faf/settings.json stay)
    faf version

The one-line installers (install.ps1, install.sh) download and verify a release, then run
`faf_cli.py self-install`, which copies it to ~/.faf/cli/<version>/ and puts `faf` on PATH.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required.")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faf_install  # noqa: E402
import faf_settings  # noqa: E402
import faf_verify  # noqa: E402
from providers.common import DEFAULT_PYTHON  # noqa: E402

RELEASES_URL = "https://github.com/TheVanice/faf-collector/releases"
RELEASES_ENV = "FAF_RELEASES_URL"  # another release host, for testing the installers
CLI_FILES = faf_install.COLLECTOR_FILES + ("faf_cli.py", "faf_verify.py")
WINDOWS = sys.platform == "win32"


def cli_root():
    return Path.home() / ".faf" / "cli"


def bin_dir():
    return Path.home() / (".faf/bin" if WINDOWS else ".local/bin")


def installed_python():
    """The interpreter the launcher runs (it sets FAF_PYTHON), so an upgrade keeps it."""
    return os.environ.get("FAF_PYTHON") or DEFAULT_PYTHON


def releases_url():
    """The built-in release host, or FAF_RELEASES_URL when it is https (http for localhost).
    Any other value exits before anything is downloaded."""
    host = os.environ.get(RELEASES_ENV, "").rstrip("/")
    if not host:
        return RELEASES_URL
    if faf_settings.endpoint_problem(host):
        raise SystemExit(f"{RELEASES_ENV} must use https (http only for localhost): {host}. Nothing changed.")
    return host


def version_key(version):
    try:
        return tuple(int(p) for p in version.lstrip("v").split("."))
    except ValueError:
        raise SystemExit(f"not a version: {version}") from None


# --- installing the command -------------------------------------------------------------------

def write_launchers(target, python):
    """`faf` (sh, also for Git Bash) and on Windows `faf.cmd`, both running target/faf_cli.py."""
    out = bin_dir()
    out.mkdir(parents=True, exist_ok=True)
    script = target / "faf_cli.py"
    sh = out / "faf"
    # write_bytes keeps the line endings exact (write_text's newline= needs Python 3.10).
    sh.write_bytes(f'#!/bin/sh\nFAF_PYTHON={python} exec {python} "{script.as_posix()}" "$@"\n'.encode())
    sh.chmod(0o755)
    paths = [sh]
    if WINDOWS:
        cmd = out / "faf.cmd"
        cmd.write_bytes(f'@setlocal & set "FAF_PYTHON={python}" & "{python}" "{script}" %*\r\n'.encode())
        paths.append(cmd)
    return paths


def _user_path(update):
    """Windows: read the user PATH, store update(entries) if it differs. Returns True when changed."""
    if sys.platform != "win32":  # also tells the type checker that the rest is Windows only
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        entries = [e for e in value.split(";") if e]
        new = update(entries)
        if new == entries:
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(new))
    try:  # tell running programs (Explorer, new terminals) that the environment changed
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except (AttributeError, OSError):
        pass
    return True


def _same_dir(a, b):
    return os.path.normcase(os.path.normpath(os.path.expandvars(a))) == os.path.normcase(os.path.normpath(b))


def add_to_path():
    """Put bin_dir() on the user PATH (Windows) or say how to (elsewhere). Returns a message or None."""
    folder = str(bin_dir())
    if WINDOWS:
        changed = _user_path(lambda e: e if any(_same_dir(x, folder) for x in e) else e + [folder])
        return f"added {folder} to your user PATH: open a new terminal to use faf" if changed else None
    if any(_same_dir(x, folder) for x in os.environ.get("PATH", "").split(os.pathsep) if x):
        return None
    return f'{folder} is not on PATH: add  export PATH="$HOME/.local/bin:$PATH"  to your shell profile'


def remove_from_path():
    if WINDOWS:
        folder = str(bin_dir())
        _user_path(lambda e: [x for x in e if not _same_dir(x, folder)])


def self_install(source, python):
    """Copy the CLI from source to ~/.faf/cli/<version>/, write the launchers, drop other versions."""
    version = faf_settings.COLLECTOR_VERSION
    target = cli_root() / version
    if Path(source).resolve() != target.resolve():
        staging = cli_root() / f".{version}.new"
        shutil.rmtree(staging, ignore_errors=True)
        for rel in CLI_FILES:
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(source) / rel, out)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    for old in cli_root().iterdir():
        if old.name != version:
            shutil.rmtree(old, ignore_errors=True)
    write_launchers(target, python)
    print(f"faf {version} installed in {target}")
    note = add_to_path()
    if note:
        print(note)
    other = shutil.which("faf")
    if other and not _same_dir(str(Path(other).parent), str(bin_dir())):
        print(f"warning: another faf at {other} comes first on PATH")
    return target


# --- releases ----------------------------------------------------------------------------------

def fetch(url):
    try:
        with urlopen(url, timeout=60) as response:  # noqa: S310
            return response.read(), response.geturl()
    except (URLError, OSError) as exc:
        raise SystemExit(f"cannot download {url}: {getattr(exc, 'reason', exc)}") from exc


def latest_tag():
    _, final = fetch(f"{releases_url()}/latest")  # redirects to .../releases/tag/vX.Y.Z
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    if not tag.startswith("v"):
        raise SystemExit(f"could not find the latest release at {releases_url()}")
    return tag


def download_release(tag, into):
    """Download release tag and check its signed checksum; returns the folder holding the unpacked collector."""
    name = f"faf-collector-{tag}.zip"
    data, _ = fetch(f"{releases_url()}/download/{tag}/{name}")
    checksum, _ = fetch(f"{releases_url()}/download/{tag}/{name}.sha256")
    try:
        signature, _ = fetch(f"{releases_url()}/download/{tag}/{name}.sha256.sig")
    except SystemExit:
        signature = b""
    problem = faf_verify.check_release(name, data, checksum, signature)
    if problem:
        raise SystemExit(f"{problem}. Nothing changed.")
    archive = Path(into) / name
    archive.write_bytes(data)
    with zipfile.ZipFile(archive) as z:
        z.extractall(Path(into) / "unpacked")
    source = Path(into) / "unpacked" / "faf"
    if not (source / "faf_cli.py").exists():
        raise SystemExit(f"{name} does not contain faf/faf_cli.py. Nothing changed.")
    return source


# --- commands ----------------------------------------------------------------------------------

def git_root(path):
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"{Path(path).resolve()} is not inside a git repository.")
    return out.stdout.strip()


def cmd_install(args):
    print(f"Fleet Analytics collector {faf_settings.COLLECTOR_VERSION}")
    warning = faf_install.check_python(DEFAULT_PYTHON)
    if warning:
        print(warning)
    if faf_settings.has_key() and faf_install.interactive() and not args.yes:
        if faf_install.confirm("An API key is configured. Replace it?"):
            settings = faf_settings.load()
            settings.pop("api_key", None)
            faf_settings.save(settings)
    has_key = faf_install.ask_key()
    if has_key:
        print(f"API key configured ({faf_settings.settings_path()}).")
    if args.init:
        providers = args.providers or ("all" if args.yes else None)
        argv = ["--target", git_root(".")] + ["--providers", providers] * bool(providers)
        return faf_install.main(argv + ["--yes"] * args.yes)
    print("Next: in each project, run  faf init")
    return 0


def cmd_init(args):
    argv = ["--target", git_root(args.dir)]
    for name in ("providers", "upload_instructions", "python_command"):
        if getattr(args, name):
            argv += [f"--{name.replace('_', '-')}", getattr(args, name)]
    argv += ["--dry-run"] * args.dry_run + ["--yes"] * args.yes
    return faf_install.main(argv)


def cmd_uninit(args):
    return faf_install.main(["--target", git_root(args.dir), "--providers", args.providers, "--uninstall"])


def cmd_upgrade(args):
    current = faf_settings.COLLECTOR_VERSION
    host = releases_url()  # validated before the first request
    if host != RELEASES_URL:
        print(f"Using releases from {host} ({RELEASES_ENV}).")
    tag = args.version if args.version else latest_tag()
    tag = tag if tag.startswith("v") else f"v{tag}"
    newer = version_key(tag) > version_key(current)
    if args.check:
        print(f"installed {current}, latest {tag.lstrip('v')}" + (": run  faf upgrade" if newer else ""))
        return 0
    if not newer and not args.version and not args.force:
        print(f"faf {current} is up to date.")
        return 0
    with tempfile.TemporaryDirectory() as temp:
        source = download_release(tag, temp)
        out = subprocess.run([args.python, str(source / "faf_cli.py"), "self-install", "--python", args.python,
                              "--upgrade"])
        if out.returncode != 0:
            return out.returncode
    print("Then run  faf init  in each project to update its hooks (unsent data is carried over).")
    return 0


def delete_after_exit(path):
    """Windows: delete path (and its folder when empty) two seconds after this process ends."""
    subprocess.Popen(f'cmd /d /c ping -n 3 127.0.0.1 >nul & del /f /q "{path}" & rmdir "{path.parent}" 2>nul',
                     creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED, NEW_PROCESS_GROUP


def cmd_uninstall(args):
    targets = [cli_root(), *(bin_dir() / n for n in ("faf", "faf.cmd"))]
    targets = [t for t in targets if t.exists()]
    print("Removes:")
    for t in targets or ["(nothing installed)"]:
        print(f"  {t}")
    print(f"Keeps: {faf_settings.settings_path()} and the hooks in your projects (faf uninit removes those).")
    if not args.yes:
        if not faf_install.interactive():
            raise SystemExit("Not run interactively: pass --yes.")
        if not faf_install.confirm("Uninstall faf?"):
            print("Nothing changed.")
            return 1
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif WINDOWS and t.suffix == ".cmd":
            delete_after_exit(t)  # cmd.exe reads faf.cmd again when this process ends
        else:
            t.unlink()
    remove_from_path()
    print("faf uninstalled.")
    return 0


def cmd_self_install(args):
    self_install(HERE, args.python)
    if args.upgrade:
        return 0
    print("Next:")
    print("  1. faf install       (stores your API key; open a new terminal first if faf is not found)")
    print("  2. cd <project>; faf init")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="faf", description="Fleet Analytics collector")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="set up this machine: API key")
    p.add_argument("--init", action="store_true", help="also run faf init in this repository")
    p.add_argument("--providers", help="with --init: all, claude-code, copilot (all with --yes, else asked)")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.set_defaults(run=cmd_install)

    p = sub.add_parser("init", help="add the hooks to a git repository")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--providers", help="all, claude-code, copilot (asked when omitted)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    p.add_argument("--yes", action="store_true", help="do not ask to confirm")
    p.add_argument("--upload-instructions", choices=("yes", "no"))
    p.add_argument("--python-command", help="interpreter the committed hooks call")
    p.set_defaults(run=cmd_init)

    p = sub.add_parser("uninit", help="remove the hooks from a git repository")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--providers", default="all")
    p.set_defaults(run=cmd_uninit)

    p = sub.add_parser("upgrade", help="install the latest or the given release")
    p.add_argument("version", nargs="?")
    p.add_argument("--check", action="store_true", help="only show whether a newer release exists")
    p.add_argument("--force", action="store_true", help="reinstall the latest release")
    p.add_argument("--python", default=installed_python(), help=argparse.SUPPRESS)
    p.set_defaults(run=cmd_upgrade)

    p = sub.add_parser("uninstall", help="remove the faf command")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(run=cmd_uninstall)

    p = sub.add_parser("version", help="print the version")
    p.set_defaults(run=lambda a: print(faf_settings.COLLECTOR_VERSION) or 0)

    p = sub.add_parser("self-install", help=argparse.SUPPRESS)
    p.add_argument("--python", default=DEFAULT_PYTHON)
    p.add_argument("--upgrade", action="store_true")
    p.set_defaults(run=cmd_self_install)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
