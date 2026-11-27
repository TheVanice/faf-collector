import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))

from providers import copilot  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "copilot_events.jsonl"
SESSION = "cccccccc-1111-2222-3333-444444444444"


class WorktreeTests(unittest.TestCase):
    WORKTREES = {"c:/dev/f123-add-search": "F123-Add-search"}

    @staticmethod
    def turn(n, *tool_args):
        yield (n * 10, {"type": "user.message", "id": f"u{n}", "timestamp": f"2026-11-22T10:0{n}:00Z", "data": {}})
        for i, args in enumerate(tool_args):
            yield (n * 10 + i + 1, {"type": "tool.execution_start", "data": {"toolCallId": f"t{n}{i}", "arguments": args}})

    def build(self, meta, worktrees=None):
        lines = [*self.turn(1, {"command": "git worktree add -b F123-Add-search C:\\dev\\F123-Add-search HEAD"}),
                 *self.turn(2),
                 *self.turn(3, {"path": "C:\\dev\\Shop\\a.cs"})]
        return copilot._build_turns(lines, meta, self.WORKTREES if worktrees is None else worktrees)

    def test_worktree_the_agent_works_in_is_found_and_sticks(self):
        meta = {"branch": "master"}
        self.assertEqual([t["worktree"] for t in self.build(meta)], ["F123-Add-search"] * 3)
        self.assertEqual(meta["worktree"], "F123-Add-search")

    def test_without_worktrees_there_is_none(self):
        turns = self.build({"branch": "master"}, worktrees={})
        self.assertEqual([(t["branch"], t["worktree"]) for t in turns], [("master", None)] * 3)

    def test_similar_folder_name_is_not_a_match(self):
        self.assertIsNone(copilot._worktree_in({"path": "C:\\dev\\F123-Add-search-2\\x"}, self.WORKTREES))
        self.assertEqual(copilot._worktree_in({"path": "C:\\dev\\F123-Add-search\\x"}, self.WORKTREES),
                         "F123-Add-search")


class CopilotTaskNameTests(unittest.TestCase):
    def test_session_name_scalar_is_unquoted(self):
        self.assertEqual(copilot._scalar('"Task: BFF-Spec\r\n\r\nrest"'), "Task: BFF-Spec\r\n\r\nrest")
        self.assertEqual(copilot._scalar("plain"), "plain")


class CopilotCollectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        self.session_dir = base / ".copilot" / "session-state" / SESSION
        self.session_dir.mkdir(parents=True)
        self.log = self.session_dir / "events.jsonl"
        shutil.copy(FIXTURE, self.log)
        (self.session_dir / "workspace.yaml").write_text(
            f"id: {SESSION}\ncwd: /repo\nclient_name: vscode-agent-host\nlast_client_name: vscode-agent-host\n",
            encoding="utf-8")
        self.ctx = SimpleNamespace(root=base, repo="owner/name", state_dir=self.state_dir,
                                   instruction_version="abc123def456", default_branch="main")
        self.hook = copilot.normalize({"sessionId": SESSION, "cwd": "/repo", "timestamp": 1,
                                       "transcriptPath": str(self.log), "stopReason": "end_turn"},
                                      "agentStop")

    def collect(self, hook=None):
        return copilot.collect(hook or self.hook, self.ctx)

    def append(self, *lines):
        with open(self.log, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def test_first_turn_totals(self):
        first = self.collect()[0]
        self.assertEqual(first["provider"], "copilot")
        self.assertEqual(first["session_id"], SESSION)
        self.assertEqual(first["ts_start"], "2026-11-22T10:00:00Z")
        self.assertEqual(first["ts_end"], "2026-11-22T10:00:30Z")
        self.assertEqual(first["llm_calls"], 3)                 # 2 main + 1 subagent call
        self.assertEqual(first["tokens_in"], 100 + 5000 + 500)  # input minus cache read and write
        self.assertEqual(first["tokens_out"], 50 + 900 + 100)
        self.assertEqual(first["tokens_cache_read"], 2300)
        self.assertEqual(first["tokens_cache_write"], 100)
        self.assertNotIn("tokens_cache_write_1h", first)       # Copilot has no 1-hour tier
        self.assertEqual(first["model"], "gpt-x")               # not the subagent's Haiku
        usage = {u["model"]: (u["out"], u["calls"]) for u in first["usage"]}
        self.assertEqual(sum(c for _, c in usage.values()), 3)   # the subagent's calls are its own entry
        self.assertEqual(sum(o for o, _ in usage.values()), first["tokens_out"])
        self.assertEqual(len(usage), 2)
        self.assertEqual(first["tool_calls"], 2)
        self.assertEqual(first["tool_errors"], 1)
        self.assertEqual(first["tool_denials"], 1)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["trigger"], "human")
        self.assertEqual(first["effort"], "high")
        self.assertEqual(first["permission_mode"], "interactive")
        self.assertEqual(first["client_version"], "1.0.108-0")
        self.assertEqual(first["entrypoint"], "vscode-agent-host")
        self.assertEqual(first["boundary_ref"], "feature/y")
        self.assertEqual(first["instruction_version"], "abc123def456")

    def test_task_from_session_name_replaces_trunk_fallback_and_fixes_earlier_turns(self):
        self.ctx.default_branch = "feature/y"  # the fixture session runs on the trunk
        before = self.collect()
        self.assertTrue(all(e["boundary_ref"].startswith("feature/y@") for e in before))
        (self.session_dir / "workspace.yaml").write_text(
            f'id: {SESSION}\nname: "TaskId: BFF-Spec\\r\\n\\r\\nWrite it"\n', encoding="utf-8")
        after = self.collect()
        self.assertEqual([e["boundary_ref"] for e in after], ["BFF-Spec"] * len(before))
        self.assertEqual([e["event_id"] for e in after], [e["event_id"] for e in before])  # upsert, not new rows

    def test_steering_and_subagent_messages_do_not_start_turns(self):
        events = self.collect()
        self.assertEqual([e["ts_start"] for e in events],
                         ["2026-11-22T10:00:00Z", "2026-11-22T11:00:00Z"])  # 3rd turn has no model call

    def test_autopilot_turn_is_system_and_abort_is_interrupted(self):
        second = self.collect()[1]
        self.assertEqual(second["trigger"], "system")
        self.assertEqual(second["status"], "interrupted")
        self.assertEqual(second["permission_mode"], "autopilot")
        self.assertEqual(second["effort"], "medium")  # from session.start, no per-call effort

    def test_last_turn_is_collected_later_with_session_context(self):
        self.collect()
        self.assertEqual(self.collect(), [])  # last turn still has no model call
        self.append({"type": "session.usage_record", "id": "e21", "timestamp": "2026-11-22T12:00:03.000Z",
                     "data": {"usage": {"model": "gpt-x", "inputTokens": 10, "outputTokens": 2}}})
        third = self.collect()
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["ts_start"], "2026-11-22T12:00:00Z")
        self.assertEqual(third[0]["boundary_ref"], "feature/y")       # kept from session.start
        self.assertEqual(third[0]["client_version"], "1.0.108-0")
        again = self.collect()
        self.assertEqual([e["event_id"] for e in again], [third[0]["event_id"]])  # re-sent, same id

    def test_compaction_tokens_count_but_do_not_pick_the_model(self):
        self.append({"type": "session.compaction_complete", "id": "e21", "timestamp": "2026-11-22T12:00:02.000Z",
                     "data": {"success": True, "compactionTokensUsed": {
                         "model": "other-model", "inputTokens": 500, "outputTokens": 400,
                         "cacheReadTokens": 300, "cacheWriteTokens": 0}}},
                    {"type": "session.usage_record", "id": "e22", "timestamp": "2026-11-22T12:00:03.000Z",
                     "data": {"usage": {"model": "gpt-x", "inputTokens": 10, "outputTokens": 2}}})
        third = self.collect()[2]
        self.assertEqual(third["llm_calls"], 2)
        self.assertEqual(third["tokens_in"], 210)
        self.assertEqual(third["tokens_cache_read"], 300)
        self.assertEqual(third["model"], "gpt-x")

    def test_event_ids_are_stable_and_provider_specific(self):
        first = self.collect()
        for path in self.state_dir.iterdir():
            path.unlink()
        self.assertEqual([e["event_id"] for e in self.collect()], [e["event_id"] for e in first])

    def test_version_is_pinned_at_session_start(self):
        start = copilot.normalize({"sessionId": SESSION, "cwd": "/repo", "source": "new"}, "sessionStart")
        self.ctx.started_at = "2026-11-22T10:30:00Z"
        self.ctx.instruction_version = "new000000000"
        with mock.patch.dict(os.environ, {"COPILOT_HOME": str(self.session_dir.parent.parent)}):
            events = self.collect(start)
        self.assertEqual(events[0]["instruction_version"], "new000000000")  # first pin covers the past

    def test_session_start_before_the_log_exists_keeps_the_pin(self):
        self.log.unlink()
        start = copilot.normalize({"sessionId": SESSION, "cwd": "/repo"}, "sessionStart")
        self.ctx.started_at = "2026-11-22T09:00:00Z"
        with mock.patch.dict(os.environ, {"COPILOT_HOME": str(self.session_dir.parent.parent)}):
            self.assertEqual(self.collect(start), [])
        shutil.copy(FIXTURE, self.log)
        self.ctx.instruction_version = "later0000000"
        events = self.collect()
        self.assertEqual(events[0]["instruction_version"], "abc123def456")

    def test_other_sessions_that_grew_are_swept(self):
        self.collect()
        other = copilot.normalize({"sessionId": "other", "cwd": "/repo"}, "agentStop")
        self.append({"type": "user.message", "id": "e30", "timestamp": "2026-11-22T13:00:00.000Z",
                     "data": {"delivery": "idle"}},
                    {"type": "session.usage_record", "id": "e31", "timestamp": "2026-11-22T13:00:01.000Z",
                     "data": {"usage": {"model": "gpt-x", "inputTokens": 1, "outputTokens": 1}}})
        swept = copilot.collect(other, self.ctx)
        self.assertEqual([e["ts_start"] for e in swept], ["2026-11-22T13:00:00Z"])


class CopilotHookInputTests(unittest.TestCase):
    def test_normalize_maps_camel_case_and_event(self):
        hook = copilot.normalize({"sessionId": "s", "transcriptPath": "/t/s/events.jsonl", "cwd": "/r"},
                                 "agentStop")
        self.assertEqual(hook["session_id"], "s")
        self.assertEqual(hook["transcript_path"], "/t/s/events.jsonl")
        self.assertEqual(hook["hook_event_name"], "Stop")
        self.assertEqual(copilot.normalize({}, "sessionStart")["hook_event_name"], "SessionStart")
        self.assertEqual(copilot.normalize({}, "sessionEnd")["hook_event_name"], "SessionEnd")

    def test_subagent_stop_and_cloud_are_skipped(self):
        self.assertIsNone(copilot.skip_reason(copilot.normalize({"sessionId": "s"}, "agentStop")))
        self.assertTrue(copilot.skip_reason(copilot.normalize({"sessionId": "s", "agentId": "a"}, "agentStop")))
        with mock.patch.dict(os.environ, {"COPILOT_AGENT_PROMPT": "do it"}):
            self.assertTrue(copilot.skip_reason(copilot.normalize({"sessionId": "s"}, "agentStop")))

    def test_instruction_files_selection(self):
        paths = [".github/copilot-instructions.md", ".github/instructions/py.instructions.md",
                 ".github/instructions/notes.md", "AGENTS.md", "src/AGENTS.md", "CLAUDE.md",
                 "src/CLAUDE.md", ".github/agents/review.agent.md", ".github/prompts/fix.prompt.md",
                 ".github/skills/x/SKILL.md", ".claude/skills/y/SKILL.md", "README.md", ".github/hooks/faf.json"]
        self.assertEqual(copilot.instruction_files(paths), sorted([
            ".github/copilot-instructions.md", ".github/instructions/py.instructions.md", "AGENTS.md",
            "src/AGENTS.md", "CLAUDE.md", ".github/agents/review.agent.md", ".github/prompts/fix.prompt.md",
            ".github/skills/x/SKILL.md", ".claude/skills/y/SKILL.md"]))


if __name__ == "__main__":
    unittest.main()
