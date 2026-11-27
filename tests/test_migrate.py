"""Migration on install: old hooks and data move over, nothing is lost."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import faf_install  # noqa: E402
import faf_migrate  # noqa: E402
import faf_settings  # noqa: E402
from providers import PROVIDERS, claude_code  # noqa: E402
from support import KEY, read_settings, temp_home, write_settings  # noqa: E402

V0_HOOK = 'python -I "$CLAUDE_PROJECT_DIR/collector/hooks/claude_hook.py"'


def turn(ts, text, uuid):
    return [
        {"type": "user", "uuid": uuid + "u", "timestamp": ts, "sessionId": "s",
         "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": uuid + "a", "timestamp": ts, "sessionId": "s",
         "message": {"id": uuid + "m", "role": "assistant", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self, clear_env=True)
        write_settings(self.home, {"api_key": KEY, "endpoint": "http://127.0.0.1:9/ingest"})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved like the installer's target: the Windows temp path can be an 8.3 short name
        # (RUNNER~1), and the Claude Code project folder below is named after the path.
        self.root = Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        # A faf.v0 collector inside the repo, hooked into .claude/settings.json.
        v0 = self.root / "collector" / "hooks"
        v0.mkdir(parents=True)
        (v0 / "claude_hook.py").write_text("", encoding="utf-8")
        (self.root / "collector" / "faf_collect.py").write_text("", encoding="utf-8")
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": V0_HOOK}]},
                     {"hooks": [{"type": "command", "command": "echo keep-me"}]}]}}), encoding="utf-8")
        local = self.root / ".faf" / "local"
        local.mkdir(parents=True)
        (local / "secrets.json").write_text("{}", encoding="utf-8")
        # The user-level v0 spool, and one never-read Claude Code session of this repo.
        (self.home / ".faf" / "spool.jsonl").write_text(json.dumps({"schema": "faf.v0"}) + "\n", encoding="utf-8")
        project = self.home / ".claude" / "projects" / __import__("re").sub(r"[^A-Za-z0-9]", "-", str(self.root))
        project.mkdir(parents=True)
        lines = turn("2026-11-20T10:00:00Z", "first", "1") + turn("2026-11-20T10:05:00Z", "second", "2")
        (project / "sess-1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    def install(self, *extra):
        with mock.patch("faf_post.flush", return_value=mock.Mock(error="no connection", queued=0)):
            with mock.patch.object(faf_migrate, "flush", return_value=mock.Mock(error="no connection", queued=0)):
                return faf_install.main(["--target", str(self.root), "--providers", "claude-code", "--yes", *extra])

    def test_plan_lists_every_step_and_changes_nothing(self):
        lines = faf_migrate.plan(self.root, [claude_code])
        text = "\n".join(lines)
        for part in ("faf.v0 collector", "secrets.json", "1 sessions never read", "legacy-v0"):
            self.assertIn(part, text)
        self.assertTrue((self.root / "collector").exists())

    def test_install_migrates_hooks_and_data(self):
        self.assertEqual(self.install(), 0)
        settings = (self.root / ".claude" / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("claude_hook.py", settings)
        self.assertIn("keep-me", settings)
        self.assertIn("faf_hook.py", settings)
        self.assertFalse((self.root / "collector").exists())
        self.assertFalse((self.root / ".faf" / "local" / "secrets.json").exists())
        spool = [json.loads(x) for p in (self.root / ".faf" / "local").glob("spool*.jsonl")
                 for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        steps = [e for e in spool if e.get("kind") == "step"]
        self.assertEqual(len(steps), 2)
        self.assertEqual({e["instruction_version"] for e in steps}, {"unknown"})
        self.assertFalse((self.home / ".faf" / "spool.jsonl").exists())
        archived = list((self.home / ".faf" / "legacy-v0").glob("*/spool.jsonl"))
        self.assertEqual(len(archived), 1)

    def test_second_install_records_nothing_twice(self):
        self.install()
        self.assertEqual(faf_migrate.plan(self.root, [claude_code]), [])

    def test_expired_cursor_is_not_backfilled_again(self):
        self.install()
        state = self.root / ".faf" / "local" / "state"
        cursor = state / "claude-code-sess-1.json"
        self.assertTrue(cursor.exists())
        old = 1_000_000_000
        os.utime(cursor, (old, old))
        ctx = faf_migrate.context(self.root, claude_code, "v")
        claude_code._sweep(ctx, exclude=None)  # the hook's sweep drops the expired cursor
        self.assertFalse(cursor.exists())
        self.assertEqual(faf_migrate.plan(self.root, [claude_code]), [])

    def test_reinstall_keeps_the_upload_choice(self):
        self.install("--upload-instructions", "yes")
        with mock.patch.object(faf_install, "interactive", return_value=True), \
                mock.patch("builtins.input", side_effect=EOFError):
            self.install()
        repo = faf_install.git_repo_slug(self.root)
        self.assertIs(read_settings(self.home)["instruction_upload"][repo], True)

    def test_unanswered_upload_prompt_stores_no_choice(self):
        write_settings(self.home, {"api_key": KEY})
        with mock.patch.object(faf_install, "interactive", return_value=True), \
                mock.patch("builtins.input", side_effect=EOFError):
            self.install()
        self.assertNotIn("instruction_upload", read_settings(self.home))

    def test_queued_uploads_follow_the_consent(self):
        local = self.root / ".faf" / "local"
        (local / "spool.jsonl").write_text(json.dumps(
            {"kind": "instruction_version", "files": [{"path": "CLAUDE.md", "sha256": "0" * 64}]}) + "\n",
            encoding="utf-8")
        self.install("--upload-instructions", "no")
        events = [json.loads(x) for p in local.glob("spool*.jsonl")
                  for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertTrue(all(e.get("files") == [] for e in events if e.get("kind") == "instruction_version"))


if __name__ == "__main__":
    unittest.main()
