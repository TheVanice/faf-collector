import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))

import faf_instructions  # noqa: E402
from faf_instructions import git_dirty, instruction_snapshot, instruction_version  # noqa: E402
from providers import claude_code  # noqa: E402

# A provider stand-in: every CLAUDE.md-named file is an instruction file.
PROVIDER = SimpleNamespace(
    instruction_files=lambda paths: [p for p in paths if p.endswith("CLAUDE.md")]
)


class InstructionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("CLAUDE.md", "rule one\n")

    def write(self, rel, text, binary=False):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(text)
        else:
            path.write_bytes(text.encode("utf-8"))
        return path

    def version(self):
        return instruction_version(self.root, PROVIDER)

    def test_same_content_gives_same_version(self):
        first = self.version()
        self.assertEqual(first, self.version())
        self.assertEqual(len(first), 12)

    def test_one_byte_change_changes_version(self):
        before = self.version()
        self.write("CLAUDE.md", "rule one.\n")
        self.assertNotEqual(before, self.version())

    def test_crlf_and_lf_give_same_version(self):
        lf = self.version()
        self.write("CLAUDE.md", b"rule one\r\n", binary=True)
        self.assertEqual(lf, self.version())

    def test_git_ignored_instruction_file_does_not_count(self):
        before = self.version()
        (self.root / ".gitignore").write_text("CLAUDE.local.md\n", encoding="utf-8")
        self.write("CLAUDE.local.md", "personal\n")
        self.assertEqual(before, self.version())

    def test_untracked_non_ignored_file_counts(self):
        before = self.version()
        self.write("sub/CLAUDE.md", "nested\n")
        self.assertNotEqual(before, self.version())

    def test_no_instruction_files_gives_none(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        self.assertEqual(faf_instructions.instruction_snapshot(empty, PROVIDER), ("none", []))

    def test_snapshot_version_matches_version_function(self):
        version, files = instruction_snapshot(self.root, PROVIDER)
        self.assertEqual(version, self.version())
        self.assertEqual(files[0]["path"], "CLAUDE.md")
        self.assertEqual(files[0]["content"], "rule one\n")

    def test_snapshot_keeps_text_locally_whatever_the_upload_setting(self):
        self.write(".faf/config.json", '{"upload_instruction_content": false}')
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertEqual(files[0]["content"], "rule one\n")
        self.assertEqual((files[0]["kind"], files[0]["size"]), ("memory", 9))

    def test_large_file_has_no_content(self):
        self.write("CLAUDE.md", "x" * (faf_instructions.MAX_CONTENT_BYTES + 1))
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertIsNone(files[0]["content"])

    def test_extra_patterns_from_config(self):
        self.write("docs/rules.txt", "extra\n")
        self.write(".faf/config.json", '{"extra_instruction_files": ["docs/*.txt"]}')
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertEqual([f["path"] for f in files], ["CLAUDE.md", "docs/rules.txt"])

    def test_binary_file_has_hash_but_no_content(self):
        self.write("CLAUDE.md", b"PNG\x00\x01", binary=True)
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertIsNone(files[0]["content"])

    def test_content_is_the_exact_hashed_bytes(self):
        # Not UTF-8: a lossy decode would not match sha256, so the file goes without content.
        self.write("CLAUDE.md", b"caf\xe9\n", binary=True)
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertIsNone(files[0]["content"])

    def test_content_hashes_to_sha256(self):
        self.write("CLAUDE.md", "rule\r\nTwo ä\r\n")
        _, files = instruction_snapshot(self.root, PROVIDER)
        self.assertEqual(hashlib.sha256(files[0]["content"].encode("utf-8")).hexdigest(), files[0]["sha256"])

    def test_total_uploaded_content_is_capped(self):
        size = faf_instructions.MAX_CONTENT_BYTES - 10
        count = faf_instructions.MAX_TOTAL_CONTENT_BYTES // size + 2
        for i in range(count):
            self.write(f"d{i:02}/CLAUDE.md", "x" * size)
        _, files = instruction_snapshot(self.root, PROVIDER)
        upload = faf_instructions.upload_files(files)
        sent = sum(len(f["content"]) for f in upload if f["content"] is not None)
        self.assertLessEqual(sent, faf_instructions.MAX_TOTAL_CONTENT_BYTES)
        self.assertTrue(any(f["content"] is None for f in upload))
        self.assertEqual(set(upload[0]), {"path", "sha256", "content"})

    def test_dirty_detection(self):
        subprocess.run(["git", "-C", str(self.root), "add", "CLAUDE.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "init"], check=True)
        self.assertFalse(git_dirty(self.root, ["CLAUDE.md"]))
        self.write("CLAUDE.md", "changed\n")
        self.assertTrue(git_dirty(self.root, ["CLAUDE.md"]))


class ImportTests(unittest.TestCase):
    """@path imports in Claude Code memory files are part of the version."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (Path(temp.name) / "outside.md").write_text("outside\n", encoding="utf-8")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def paths(self):
        return [f["path"] for f in instruction_snapshot(self.root, claude_code)[1]]

    def test_import_is_resolved_from_the_importing_file(self):
        self.write("docs/CLAUDE.md", "See @guide.md.\n")
        self.write("docs/guide.md", "Then @../shared/a.md\n")
        self.write("shared/a.md", "a\n")
        self.assertEqual(self.paths(), ["docs/CLAUDE.md", "docs/guide.md", "shared/a.md"])

    def test_editing_an_imported_file_changes_the_version(self):
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write("AGENTS.md", "one\n")
        before = instruction_version(self.root, claude_code)
        self.write("AGENTS.md", "two\n")
        self.assertNotEqual(before, instruction_version(self.root, claude_code))

    def test_imports_outside_the_repo_or_git_ignored_are_skipped(self):
        self.write(".gitignore", "private.md\n")
        self.write("private.md", "secret\n")
        self.write("CLAUDE.md", "@../outside.md @/etc/hosts @~/x.md @C:/x.md @private.md @missing.md\n")
        self.assertEqual(self.paths(), ["CLAUDE.md"])

    def test_code_spans_blocks_and_emails_are_not_imports(self):
        for name in ("a.md", "b.md", "example.com"):
            self.write(name, "x\n")
        self.write("CLAUDE.md", "Mail me@example.com. Use `@a.md`.\n```\n@b.md\n```\n")
        self.assertEqual(self.paths(), ["CLAUDE.md"])

    def test_skill_files_do_not_import(self):
        self.write(".claude/skills/s/SKILL.md", "@extra.md\n")
        self.write("extra.md", "x\n")
        self.assertEqual(self.paths(), [".claude/skills/s/SKILL.md"])

    def test_import_cycles_and_depth_are_bounded(self):
        self.write("CLAUDE.md", "@f1.md\n")
        for i in range(1, 8):
            self.write(f"f{i}.md", f"@f{i + 1}.md @CLAUDE.md\n")
        self.assertEqual(self.paths(), ["CLAUDE.md"] + [f"f{i}.md" for i in range(1, 6)])


class ChangeShapeTests(unittest.TestCase):
    def test_kinds_by_path(self):
        kind = faf_instructions.file_kind
        cases = {"CLAUDE.md": "memory", "sub/AGENTS.md": "memory", ".github/copilot-instructions.md": "memory",
                 "GEMINI.md": "memory", ".claude/rules/a.md": "rule", ".github/instructions/x.instructions.md": "rule",
                 ".claude/skills/s/SKILL.md": "skill", ".github/skills/s/SKILL.md": "skill",
                 ".claude/agents/a.md": "agent", ".github/agents/a.md": "agent",
                 ".claude/commands/c.md": "command", ".github/prompts/p.prompt.md": "prompt",
                 "docs/x.txt": "other"}
        for path, expected in cases.items():
            self.assertEqual(kind(path), expected, path)
        self.assertEqual(kind("docs/guide.md", imported=True), "import")

    def test_imported_files_have_kind_import(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "CLAUDE.md").write_text("@guide.md\n", encoding="utf-8")
        (root / "guide.md").write_text("g\n", encoding="utf-8")
        files = instruction_snapshot(root, claude_code)[1]
        self.assertEqual({f["path"]: f["kind"] for f in files}, {"CLAUDE.md": "memory", "guide.md": "import"})

    @staticmethod
    def file(path, text, kind="memory"):
        import hashlib
        raw = text.encode("utf-8")
        return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "kind": kind,
                "content": text}

    def test_first_version_lists_every_file_as_added(self):
        changes = faf_instructions.change_shape(None, [self.file("CLAUDE.md", "a\nb\n")])
        self.assertEqual(changes, [{"kind": "memory", "status": "added", "lines_added": 2,
                                    "lines_removed": 0, "size": 4}])

    def test_changed_removed_and_unchanged_files(self):
        old = [self.file("CLAUDE.md", "a\nb\nc\n"), self.file(".claude/rules/r.md", "r\n", "rule"),
               self.file("same.md", "s\n", "other")]
        new = [self.file("CLAUDE.md", "a\nB\nc\nd\n"), self.file("same.md", "s\n", "other")]
        previous = faf_instructions.local_snapshot("v1", old)["files"]
        changes = faf_instructions.change_shape(previous, new)
        self.assertEqual(changes, [
            {"kind": "rule", "status": "removed", "lines_added": 0, "lines_removed": 1, "size": 2},
            {"kind": "memory", "status": "changed", "lines_added": 2, "lines_removed": 1, "size": 8},
        ])
        self.assertNotIn("path", json.dumps(changes))

    def test_binary_change_counts_no_lines(self):
        old = dict(self.file("CLAUDE.md", "x"), content=None)
        new = dict(self.file("CLAUDE.md", "y"), content=None)
        changes = faf_instructions.change_shape({"CLAUDE.md": old}, [new])
        self.assertEqual((changes[0]["status"], changes[0]["lines_added"], changes[0]["lines_removed"]),
                         ("changed", 0, 0))


if __name__ == "__main__":
    unittest.main()
