import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))

import faf_event  # noqa: E402
from faf_event import Spool, StepEvent, ensure_gitignore, git_repo_slug, make_event_id  # noqa: E402


class EventIdTests(unittest.TestCase):
    def test_id_is_stable_and_depends_on_all_parts(self):
        a = make_event_id("claude-code", "s1", "p1")
        self.assertEqual(a, make_event_id("claude-code", "s1", "p1"))
        self.assertEqual(len(a), 32)
        self.assertNotEqual(a, make_event_id("claude-code", "s1", "p2"))
        self.assertNotEqual(a, make_event_id("copilot", "s1", "p1"))


class RepoSlugTests(unittest.TestCase):
    def slug_for(self, url, root="/work/fallback"):
        with mock.patch.object(faf_event, "_git", return_value=url):
            return git_repo_slug(Path(root))

    def test_https_url(self):
        self.assertEqual(self.slug_for("https://github.com/Owner/Name.git"), "Owner/Name")

    def test_ssh_url(self):
        self.assertEqual(self.slug_for("git@github.com:Owner/Name.git"), "Owner/Name")

    def test_url_without_suffix(self):
        self.assertEqual(self.slug_for("https://github.com/Owner/Name/"), "Owner/Name")

    def test_azure_devops_urls_agree(self):
        for url in ("https://contoso.visualstudio.com/Shop/_git/Web",
                    "https://org@dev.azure.com/org/Shop/_git/Web",
                    "git@ssh.dev.azure.com:v3/org/Shop/Web"):
            self.assertEqual(self.slug_for(url), "Shop/Web", url)

    def test_no_origin_uses_folder_name(self):
        self.assertEqual(self.slug_for(None, root="/work/my-repo"), "my-repo")


class GitignoreTests(unittest.TestCase):
    def test_line_is_added_once(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".gitignore"
            path.write_text("existing", encoding="utf-8")
            self.assertTrue(ensure_gitignore(root, "/.faf/local/"))
            self.assertFalse(ensure_gitignore(root, "/.faf/local/"))
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(),
                             ["existing", "/.faf/local/"])


class SpoolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "spool.jsonl"

    def test_failed_batch_is_returned_again(self):
        spool = Spool(self.path)
        spool.append(StepEvent(event_id="e1", provider="p", repo="r", boundary_ref="b",
                               session_id="s", ts_start="t", ts_end="t", instruction_version="v"))
        first = spool.take_batches()
        self.assertEqual(len(first), 1)
        second = spool.take_batches()
        self.assertEqual([p for p, _ in second], [p for p, _ in first])
        self.assertEqual(second[0][1][0]["event_id"], "e1")

    def test_event_written_after_take_is_kept(self):
        spool = Spool(self.path)
        spool.append({"event_id": "old"})
        batches = spool.take_batches()
        spool.append({"event_id": "new"})  # written while a flush is in progress
        os.remove(batches[0][0])
        remaining = spool.take_batches()
        self.assertEqual([e["event_id"] for _, events in remaining for e in events], ["new"])

    def test_batches_taken_in_the_same_clock_tick_are_all_kept(self):
        spool = Spool(self.path)
        with mock.patch.object(faf_event.time, "time_ns", return_value=1):  # coarse clock: Windows, Python < 3.13
            spool.append({"event_id": "first"})
            spool.take_batches()
            spool.append({"event_id": "second"})
            batches = spool.take_batches()
        self.assertEqual([e["event_id"] for _, events in batches for e in events], ["first", "second"])

    def test_step_event_drops_none_fields(self):
        event = StepEvent(event_id="e", provider="p", repo="r", boundary_ref="b", session_id="s",
                          ts_start="t", ts_end="t", instruction_version="v", model=None)
        data = event.to_json()
        self.assertNotIn("model", data)
        self.assertEqual(data["schema"], "faf.v1")
        self.assertEqual(data["kind"], "step")


if __name__ == "__main__":
    unittest.main()
