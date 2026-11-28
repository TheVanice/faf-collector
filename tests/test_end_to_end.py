"""Runs the real hook against a local HTTP server standing in for the ingest endpoint."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_hook  # noqa: E402
import faf_settings  # noqa: E402
from support import KEY, temp_home, write_settings  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_transcript.jsonl"
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class Ingest(BaseHTTPRequestHandler):
    received = []
    headers_seen = []
    reject_kind: Optional[str] = None  # events of this kind are answered as rejected

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Ingest.headers_seen.append(dict(self.headers))
        Ingest.received.extend(body)
        rejected = [{"index": i, "error": "test rejection"} for i, e in enumerate(body)
                    if e.get("kind") == Ingest.reject_kind]
        payload = json.dumps({"accepted": len(body) - len(rejected), "rejected": rejected}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


class HookEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Ingest)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "CLAUDE.md").write_text("rule one\n", encoding="utf-8")
        local = self.repo / ".faf" / "local"
        local.mkdir(parents=True)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/ingest"
        transcript_dir = Path(temp.name) / "home" / ".claude" / "projects" / "proj"
        transcript_dir.mkdir(parents=True)
        self.transcript = transcript_dir / f"{SESSION}.jsonl"
        shutil.copy(FIXTURE, self.transcript)
        self.temp = Path(temp.name)
        Ingest.received = []
        Ingest.headers_seen = []
        Ingest.reject_kind = None
        env = mock.patch.dict(os.environ)  # snapshot, restored after the test
        env.start()
        self.addCleanup(env.stop)
        for name in [k for k in os.environ if k.startswith("FAF_") or k == "CLAUDE_PROJECT_DIR"]:
            del os.environ[name]
        self.home = temp_home(self)
        self.configure()

    def configure(self, **extra):
        write_settings(self.home, {"api_key": KEY, "endpoint": self.url, **extra})

    def consent(self):
        self.configure(instruction_upload={"repo": True})  # no origin: the slug is the folder name

    def run_hook(self, cwd=None, event="Stop"):
        hook_input = json.dumps({"session_id": SESSION, "cwd": str(cwd or self.repo),
                                 "hook_event_name": event, "transcript_path": str(self.transcript)})
        out = io.StringIO()
        with redirect_stdout(out):
            code = faf_hook.main(["--provider", "claude-code"], stdin=io.StringIO(hook_input))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")  # the hook must never print
        return list(Ingest.received)

    def log_text(self):
        path = self.repo / ".faf" / "local" / "collector.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def other_repo(self, name="other"):
        other = self.temp / name
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        return other

    def test_first_run_sends_version_and_turns_then_nothing_twice(self):
        sent = self.run_hook()
        kinds = [e["kind"] for e in sent]
        self.assertEqual(kinds.count("instruction_version"), 1)
        self.assertEqual(kinds.count("step"), 2)
        version = next(e for e in sent if e["kind"] == "instruction_version")
        self.assertIsNone(version["previous_version"])
        self.assertEqual(version["files"], [])  # upload is off until the user consents
        self.assertEqual(version["changes"], [{"kind": "memory", "status": "added", "lines_added": 1,
                                               "lines_removed": 0, "size": 9}])
        self.assertFalse((self.repo / ".faf" / "local" / "spool.jsonl").exists())
        headers = {k.lower(): v for k, v in Ingest.headers_seen[0].items()}
        self.assertEqual(headers["x-api-key"], KEY)
        self.assertEqual(headers["x-faf-collector"], faf_settings.COLLECTOR_VERSION)
        self.assertEqual(headers["user-agent"], f"faf-collector/{faf_settings.COLLECTOR_VERSION}")

        Ingest.received = []
        again = self.run_hook()
        self.assertEqual([e["kind"] for e in again].count("instruction_version"), 0)

    def test_changed_instructions_register_new_version_with_predecessor(self):
        first = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        (self.repo / "CLAUDE.md").write_text("rule one, changed\n", encoding="utf-8")
        Ingest.received = []
        second = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(second["previous_version"], first["version"])
        self.assertTrue(second["dirty"])  # the repo has no commits, so CLAUDE.md is untracked
        self.assertEqual(second["changes"], [{"kind": "memory", "status": "changed", "lines_added": 1,
                                              "lines_removed": 1, "size": 18}])

    def test_consent_uploads_content(self):
        self.consent()
        version = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        self.assertEqual(version["files"], [{"path": "CLAUDE.md", "sha256": version["files"][0]["sha256"],
                                             "content": "rule one\n"}])
        self.assertIn("uploaded instructions (1 files)", self.log_text())

    def test_repo_config_can_disable_but_not_enable(self):
        (self.repo / ".faf" / "config.json").write_text('{"upload_instruction_content": true}', encoding="utf-8")
        version = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        self.assertEqual(version["files"], [])
        self.consent()
        (self.repo / "CLAUDE.md").write_text("rule two\n", encoding="utf-8")
        (self.repo / ".faf" / "config.json").write_text('{"upload_instruction_content": false}', encoding="utf-8")
        Ingest.received = []
        version = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        self.assertEqual(version["files"], [])

    def test_opting_in_later_resends_the_current_version_once(self):
        first = next(e for e in self.run_hook() if e["kind"] == "instruction_version")
        self.consent()
        Ingest.received = []
        again = [e for e in self.run_hook() if e["kind"] == "instruction_version"]
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["version"], first["version"])
        self.assertEqual(again[0]["files"][0]["content"], "rule one\n")
        Ingest.received = []
        self.assertEqual([e for e in self.run_hook() if e["kind"] == "instruction_version"], [])

    def test_step_events_carry_the_current_version(self):
        sent = self.run_hook()
        version = next(e for e in sent if e["kind"] == "instruction_version")["version"]
        for e in (e for e in sent if e["kind"] == "step"):
            self.assertEqual(e["instruction_version"], version)

    def test_unconfigured_repo_does_nothing(self):
        write_settings(self.home, {})
        self.assertEqual(self.run_hook(), [])
        self.assertFalse((self.repo / ".faf" / "local" / "state").exists())


    def test_activity_log_tells_what_happened(self):
        self.run_hook(event="SessionStart")
        text = self.log_text()
        for expected in (f"[SessionStart] v{faf_settings.COLLECTOR_VERSION} active, instruction upload off",
                         "[SessionStart] Instruction upload is off for repo.",
                         "[SessionStart] new instruction version queued (1 changed files)",
                         "[SessionStart] recorded 2 turns", "[SessionStart] sent 3 events to 127.0.0.1:"):
            self.assertIn(expected, text)

    def test_activity_log_holds_no_data(self):
        self.consent()
        self.run_hook(event="SessionStart")
        Ingest.reject_kind = "step"
        self.run_hook()
        text = self.log_text()
        self.assertTrue(text)
        sent = " ".join(json.dumps(e) for e in Ingest.received)
        for secret in (KEY, "repo", "feature/x", SESSION, SESSION[:8], "claude-opus", "claude-sonnet",
                       "CLAUDE.md", "rule one", "test rejection", str(self.repo), "tokens"):
            self.assertNotIn(secret, text.replace("rejected.jsonl", ""), secret)
        self.assertIn("feature/x", sent)  # the data itself was sent

    def test_activity_log_says_why_nothing_is_recorded(self):
        write_settings(self.home, {})
        self.run_hook()
        self.assertIn("not recording: no API key", self.log_text())

    def test_activity_log_rotates(self):
        log = self.repo / ".faf" / "local" / "collector.log"
        log.write_text("x" * (faf_hook.LOG_MAX_BYTES + 1), encoding="utf-8")
        self.run_hook()
        self.assertTrue((self.repo / ".faf" / "local" / "collector.log.1").exists())
        self.assertLess(log.stat().st_size, 10_000)

    def test_rejected_events_are_kept_and_logged(self):
        Ingest.reject_kind = "instruction_version"
        self.run_hook()
        rejected = (self.repo / ".faf" / "local" / "rejected.jsonl").read_text(encoding="utf-8")
        self.assertIn("test rejection", rejected)
        self.assertIn("1 events rejected, kept in .faf/local/rejected.jsonl", self.log_text())

    def test_unreachable_ingest_is_logged_and_events_stay(self):
        write_settings(self.home, {"api_key": KEY, "endpoint": "http://127.0.0.1:9/api/ingest"})
        self.assertEqual(self.run_hook(), [])
        self.assertRegex(self.log_text(), r"send failed \((no connection|timeout)\), 3 events queued")
        self.assertTrue(list((self.repo / ".faf" / "local").glob("spool.*.sending.jsonl")))

    def test_project_dir_wins_over_cwd(self):
        other = self.other_repo()
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.assertTrue(self.run_hook(cwd=other))
        self.assertFalse((other / ".faf").exists())

    def test_repo_without_collector_gets_no_files(self):
        other = self.other_repo()
        self.assertEqual(self.run_hook(cwd=other), [])
        self.assertEqual(sorted(p.name for p in other.iterdir()), [".git"])

    def test_stale_lock_is_broken(self):
        lock = self.repo / ".faf" / "local" / "hook.lock"
        lock.write_text("1", encoding="utf-8")
        old = time.time() - faf_hook.LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        self.assertTrue(self.run_hook())
        self.assertFalse(lock.exists())

    def test_held_lock_skips_the_run_and_logs_it(self):
        (self.repo / ".faf" / "local" / "hook.lock").write_text("1", encoding="utf-8")
        with mock.patch.object(faf_hook, "LOCK_WAIT_SECONDS", 0.2):
            self.assertEqual(self.run_hook(), [])
        self.assertIn("skipped: another hook run is active", self.log_text())

    def run_copilot_hook(self, event="agentStop", **extra):
        session = "cccccccc-1111-2222-3333-444444444444"
        log = self.temp / ".copilot" / "session-state" / session / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE.parent / "copilot_events.jsonl", log)
        payload = {"sessionId": session, "cwd": str(self.repo), "timestamp": 1764000000000,
                   "transcriptPath": str(log), "stopReason": "end_turn", **extra}
        out = io.StringIO()
        with redirect_stdout(out):
            code = faf_hook.main(["--provider", "copilot", "--event", event],
                                 stdin=io.StringIO(json.dumps(payload)))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")  # empty stdout = no decision, never blocks the agent
        return list(Ingest.received)

    def test_copilot_turns_are_sent(self):
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.other_repo())  # inherited, must not win
        received = self.run_copilot_hook()
        steps = [e for e in received if e["kind"] == "step"]
        self.assertEqual({e["provider"] for e in steps}, {"copilot"})
        self.assertEqual(len(steps), 2)
        versions = [e for e in received if e["kind"] == "instruction_version"]
        self.assertEqual([v["provider"] for v in versions], ["copilot"])
        self.assertIn("[Stop] sent", self.log_text())

    def test_copilot_subagent_stop_is_ignored(self):
        self.assertEqual(self.run_copilot_hook(agentId="sub1"), [])
        self.assertIn("ignored: subagent stop", self.log_text())

    def test_claude_hook_run_by_copilot_is_ignored(self):
        # Copilot also runs .claude/settings.json hooks, with its own payload.
        hook_input = json.dumps({"hook_event_name": "Stop", "session_id": "x", "cwd": str(self.repo),
                                 "transcript_path": str(self.temp / ".copilot" / "session-state" / "x"
                                                        / "events.jsonl")})
        with redirect_stdout(io.StringIO()):
            faf_hook.main(["--provider", "claude-code"], stdin=io.StringIO(hook_input))
        self.assertEqual(Ingest.received, [])
        self.assertIn("ignored: not a Claude Code hook call", self.log_text())

    def test_stdin_is_decoded_as_utf8(self):
        repo = self.temp / "repö"
        shutil.copytree(self.repo, repo)
        hook_input = json.dumps({"session_id": SESSION, "cwd": str(repo),
                                 "transcript_path": str(self.transcript)}, ensure_ascii=False)
        fake_stdin = mock.Mock(buffer=io.BytesIO(hook_input.encode("utf-8")))
        with mock.patch.object(sys, "stdin", fake_stdin), redirect_stdout(io.StringIO()):
            faf_hook.main(["--provider", "claude-code"])
        self.assertTrue(Ingest.received)


class HookRobustnessTests(unittest.TestCase):
    def setUp(self):
        # The hook logs errors into the repo found from the working directory,
        # so run these tests from a throwaway folder, never from the real repo.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous = os.getcwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous)

    def test_crash_is_logged_without_message(self):
        repo = Path(os.getcwd())
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".faf" / "local").mkdir(parents=True)
        home = temp_home(self, clear_env=True)
        write_settings(home, {"api_key": KEY, "endpoint": "http://127.0.0.1:9/x"})
        hook_input = json.dumps({"session_id": SESSION, "cwd": str(repo), "hook_event_name": "Stop",
                                 "transcript_path": str(repo / "t.jsonl")})
        with mock.patch.object(faf_hook, "instruction_snapshot", side_effect=RuntimeError("leak /x/y")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(faf_hook.main(["--provider", "claude-code"], stdin=io.StringIO(hook_input)), 0)
        text = (repo / ".faf" / "local" / "collector.log").read_text(encoding="utf-8")
        self.assertRegex(text, r"\[Stop\] error RuntimeError at test_end_to_end\.py:\d+|"
                               r"\[Stop\] error RuntimeError at mock\.py:\d+")
        self.assertNotIn("leak", text)

    def test_bad_stdin_still_exits_zero_and_prints_nothing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = faf_hook.main(["--provider", "claude-code"], stdin=io.StringIO("{not json"))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_empty_stdin_does_nothing(self):
        # The second interpreter of the hook command finds the hook JSON already read.
        for extra in ([], ["--notice"]):
            with mock.patch.object(faf_hook, "run") as run, mock.patch.object(faf_hook, "notice") as notice, \
                    redirect_stdout(io.StringIO()) as out:
                code = faf_hook.main(["--provider", "claude-code", *extra], stdin=io.StringIO(""))
            self.assertEqual((code, out.getvalue()), (0, ""))
            run.assert_not_called()
            notice.assert_not_called()

    def test_unknown_provider_exits_zero(self):
        with redirect_stdout(io.StringIO()) as out:
            code = faf_hook.main(["--provider", "nope"], stdin=io.StringIO("{}"))
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
