import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_cli  # noqa: E402
import faf_settings  # noqa: E402
from support import KEY, read_settings, temp_home, write_settings  # noqa: E402

VERSION = faf_settings.COLLECTOR_VERSION


def release_zip(files=faf_cli.CLI_FILES):
    """A release zip as the public repo's release workflow builds it (prefix faf/)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel in files:
            z.write(COLLECTOR / rel, f"faf/{rel}")
    return buf.getvalue()


def run(*argv):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = faf_cli.main(list(argv))
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self, clear_env=True)
        self.path_mock = self.patch(faf_cli, "_user_path")  # never touch the real user PATH
        self.patch(faf_cli.faf_install, "interactive")

    def patch(self, owner, name):
        patcher = mock.patch.object(owner, name, return_value=False)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_self_install_copies_cli_writes_launchers_and_prunes(self):
        old = self.home / ".faf" / "cli" / "0.9.0"
        old.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            target = faf_cli.self_install(COLLECTOR, "py3")
        self.assertEqual(target, self.home / ".faf" / "cli" / VERSION)
        for rel in faf_cli.CLI_FILES:
            self.assertTrue((target / rel).exists(), rel)
        self.assertFalse((target / "tests").exists())
        self.assertFalse(old.exists())
        sh = (faf_cli.bin_dir() / "faf").read_text(encoding="utf-8")
        self.assertIn(f'FAF_PYTHON=py3 exec py3 "{(target / "faf_cli.py").as_posix()}" "$@"', sh)
        if faf_cli.WINDOWS:
            cmd = (faf_cli.bin_dir() / "faf.cmd").read_text(encoding="utf-8")
            self.assertIn(f'"py3" "{target / "faf_cli.py"}" %*', cmd)
            self.path_mock.assert_called_once()
        # Running from the installed copy (re-run) keeps it.
        with contextlib.redirect_stdout(io.StringIO()):
            faf_cli.self_install(target, "py3")
        self.assertTrue((target / "faf_cli.py").exists())

    def test_installed_launcher_runs(self):
        with contextlib.redirect_stdout(io.StringIO()):
            target = faf_cli.self_install(COLLECTOR, sys.executable)
        out = subprocess.run([sys.executable, str(target / "faf_cli.py"), "version"],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), VERSION)

    def fetcher(self, tag, data, checksum=None):
        checksum = hashlib.sha256(data).hexdigest() if checksum is None else checksum
        base = faf_cli.RELEASES_URL
        name = f"faf-collector-{tag}.zip"
        pages = {
            f"{base}/latest": (b"", f"{base}/tag/{tag}"),
            f"{base}/download/{tag}/{name}": (data, ""),
            f"{base}/download/{tag}/{name}.sha256": (f"{checksum}  {name}\n".encode(), ""),
        }
        return mock.patch.object(faf_cli, "fetch", side_effect=lambda url: pages[url])

    def test_latest_tag_follows_redirect(self):
        with self.fetcher("v9.1.0", b""):
            self.assertEqual(faf_cli.latest_tag(), "v9.1.0")

    def test_download_release_verifies_checksum(self):
        data = release_zip()
        with tempfile.TemporaryDirectory() as temp, self.fetcher("v9.0.0", data):
            source = faf_cli.download_release("v9.0.0", temp)
            self.assertTrue((source / "faf_cli.py").exists())
        with tempfile.TemporaryDirectory() as temp, self.fetcher("v9.0.0", data, "0" * 64):
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                faf_cli.download_release("v9.0.0", temp)

    def test_download_release_rejects_zip_without_cli(self):
        data = release_zip(("faf_hook.py",))
        with tempfile.TemporaryDirectory() as temp, self.fetcher("v9.0.0", data):
            with self.assertRaisesRegex(SystemExit, "does not contain"):
                faf_cli.download_release("v9.0.0", temp)

    def test_upgrade_check_and_up_to_date(self):
        with self.fetcher("v9.0.0", b""):
            code, out = run("upgrade", "--check")
        self.assertIn(f"installed {VERSION}, latest 9.0.0: run  faf upgrade", out)
        with self.fetcher(f"v{VERSION}", b""), mock.patch.object(faf_cli, "download_release") as download:
            code, out = run("upgrade")
        self.assertIn("is up to date", out)
        download.assert_not_called()

    def test_upgrade_installs_new_release_with_its_own_code(self):
        with self.fetcher("v9.0.0", release_zip()), \
                mock.patch.object(faf_cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as sub:
            code, out = run("upgrade")
        self.assertEqual(code, 0)
        cmd = sub.call_args[0][0]
        self.assertTrue(cmd[1].endswith(str(Path("unpacked") / "faf" / "faf_cli.py")))
        self.assertEqual(cmd[2:], ["self-install", "--python", faf_cli.installed_python(), "--upgrade"])

    def test_init_targets_git_root_and_passes_options(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "sub").mkdir()
            with mock.patch.object(faf_cli.faf_install, "main", return_value=0) as main:
                run("init", str(repo / "sub"), "--providers", "claude-code", "--yes", "--upload-instructions", "no")
            argv = main.call_args[0][0]
            self.assertEqual(Path(argv[1]).resolve(), repo.resolve())
            self.assertEqual(argv[2:], ["--providers", "claude-code", "--upload-instructions", "no", "--yes"])
            with mock.patch.object(faf_cli.faf_install, "main", return_value=0) as main:
                run("uninit", str(repo))
            self.assertEqual(main.call_args[0][0][2:], ["--providers", "all", "--uninstall"])

    def test_init_outside_git_fails(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(SystemExit, "not inside a git"):
            run("init", temp)

    def test_install_keeps_existing_key(self):
        write_settings(self.home, {"api_key": KEY})
        code, out = run("install", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("API key configured", out)
        self.assertEqual(read_settings(self.home)["api_key"], KEY)

    def test_install_without_key_explains_invite(self):
        code, out = run("install")
        self.assertIn("No API key configured", out)

    def test_uninstall_removes_cli_keeps_settings(self):
        write_settings(self.home, {"api_key": KEY})
        with contextlib.redirect_stdout(io.StringIO()):
            faf_cli.self_install(COLLECTOR, "python")
        with mock.patch.object(faf_cli, "delete_after_exit") as later:
            code, _ = run("uninstall", "--yes")
        self.assertEqual(code, 0)
        if faf_cli.WINDOWS:
            later.assert_called_once_with(faf_cli.bin_dir() / "faf.cmd")
        self.assertFalse((self.home / ".faf" / "cli").exists())
        self.assertFalse((faf_cli.bin_dir() / "faf").exists())
        self.assertEqual(read_settings(self.home)["api_key"], KEY)

    def test_uninstall_needs_yes_without_terminal(self):
        with self.assertRaisesRegex(SystemExit, "--yes"):
            run("uninstall")


if __name__ == "__main__":
    unittest.main()
