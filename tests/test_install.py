import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_install  # noqa: E402
import faf_settings  # noqa: E402
from support import KEY, read_settings, temp_home, write_settings  # noqa: E402


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / ".gitignore").write_text("existing-rule\n", encoding="utf-8")
        self.settings = self.repo / ".claude" / "settings.json"
        self.local_settings = self.repo / ".claude" / "settings.local.json"
        self.home = temp_home(self, clear_env=True)
        patcher = mock.patch.object(faf_install, "interactive", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, *extra, yes=True):
        args = ["--target", str(self.repo), "--providers", "claude-code", *extra] + (["--yes"] if yes else [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = faf_install.main(args)
        self.assertEqual(code, 0)
        return out.getvalue()

    def faf_entries(self, event):
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        return [g for g in data["hooks"].get(event, []) if "faf_hook.py" in json.dumps(g)]

    def test_install_creates_layout_and_hooks(self):
        self.install()
        self.assertTrue((self.repo / ".faf" / "collector" / "faf_hook.py").exists())
        self.assertFalse((self.repo / ".faf" / "collector" / "tests").exists())
        self.assertTrue((self.repo / ".faf" / "config.json").exists())
        self.assertNotIn("upload_instruction_content",
                         json.loads((self.repo / ".faf" / "config.json").read_text(encoding="utf-8")))
        self.assertFalse((self.repo / ".faf" / "local" / "secrets.json").exists())
        for event in ("Stop", "SessionEnd"):
            self.assertEqual(len(self.faf_entries(event)), 1)
        start = self.faf_entries("SessionStart")  # collector (async) and the update notice (synchronous)
        self.assertEqual(len(start), 2)
        notice = [h for g in start for h in g["hooks"] if "--notice" in h["command"]]
        self.assertEqual(len(notice), 1)
        self.assertFalse(notice[0].get("async", False))
        entry = self.faf_entries("Stop")[0]["hooks"][0]
        self.assertEqual(entry["command"], faf_install.DEFAULT_PYTHON
                         + ' "$CLAUDE_PROJECT_DIR/.faf/collector/faf_hook.py" --provider claude-code; exit 0')
        self.assertNotIn("args", entry)  # Copilot ignores args and would run a bare interpreter
        self.assertTrue(entry["async"])
        end = self.faf_entries("SessionEnd")[0]["hooks"][0]
        self.assertNotIn("async", end)  # async hooks are killed when Claude Code exits
        self.assertGreater(end["timeout"], 1.5)

    def test_collector_source_repo_runs_the_source_without_a_copy(self):
        source = self.repo / "collector"
        shutil.copytree(COLLECTOR, source, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        with mock.patch.object(faf_install, "HERE", source):
            self.install()
        self.assertFalse((self.repo / ".faf" / "collector").exists())
        self.assertIn('"$CLAUDE_PROJECT_DIR/collector/faf_hook.py"',
                      self.faf_entries("Stop")[0]["hooks"][0]["command"])

    def test_python_command_option_is_used(self):
        self.install("--python-command", "py")
        self.assertTrue(self.faf_entries("Stop")[0]["hooks"][0]["command"].startswith("py "))

    def test_old_per_machine_hooks_are_removed(self):
        self.local_settings.parent.mkdir()
        self.local_settings.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "python",
                                 "args": ["C:/x/.faf/collector/faf_hook.py"]}]},
                     {"hooks": [{"type": "command", "command": "other"}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "python",
                                       "args": ["C:/x/.faf/collector/faf_hook.py"]}]}],
        }}), encoding="utf-8")
        self.install()
        local = json.loads(self.local_settings.read_text(encoding="utf-8"))
        self.assertEqual(local, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}})

    def test_reinstall_does_not_duplicate_and_keeps_user_data(self):
        self.install()
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["WebSearch"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
        }), encoding="utf-8")
        (self.repo / ".faf" / "config.json").write_text('{"extra_instruction_files": ["x"]}', encoding="utf-8")
        self.install()
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], {"allow": ["WebSearch"]})
        self.assertEqual(len(data["hooks"]["Stop"]), 2)  # the user's hook + one faf hook
        self.assertEqual(len(self.faf_entries("Stop")), 1)
        self.assertEqual(json.loads((self.repo / ".faf" / "config.json").read_text(encoding="utf-8")),
                         {"extra_instruction_files": ["x"]})

    def test_gitignore_has_local_entry_once(self):
        self.install()
        self.install()
        lines = (self.repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/.faf/local/"), 1)
        self.assertIn("existing-rule", lines)
        check = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "-q",
                                ".faf/local/secrets.json"])
        self.assertEqual(check.returncode, 0)

    def test_copies_exactly_the_file_list(self):
        self.install()
        dest = self.repo / ".faf" / "collector"
        copied = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        self.assertEqual(copied, sorted(faf_install.COLLECTOR_FILES))

    def test_file_list_covers_every_collector_module(self):
        modules = {p.relative_to(COLLECTOR).as_posix() for p in COLLECTOR.rglob("*.py")
                   if "tests" not in p.relative_to(COLLECTOR).parts}
        # faf_cli.py is the machine-wide command; projects only get the hooks.
        self.assertEqual(modules - set(faf_install.COLLECTOR_FILES), {"faf_cli.py"})
        for rel in faf_install.COLLECTOR_FILES:
            self.assertTrue((COLLECTOR / rel).is_file(), rel)
        self.assertIn("LICENSE", faf_install.COLLECTOR_FILES)

    def test_dry_run_shows_plan_and_changes_nothing(self):
        (self.repo / "CLAUDE.md").write_text("x\n", encoding="utf-8")
        before = sorted(p.name for p in self.repo.iterdir())
        out = self.install("--dry-run", yes=False)
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), before)
        self.assertFalse((self.home / ".faf").exists())
        for expected in ("Files:", ".claude/settings.json", "Hooks:", "SessionStart, Stop, SessionEnd",
                         "Sends to: faf-func-34wezaqh.azurewebsites.net", "Data sent:", "  CLAUDE.md",
                         "Dry run: nothing changed."):
            self.assertIn(expected, out)

    def test_not_interactive_needs_yes(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            faf_install.main(["--target", str(self.repo), "--providers", "claude-code"])
        self.assertFalse((self.repo / ".faf").exists())

    def test_upload_stays_off_when_not_interactive(self):
        out = self.install()
        self.assertFalse((self.home / ".faf" / "settings.json").exists())
        self.assertIn("instruction upload off", out)
        self.assertIn(faf_settings.INVITE_URL, out)  # no key: where to get one

    def test_upload_flag_records_consent_per_repo(self):
        write_settings(self.home, {"api_key": KEY})
        out = self.install("--upload-instructions", "yes")
        self.assertEqual(read_settings(self.home), {"api_key": KEY, "instruction_upload": {"repo": True}})
        self.assertIn("instruction upload on", out)
        self.assertNotIn(faf_settings.INVITE_URL, out)

    def test_install_log_lines(self):
        write_settings(self.home, {"api_key": KEY})
        self.install()
        log = (self.repo / ".faf" / "local" / "collector.log").read_text(encoding="utf-8")
        for expected in (f"[install] installing v{faf_settings.COLLECTOR_VERSION}", "[install] .faf/collector/ copied",
                         "[install] .faf/config.json created", "[install] .gitignore changed",
                         "[install] Claude Code: registered SessionStart, Stop, SessionEnd hooks in .claude/settings.json",
                         "[install] sends to faf-func-34wezaqh.azurewebsites.net, instruction upload off",
                         "[install] installed"):
            self.assertIn(expected, log)
        self.assertNotIn(KEY, log)
        self.assertNotIn(str(self.repo), log)

    def test_uninstall_removes_only_faf_entries(self):
        self.install()
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["WebSearch"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
        }), encoding="utf-8")
        self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            faf_install.main(["--target", str(self.repo), "--providers", "claude-code", "--uninstall"])
        self.assertFalse((self.repo / ".faf" / "collector" / ".git").exists())
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], {"allow": ["WebSearch"]})
        self.assertEqual(data["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]})

    def test_invalid_settings_file_is_not_overwritten(self):
        self.settings.parent.mkdir()
        self.settings.write_text("{ broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ broken")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                faf_install.main(["--target", str(self.repo), "--providers", "nope"])

    # ------------------------------------------------------------ copilot

    def copilot_hooks(self):
        return json.loads((self.repo / ".github" / "hooks" / "faf.json").read_text(encoding="utf-8"))

    def test_copilot_hook_file_uses_the_right_python_per_platform(self):
        out = self.install_providers("copilot")
        data = self.copilot_hooks()
        self.assertEqual(data["version"], 1)
        self.assertEqual(sorted(data["hooks"]), ["agentStop", "sessionEnd", "sessionStart"])
        stop = data["hooks"]["agentStop"][0]
        self.assertEqual(stop["bash"],
                         "python3 .faf/collector/faf_hook.py --provider copilot --event agentStop")
        self.assertEqual(stop["powershell"],
                         "python .faf/collector/faf_hook.py --provider copilot --event agentStop")
        self.assertEqual(stop["cwd"], ".")
        self.assertFalse(self.settings.exists())  # Claude Code settings untouched
        self.assertIn(".github/hooks/faf.json", out)

    def test_all_installs_both_and_python_option_applies_to_both_shells(self):
        self.install_providers("all", "--python-command", "py")
        stop = self.copilot_hooks()["hooks"]["agentStop"][0]
        self.assertTrue(stop["bash"].startswith("py ") and stop["powershell"].startswith("py "))
        self.assertEqual(len(self.faf_entries("Stop")), 1)

    def test_copilot_reinstall_and_uninstall(self):
        self.install_providers("copilot")
        first = self.copilot_hooks()
        self.install_providers("copilot")
        self.assertEqual(self.copilot_hooks(), first)
        self.install_providers("copilot", "--uninstall")
        self.assertFalse((self.repo / ".github" / "hooks").exists())

    def install_providers(self, providers, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = faf_install.main(["--target", str(self.repo), "--providers", providers, "--yes", *extra])
        self.assertEqual(code, 0)
        return out.getvalue()


if __name__ == "__main__":
    unittest.main()
