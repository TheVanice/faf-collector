import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))

from providers import claude_code  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_transcript.jsonl"
SESSION = "11111111-2222-3333-4444-555555555555"


class ClaudeCodeCollectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        self.transcript = base / ".claude" / "projects" / "proj" / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        shutil.copy(FIXTURE, self.transcript)
        self.ctx = SimpleNamespace(root=base, repo="owner/name", state_dir=self.state_dir,
                                   instruction_version="abc123def456")
        self.hook = {"session_id": SESSION, "transcript_path": str(self.transcript)}

    def by_branch_turn(self, events, ts_start):
        return next(e for e in events if e["ts_start"] == ts_start)

    def test_turn_totals(self):
        events = claude_code.collect(self.hook, self.ctx)
        self.assertEqual(len(events), 2)
        first = self.by_branch_turn(events, "2026-11-21T10:00:00Z")
        self.assertEqual(first["provider"], "claude-code")
        self.assertEqual(first["repo"], "owner/name")
        self.assertEqual(first["boundary_ref"], "feature/x")
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertEqual(first["llm_calls"], 2)          # m1 and m2, m2 counted once
        self.assertEqual(first["tokens_in"], 200)
        self.assertEqual(first["tokens_out"], 80)
        self.assertEqual(first["tokens_cache_read"], 1000)
        self.assertEqual(first["tokens_cache_write"], 200)
        self.assertEqual(first["tool_calls"], 2)
        self.assertEqual(first["tool_errors"], 1)
        self.assertEqual(first["status"], "interrupted")
        self.assertEqual(first["ts_end"], "2026-11-21T10:00:30Z")
        self.assertEqual(first["instruction_version"], "abc123def456")

    def test_task_from_session_title_fixes_earlier_turns(self):
        self.ctx.default_branch = "feature/x"  # the fixture session runs on the trunk
        before = claude_code.collect(self.hook, self.ctx)
        self.assertTrue(all(e["boundary_ref"].startswith("feature/x@") for e in before))
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Task: BFF-Spec", "sessionId": SESSION}) + "\n")
        after = claude_code.collect(self.hook, self.ctx)
        self.assertEqual(sorted(e["boundary_ref"] for e in after), ["BFF-Spec"] * len(before))
        self.assertEqual(sorted(e["event_id"] for e in after), sorted(e["event_id"] for e in before))

    def test_rename_wins_over_later_auto_titles(self):
        self.ctx.default_branch = "feature/x"

        def title(kind, key, text):
            with open(self.transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": kind, key: text, "sessionId": SESSION}) + "\n")
            return {e["boundary_ref"] for e in claude_code.collect(self.hook, self.ctx)}

        self.assertEqual(title("ai-title", "aiTitle", "Auto name"), {"Auto name"})
        self.assertEqual(title("custom-title", "customTitle", "My name"), {"My name"})
        self.assertEqual(title("ai-title", "aiTitle", "Auto name"), {"My name"})

    def test_synthetic_model_is_ignored_and_second_turn_is_ok(self):
        events = claude_code.collect(self.hook, self.ctx)
        second = self.by_branch_turn(events, "2026-11-21T11:00:00Z")
        self.assertEqual(second["model"], "claude-opus-5-5")
        self.assertEqual(second["llm_calls"], 1)
        self.assertEqual(second["tokens_out"], 500)
        self.assertEqual(second["status"], "ok")

    def test_second_call_resends_only_last_turn_with_same_id(self):
        first_run = claude_code.collect(self.hook, self.ctx)
        last_id = self.by_branch_turn(first_run, "2026-11-21T11:00:00Z")["event_id"]
        second_run = claude_code.collect(self.hook, self.ctx)
        self.assertEqual([e["event_id"] for e in second_run], [last_id])

    def test_new_turn_after_last_call_is_collected_with_previous_turn_resent(self):
        claude_code.collect(self.hook, self.ctx)
        extra = ('{"type":"user","promptId":"p3","uuid":"u9","timestamp":"2026-11-21T12:00:00.000Z",'
                 '"message":{"role":"user","content":"x"}}\n'
                 '{"type":"assistant","uuid":"a9","timestamp":"2026-11-21T12:00:05.000Z",'
                 '"message":{"id":"m9","model":"claude-opus-5-5","role":"assistant","content":[],'
                 '"usage":{"input_tokens":1,"output_tokens":2}}}\n')
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(extra)
        events = claude_code.collect(self.hook, self.ctx)
        self.assertEqual([e["ts_start"] for e in events],
                         ["2026-11-21T11:00:00Z", "2026-11-21T12:00:00Z"])

    def test_turn_in_progress_is_completed_on_next_call(self):
        lines = self.transcript.read_text(encoding="utf-8").splitlines(keepends=True)
        # Cut right after turn 2's prompt: turn 2 has no model call yet, so it is not emitted.
        self.transcript.write_text("".join(lines[:9]), encoding="utf-8")
        partial = claude_code.collect(self.hook, self.ctx)
        self.assertEqual([e["ts_start"] for e in partial], ["2026-11-21T10:00:00Z"])
        self.transcript.write_text("".join(lines), encoding="utf-8")
        full = claude_code.collect(self.hook, self.ctx)
        self.assertEqual([e["ts_start"] for e in full], ["2026-11-21T11:00:00Z"])
        self.assertEqual(full[-1]["tokens_out"], 500)

    def test_subagent_usage_is_added_to_its_turn(self):
        sub_dir = self.transcript.parent / SESSION / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-x.jsonl").write_text(
            '{"type":"assistant","timestamp":"2026-11-21T11:00:15.000Z",'
            '"message":{"id":"sa1","model":"claude-haiku-5-5","role":"assistant",'
            '"content":[{"type":"tool_use","id":"st1","name":"Grep","input":{}}],'
            '"usage":{"input_tokens":5,"output_tokens":7}}}\n',
            encoding="utf-8")
        events = claude_code.collect(self.hook, self.ctx)
        second = self.by_branch_turn(events, "2026-11-21T11:00:00Z")
        self.assertEqual(second["llm_calls"], 2)
        self.assertEqual(second["tokens_out"], 507)
        self.assertEqual(second["tool_calls"], 1)

    def test_model_is_the_main_conversation_model_even_when_subagents_write_more(self):
        sub_dir = self.transcript.parent / SESSION / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-x.jsonl").write_text(
            '{"type":"assistant","timestamp":"2026-11-21T11:00:15.000Z",'
            '"message":{"id":"sa1","model":"claude-haiku-5-5","role":"assistant","content":[],'
            '"usage":{"input_tokens":5,"output_tokens":9000}}}\n',
            encoding="utf-8")
        second = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        self.assertEqual(second["model"], "claude-opus-5-5")  # main: 500 out, Haiku helper: 9000
        self.assertEqual(second["tokens_out"], 9500)          # helper tokens still count
        # ...and are priced as Haiku: the per-model split names each model's share.
        usage = {u["model"]: u for u in second["usage"]}
        self.assertEqual(sorted(usage), ["claude-haiku-5-5", "claude-opus-5-5"])
        self.assertEqual((usage["claude-haiku-5-5"]["out"], usage["claude-haiku-5-5"]["calls"]), (9000, 1))
        self.assertEqual(usage["claude-opus-5-5"]["out"], 500)
        for key, field in (("in", "tokens_in"), ("out", "tokens_out"), ("cr", "tokens_cache_read"),
                           ("cw", "tokens_cache_write"), ("calls", "llm_calls")):
            self.assertEqual(sum(u[key] for u in second["usage"]), second[field])

    def agent_file(self, name, *lines):
        sub_dir = self.transcript.parent / SESSION / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        with open(sub_dir / f"{name}.jsonl", "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def test_each_subagent_of_a_turn_is_listed_with_its_own_usage(self):
        before = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        self.assertNotIn("subagents", before)
        failed = [{"type": "tool_result", "tool_use_id": tool, "is_error": True, "content": text}
                  for tool, text in (("st1", "Exit code 1"), ("st2", "Permission to use Bash was denied"))]
        calls = assistant("sa1", "2026-11-21T11:00:15.500Z", out=7, attributionAgent="plugin:code-reviewer")
        calls["message"]["content"] = [{"type": "tool_use", "id": t, "name": "Bash", "input": {"command": "SECRET"}}
                                       for t in ("st1", "st2")]
        self.agent_file("agent-a", user("s1", "2026-11-21T11:00:15Z", "SECRET task"), calls,
                        user("s2", "2026-11-21T11:00:20Z", failed),
                        {"type": "api-request", "timestamp": "2026-11-21T11:30:00Z"})
        second_model = assistant("sb2", "2026-11-21T11:00:40Z", out=900, attributionAgent="not a plain name")
        second_model["message"].update(model="claude-haiku-5-5", usage={
            "input_tokens": 5, "output_tokens": 900, "cache_read_input_tokens": 11,
            "cache_creation_input_tokens": 13, "cache_creation": {"ephemeral_1h_input_tokens": 3}})
        self.agent_file("agent-b", second_model)
        for path in self.state_dir.iterdir():
            path.unlink()  # read both turns again
        events = claude_code.collect(self.hook, self.ctx)
        second = self.by_branch_turn(events, "2026-11-21T11:00:00Z")
        first_agent, other = second["subagents"]
        self.assertEqual(first_agent, {
            "agent_id": "agent-a", "agent_type": "plugin:code-reviewer", "model": "claude-opus-5-5",
            "ts_start": "2026-11-21T11:00:15Z", "ts_end": "2026-11-21T11:00:20Z",
            "in": 1, "out": 7, "cr": 0, "cw": 0, "cw1h": 0, "calls": 1,
            "tool_calls": 2, "tool_errors": 1, "tool_denials": 1})
        self.assertEqual(other, {
            "agent_id": "agent-b", "model": "claude-haiku-5-5",
            "ts_start": "2026-11-21T11:00:40Z", "ts_end": "2026-11-21T11:00:40Z",
            "in": 5, "out": 900, "cr": 11, "cw": 13, "cw1h": 3, "calls": 1,
            "tool_calls": 0, "tool_errors": 0, "tool_denials": 0})
        for key, total in (("in", "tokens_in"), ("out", "tokens_out"), ("cr", "tokens_cache_read"),
                           ("cw", "tokens_cache_write"), ("cw1h", "tokens_cache_write_1h"), ("calls", "llm_calls"),
                           ("tool_calls", "tool_calls"), ("tool_errors", "tool_errors"),
                           ("tool_denials", "tool_denials")):
            self.assertEqual(sum(s[key] for s in second["subagents"]), second[total] - before[total], key)
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertNotIn("subagents", self.by_branch_turn(events, "2026-11-21T10:00:00Z"))

    def test_agent_that_spans_turns_has_one_entry_per_turn_with_that_share(self):
        self.agent_file("agent-a", assistant("sa1", "2026-11-21T10:00:15Z", out=3),
                        assistant("sa2", "2026-11-21T10:59:59Z", out=4, attributionAgent="Explore"),
                        assistant("sa3", "2026-11-21T11:00:15Z", out=5),
                        user("s1", "2026-11-21T11:00:30Z", "x"))
        first, second = claude_code.collect(self.hook, self.ctx)
        shares = [(s["agent_type"], s["out"], s["calls"], s["ts_start"], s["ts_end"])
                  for e in (first, second) for s in e["subagents"]]
        self.assertEqual(shares, [
            ("Explore", 7, 2, "2026-11-21T10:00:15Z", "2026-11-21T10:59:59Z"),  # ends after its turn
            ("Explore", 5, 1, "2026-11-21T11:00:15Z", "2026-11-21T11:00:30Z")])
        # The next call re-reads only the last turn; the type is still the agent's.
        self.assertEqual(claude_code.collect(self.hook, self.ctx), [second])

    def test_window_without_a_model_call_has_an_unknown_model(self):
        self.agent_file("agent-a", assistant("sa1", "2026-11-21T10:00:15Z"),
                        user("s1", "2026-11-21T11:00:30Z", "x"))
        second = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        self.assertEqual([(s["model"], s["calls"], s["out"]) for s in second["subagents"]], [("unknown", 0, 0)])

    def test_what_the_main_conversation_counts_is_not_counted_for_a_subagent(self):
        # A forked agent's file starts with a copy of the parent's message and its tool call.
        parent = assistant("m3", "2026-11-21T11:00:11Z", out=500)
        parent["message"]["content"] = [{"type": "tool_use", "id": "t9", "name": "Agent", "input": {}}]
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(parent) + "\n")
        result = [{"type": "tool_result", "tool_use_id": "t9", "is_error": True, "content": "x"}]
        self.agent_file("agent-a", parent, user("s1", "2026-11-21T11:00:12Z", result),
                        assistant("sa1", "2026-11-21T11:00:15Z", out=7, attributionAgent="fork"))
        second = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        [fork] = second["subagents"]
        self.assertEqual((fork["agent_type"], fork["out"], fork["calls"], fork["tool_calls"], fork["tool_errors"]),
                         ("fork", 7, 1, 0, 0))
        self.assertEqual(fork["ts_start"], "2026-11-21T11:00:12Z")  # the copied line is the parent's
        self.assertEqual((second["tokens_out"], second["llm_calls"], second["tool_calls"]), (507, 2, 1))

    def test_a_label_in_the_file_name_is_not_sent(self):
        self.agent_file("agent-acompact-0123456789abcdef", assistant("sa1", "2026-11-21T11:00:15Z"))
        self.agent_file("agent-afedcba9876543210", assistant("sa2", "2026-11-21T11:00:16Z"))
        second = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        self.assertEqual([s["agent_id"] for s in second["subagents"]],
                         ["agent-a0123456789abcdef", "agent-afedcba9876543210"])

    def test_subagents_beyond_the_limit_are_left_out_latest_first_but_still_counted(self):
        for n in range(201):  # file names sort against start order: the earliest start is the last name
            self.agent_file(f"agent-{200 - n:03}",
                            assistant(f"sa{n}", f"2026-11-21T11:{1 + n // 60:02}:{n % 60:02}Z"))
        second = self.by_branch_turn(claude_code.collect(self.hook, self.ctx), "2026-11-21T11:00:00Z")
        ids = [s["agent_id"] for s in second["subagents"]]
        self.assertEqual((len(ids), ids[0], ids[-1]), (200, "agent-200", "agent-001"))
        self.assertEqual(second["llm_calls"], 202)

    def test_copilot_payloads_are_skipped(self):
        self.assertIsNone(claude_code.skip_reason(self.hook))
        self.assertTrue(claude_code.skip_reason({"session_id": "x", "hook_event_name": "SessionStart"}))
        self.assertTrue(claude_code.skip_reason({"session_id": "x", "transcript_path":
                                                 "/h/.copilot/session-state/x/events.jsonl"}))
        self.assertTrue(claude_code.skip_reason({"sessionId": "x", "transcriptPath": "/t.jsonl"}))

    def test_transcript_outside_claude_folder_is_ignored(self):
        other = self.ctx.root / "elsewhere.jsonl"
        shutil.copy(FIXTURE, other)
        self.assertEqual(claude_code.collect({"session_id": SESSION,
                                              "transcript_path": str(other)}, self.ctx), [])

    def test_missing_fields_return_nothing(self):
        self.assertEqual(claude_code.collect({}, self.ctx), [])


def user(uid, ts, content, prompt_id=None, **extra):
    line = {"type": "user", "uuid": uid, "timestamp": ts, "message": {"role": "user", "content": content}}
    if prompt_id:
        line["promptId"] = prompt_id
    line.update(extra)
    return line


def assistant(mid, ts, out=1, **extra):
    line = {"type": "assistant", "uuid": "a-" + mid + ts, "timestamp": ts,
            "message": {"id": mid, "model": "claude-opus-5-5", "role": "assistant", "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": out}}}
    line.update(extra)
    return line


class TranscriptShapeTests(unittest.TestCase):
    """Field shapes as found in real Claude Code transcripts."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        self.projects = base / "config" / "projects" / "proj"
        self.projects.mkdir(parents=True)
        self.ctx = SimpleNamespace(root=base, repo="owner/name", state_dir=self.state_dir,
                                   instruction_version="v1", default_branch="main")

    def transcript(self, session, lines, append=False):
        path = self.projects / f"{session}.jsonl"
        with open(path, "a" if append else "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return path

    def collect(self, session, event="Stop"):
        return claude_code.collect({"session_id": session, "hook_event_name": event,
                                    "transcript_path": str(self.projects / f"{session}.jsonl")}, self.ctx)

    def test_trigger_comes_from_origin_kind(self):
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "fix it", "p1", origin={"kind": "human"}, promptSource="sdk"),
            assistant("m1", "2026-11-21T10:00:05Z"),
            user("u2", "2026-11-21T10:01:00Z", "<task-notification>done</task-notification>", "p2",
                 origin={"kind": "task-notification"}, promptSource="system"),
            assistant("m2", "2026-11-21T10:01:05Z"),
            user("u3", "2026-11-21T10:02:00Z", "<task-notification>x", "p3"),
            assistant("m3", "2026-11-21T10:02:05Z"),
            user("u4", "2026-11-21T10:03:00Z", "plain, no origin", "p4"),
            assistant("m4", "2026-11-21T10:03:05Z"),
        ])
        self.assertEqual([e["trigger"] for e in self.collect(SESSION)], ["human", "system", "system", "human"])

    def test_compact_summary_transcript_only_and_command_output_do_not_start_turns(self):
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
            assistant("m1", "2026-11-21T10:00:05Z"),
            user("u2", "2026-11-21T10:00:10Z", "summary", isCompactSummary=True, isVisibleInTranscriptOnly=True),
            user("u3", "2026-11-21T10:00:11Z", "<local-command-stdout>ok</local-command-stdout>"),
            assistant("m2", "2026-11-21T10:00:20Z"),
        ])
        events = self.collect(SESSION)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["llm_calls"], 2)

    def test_turn_end_ignores_meta_and_bookkeeping_lines(self):
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
            assistant("m1", "2026-11-21T10:00:05Z"),
            {"type": "queue-operation", "timestamp": "2026-11-21T11:00:00Z"},
            {"type": "attachment", "timestamp": "2026-11-21T11:00:00Z"},
            user("u2", "2026-11-21T12:00:00Z", "caveat", isMeta=True),
        ])
        self.assertEqual(self.collect(SESSION)[0]["ts_end"], "2026-11-21T10:00:05Z")

    def test_confounders_are_read_per_turn(self):
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "go", "p1", permissionMode="auto", version="2.5.1",
                 entrypoint="claude-vscode"),
            assistant("m1", "2026-11-21T10:00:05Z", effort="high"),
            assistant("m2", "2026-11-21T10:00:06Z", effort="xhigh"),
            assistant("m3", "2026-11-21T10:00:07Z", effort="xhigh"),
            user("u2", "2026-11-21T10:01:00Z", "again", "p2", permissionMode="default"),
            assistant("m4", "2026-11-21T10:01:05Z"),
        ])
        first, second = self.collect(SESSION)
        self.assertEqual((first["effort"], first["permission_mode"], first["client_version"],
                          first["entrypoint"]), ("xhigh", "auto", "2.5.1", "claude-vscode"))
        self.assertEqual(second["permission_mode"], "default")
        self.assertNotIn("effort", second)

    def test_blocked_tool_calls_are_denials_not_errors(self):
        def result(uid, ts, tool_id, text):
            return user(uid, ts, [{"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                                   "content": [{"type": "text", "text": text}]}])
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
            assistant("m1", "2026-11-21T10:00:05Z"),
            result("u2", "2026-11-21T10:00:06Z", "t1", "Permission for this action was denied by the user"),
            result("u3", "2026-11-21T10:00:07Z", "t2", "PreToolUse:Bash hook error: blocked"),
            result("u4", "2026-11-21T10:00:08Z", "t3", "Exit code 1\nTraceback"),
            assistant("m2", "2026-11-21T10:00:09Z"),
        ])
        event = self.collect(SESSION)[0]
        self.assertEqual((event["tool_errors"], event["tool_denials"]), (1, 2))

    def test_one_hour_cache_writes_are_counted_separately(self):
        line = assistant("m1", "2026-11-21T10:00:05Z")
        line["message"]["usage"].update({"cache_creation_input_tokens": 300, "cache_creation": {
            "ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}})
        self.transcript(SESSION, [user("u1", "2026-11-21T10:00:00Z", "go", "p1"), line])
        event = self.collect(SESSION)[0]
        self.assertEqual((event["tokens_cache_write"], event["tokens_cache_write_1h"]), (300, 200))

    def test_version_is_pinned_at_session_start_and_on_reload(self):
        self.transcript(SESSION, [])
        self.collect(SESSION, event="SessionStart")        # Claude Code loaded v1
        self.transcript(SESSION, [user("u1", "2099-01-01T10:00:00Z", "go", "p1"),
                                  assistant("m1", "2099-01-01T10:00:05Z")], append=True)
        self.ctx.instruction_version = "v2"                   # CLAUDE.md edited mid-session
        self.assertEqual(self.collect(SESSION)[0]["instruction_version"], "v1")
        self.ctx.started_at = "2099-01-01T10:30:00Z"
        self.collect(SESSION, event="SessionStart")           # e.g. /compact reloads CLAUDE.md
        self.transcript(SESSION, [user("u2", "2099-01-01T11:00:00Z", "more", "p2"),
                                  assistant("m2", "2099-01-01T11:00:05Z")], append=True)
        events = self.collect(SESSION)
        self.assertEqual([e["instruction_version"] for e in events], ["v1", "v2"])

    def test_without_session_start_the_first_seen_version_is_kept(self):
        self.transcript(SESSION, [user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
                                  assistant("m1", "2026-11-21T10:00:05Z")])
        self.collect(SESSION)
        self.ctx.instruction_version = "v2"
        self.assertEqual(self.collect(SESSION)[0]["instruction_version"], "v1")

    def test_other_sessions_that_grew_are_swept(self):
        other = "99999999-0000-0000-0000-000000000000"
        self.transcript(other, [user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
                                assistant("m1", "2026-11-21T10:00:05Z")])
        self.collect(other, event="SessionEnd")
        self.transcript(other, [assistant("m2", "2026-11-21T10:00:09Z", out=7)], append=True)
        self.transcript(SESSION, [])
        swept = self.collect(SESSION)
        self.assertEqual([(e["session_id"], e["llm_calls"]) for e in swept], [(other, 2)])
        self.assertEqual(self.collect(SESSION), [])  # unchanged transcripts are not read again

    def test_state_of_deleted_transcript_is_removed(self):
        other = "99999999-0000-0000-0000-000000000000"
        path = self.transcript(other, [user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
                                       assistant("m1", "2026-11-21T10:00:05Z")])
        self.collect(other)
        path.unlink()
        self.transcript(SESSION, [])
        self.collect(SESSION)
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()),
                         [f"claude-code-{SESSION}.json", "retired-claude-code.json"])

    def test_trunk_and_detached_work_is_one_task_per_session(self):
        self.transcript(SESSION, [
            user("u1", "2026-11-21T10:00:00Z", "go", "p1", gitBranch="main"),
            assistant("m1", "2026-11-21T10:00:05Z"),
            user("u2", "2026-11-21T10:01:00Z", "go", "p2", gitBranch="HEAD"),
            assistant("m2", "2026-11-21T10:01:05Z"),
            user("u3", "2026-11-21T10:02:00Z", "go", "p3", gitBranch="feature/y"),
            assistant("m3", "2026-11-21T10:02:05Z"),
        ])
        self.assertEqual([e["boundary_ref"] for e in self.collect(SESSION)],
                         ["main@11111111", "detached@11111111", "feature/y"])

    def test_transcript_must_be_named_after_the_session(self):
        self.transcript("other-name", [user("u1", "2026-11-21T10:00:00Z", "go", "p1"),
                                       assistant("m1", "2026-11-21T10:00:05Z")])
        self.assertEqual(claude_code.collect({"session_id": SESSION, "transcript_path":
                                              str(self.projects / "other-name.jsonl")}, self.ctx), [])


if __name__ == "__main__":
    unittest.main()
