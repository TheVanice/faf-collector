import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.common import boundary, task_from_title  # noqa: E402

CTX = SimpleNamespace(default_branch="main")
SID = "aaaaaaaa-1111-2222-3333-444444444444"


class TaskTitleTests(unittest.TestCase):
    def test_task_prefix_is_dropped(self):
        for title in ("Task: BFF-Spec", "TaskId: BFF-Spec", "task id = BFF-Spec", "Task-ID:BFF-Spec",
                      "task_id: BFF-Spec", "  TASK : BFF-Spec  ", "Task: BFF-Spec\r\n\r\nWrite the spec"):
            self.assertEqual(task_from_title(title), "BFF-Spec", title)

    def test_any_other_title_is_taken_as_it_is(self):
        for title in ("Tasks: x", "Write the BFF spec", "My Task: x", "Taskforce: x"):
            self.assertEqual(task_from_title(title), title)
        self.assertEqual(task_from_title("Write the spec\r\nmore"), "Write the spec")
        self.assertEqual(task_from_title("x" * 200), "x" * 120)

    def test_empty_titles_give_no_task(self):
        for title in (None, "", "  \n ", "Task:"):
            self.assertIsNone(task_from_title(title), title)


class BoundaryOrderTests(unittest.TestCase):
    def test_branch_wins_over_everything(self):
        self.assertEqual(boundary("feature/x", SID, CTX, worktree="wt", task="t"), "feature/x")

    def test_then_worktree_then_task_then_fallback(self):
        self.assertEqual(boundary("main", SID, CTX, worktree="wt", task="t"), "wt")
        self.assertEqual(boundary("main", SID, CTX, task="t"), "t")
        self.assertEqual(boundary("main", SID, CTX), "main@aaaaaaaa")

    def test_detached_head(self):
        self.assertEqual(boundary(None, SID, CTX, task="t"), "t")
        self.assertEqual(boundary("HEAD", SID, CTX), "detached@aaaaaaaa")


if __name__ == "__main__":
    unittest.main()
