import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_log  # noqa: E402
import faf_post  # noqa: E402
import faf_settings  # noqa: E402
from faf_event import Spool, local_dir  # noqa: E402
from support import KEY, OTHER_KEY, read_settings, temp_home, write_settings  # noqa: E402


def step(n):
    return {"schema": "faf.v1", "kind": "step", "event_id": f"e{n}"}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self, clear_env=True)

    def test_no_settings_means_no_key(self):
        self.assertEqual(faf_settings.resolve(), (None, None, "no API key"))

    def test_key_from_file_goes_to_builtin_endpoint(self):
        write_settings(self.home, {"api_key": KEY})
        self.assertEqual(faf_settings.resolve(), (faf_settings.BUILTIN_ENDPOINT, KEY, None))

    def test_endpoint_in_the_same_file_as_the_key_is_used(self):
        write_settings(self.home, {"api_key": KEY, "endpoint": "https://other.test/api/ingest"})
        self.assertEqual(faf_settings.resolve()[:2], ("https://other.test/api/ingest", KEY))

    def test_environment_key_only_goes_to_builtin_endpoint(self):
        write_settings(self.home, {"api_key": KEY, "endpoint": "https://other.test/api/ingest"})
        with mock.patch.dict(os.environ, {"FAF_API_KEY": OTHER_KEY}):
            self.assertEqual(faf_settings.resolve(), (faf_settings.BUILTIN_ENDPOINT, OTHER_KEY, None))

    def test_no_environment_variable_redirects_the_key(self):
        write_settings(self.home, {"api_key": KEY})
        with mock.patch.dict(os.environ, {"FAF_INGEST_URL": "https://evil.test", "FAF_ENDPOINT": "https://evil.test",
                                          "FAF_INGEST_KEY": OTHER_KEY, "FAF_HOME": "/evil"}):
            self.assertEqual(faf_settings.resolve(), (faf_settings.BUILTIN_ENDPOINT, KEY, None))

    def test_repo_secrets_file_is_ignored(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        local_dir(repo).joinpath("secrets.json").write_text(
            json.dumps({"FAF_INGEST_URL": "https://evil.test", "FAF_INGEST_KEY": KEY}), encoding="utf-8")
        self.assertFalse(faf_post.is_configured(repo))

    def test_plain_http_is_refused_except_loopback(self):
        write_settings(self.home, {"api_key": KEY, "endpoint": "http://example.test/api/ingest"})
        self.assertEqual(faf_settings.resolve()[2], "endpoint must use https")
        for host in ("127.0.0.1:8080", "localhost", "[::1]:9"):
            write_settings(self.home, {"api_key": KEY, "endpoint": f"http://{host}/api/ingest"})
            self.assertIsNone(faf_settings.resolve()[2], host)

    def test_endpoint_with_credentials_is_refused(self):
        for netloc in ("user:secret@example.test", "user@example.test", "@example.test", "u:p@localhost"):
            scheme = "http" if "localhost" in netloc else "https"
            write_settings(self.home, {"api_key": KEY, "endpoint": f"{scheme}://{netloc}/api/ingest"})
            self.assertEqual(faf_settings.resolve()[2], "endpoint must not contain credentials", netloc)
            self.assertNotIn("secret", faf_settings.endpoint_host())

    def test_display_host_leaves_out_credentials(self):
        self.assertEqual(faf_settings.display_host("https://u:secret@example.test:8443/x"), "example.test:8443")
        self.assertEqual(faf_settings.display_host("http://[::1]:9/x"), "[::1]:9")
        self.assertEqual(faf_settings.display_host("https://example.test/x"), "example.test")

    def test_malformed_key_is_not_sent(self):
        write_settings(self.home, {"api_key": "faf_short"})
        self.assertEqual(faf_settings.resolve()[2], "API key malformed")

    def test_unreadable_settings(self):
        path = write_settings(self.home, {})
        path.write_text("{", encoding="utf-8")
        self.assertEqual(faf_settings.resolve()[2], "settings file unreadable")

    def test_upload_is_off_until_the_user_consents_and_repo_can_only_disable(self):
        self.assertFalse(faf_settings.upload_enabled("o/r", {"upload_instruction_content": True}))
        faf_settings.set_upload_consent("o/r", True)
        self.assertTrue(faf_settings.upload_enabled("o/r", {}))
        self.assertFalse(faf_settings.upload_enabled("o/r", {"upload_instruction_content": False}))
        self.assertFalse(faf_settings.upload_enabled("o/other", {}))
        faf_settings.set_upload_consent("o/r", False)
        self.assertFalse(faf_settings.upload_enabled("o/r", {}))

    def test_upload_reminder_names_what_turns_it_on(self):
        self.assertIn("faf init --upload-instructions yes", faf_settings.upload_reminder("o/r", {}) or "")
        faf_settings.set_upload_consent("o/r", True)
        self.assertIsNone(faf_settings.upload_reminder("o/r", {}))
        self.assertIn("upload_instruction_content to false",
                      faf_settings.upload_reminder("o/r", {"upload_instruction_content": False}) or "")

    def test_storing_the_key_keeps_other_settings(self):
        write_settings(self.home, {"instruction_upload": {"o/r": True}})
        faf_settings.store_key(KEY)
        self.assertEqual(read_settings(self.home), {"instruction_upload": {"o/r": True}, "api_key": KEY})

    @unittest.skipIf(os.name != "posix", "file modes are POSIX only")
    def test_key_file_is_private_under_a_permissive_umask(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        faf_settings.store_key(KEY)
        path = faf_settings.settings_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    @unittest.skipIf(os.name != "posix", "file modes are POSIX only")
    def test_group_or_world_readable_settings_are_tightened(self):
        path = write_settings(self.home, {"api_key": KEY})
        path.chmod(0o644)
        faf_settings.resolve()  # reading alone tightens the file
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.chmod(0o644)
        path.with_name(path.name + ".tmp").write_text("{}", encoding="utf-8")
        path.with_name(path.name + ".tmp").chmod(0o644)  # a leftover temp file from an older install
        faf_settings.store_key(KEY)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(read_settings(self.home), {"api_key": KEY})


class PostTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self, clear_env=True)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.spool = Spool(local_dir(self.root) / "spool.jsonl")

    def configure(self, endpoint="https://example.test/ingest"):
        write_settings(self.home, {"api_key": KEY, "endpoint": endpoint})

    def waiting(self):
        return sum(len(events) for _, events in self.spool.take_batches())

    def rejected(self):
        path = faf_post.rejected_path(self.root)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_events_are_posted_and_batch_is_removed(self):
        self.configure()
        self.spool.append(step(1))
        with mock.patch.object(faf_post, "post_batch", return_value=(200, {"accepted": 1})) as post:
            result = faf_post.flush(self.root, retries=0)
        self.assertTrue(result)
        self.assertEqual((result.sent, result.status), (1, 200))
        post.assert_called_once_with([step(1)], "https://example.test/ingest", KEY, timeout=20)
        self.assertEqual(self.waiting(), 0)

    def test_without_key_nothing_is_sent_and_events_stay(self):
        self.spool.append(step(1))
        with mock.patch.object(faf_post, "post_batch") as post:
            result = faf_post.flush(self.root, retries=0)
        self.assertFalse(result)
        self.assertEqual(result.error, "no API key")
        post.assert_not_called()
        self.assertEqual(self.waiting(), 1)

    def test_failed_post_keeps_batch_for_next_time(self):
        self.configure()
        self.spool.append(step(1))
        with mock.patch.object(faf_post, "post_batch", side_effect=urllib.error.URLError("offline")):
            result = faf_post.flush(self.root, retries=0)
        self.assertEqual((result.error, result.queued), ("no connection", 1))
        self.assertEqual(self.waiting(), 1)

    def test_unknown_key_stops_and_keeps(self):
        self.configure()
        self.spool.append(step(1))
        with mock.patch.object(faf_post, "post_batch", return_value=(401, "key not accepted")):
            result = faf_post.flush(self.root, retries=0)
        self.assertEqual(result.error, "401 key not accepted")
        self.assertEqual(self.waiting(), 1)
        self.assertEqual(self.rejected(), [])

    def test_large_flush_is_split_into_chunks(self):
        self.configure()
        for n in range(faf_post.MAX_BATCH + 3):
            self.spool.append(step(n))
        answers = [(200, {"accepted": faf_post.MAX_BATCH}), (200, {"accepted": 3})]
        with mock.patch.object(faf_post, "post_batch", side_effect=answers) as post:
            self.assertTrue(faf_post.flush(self.root, retries=0))
        self.assertEqual([len(c.args[0]) for c in post.call_args_list], [faf_post.MAX_BATCH, 3])

    def test_rejected_events_are_kept_with_reason(self):
        self.configure()
        self.spool.append(step(1))
        self.spool.append(step(2))
        body = {"accepted": 1, "rejected": [{"index": 1, "error": "missing fields: ts_end"}]}
        with mock.patch.object(faf_post, "post_batch", return_value=(200, body)):
            result = faf_post.flush(self.root, retries=0)
        self.assertTrue(result)
        self.assertEqual((result.sent, result.rejected), (1, 1))
        self.assertEqual([(r["event"], r["reason"]) for r in self.rejected()],
                         [(step(2), "missing fields: ts_end")])
        self.assertEqual(self.waiting(), 0)

    def test_answer_that_does_not_account_for_every_event_keeps_batch(self):
        self.configure()
        self.spool.append(step(1))
        self.spool.append(step(2))
        for body in ({"accepted": 1}, {"accepted": 1, "rejected": []}, {"accepted": 3},
                     {"accepted": True, "rejected": [{"index": 1}]},
                     {"accepted": 0, "rejected": [{"index": 0}, {"index": 0}]},
                     {"accepted": 1, "rejected": [{"index": 5}]}, {"accepted": 1, "rejected": 2}):
            with mock.patch.object(faf_post, "post_batch", return_value=(200, body)):
                result = faf_post.flush(self.root, retries=0)
            self.assertEqual(result.error, "200 not an ingest answer", body)
            self.assertEqual(self.waiting(), 2, body)
        self.assertEqual(self.rejected(), [])

    def test_old_backend_rejection_count_keeps_whole_chunk(self):
        self.configure()
        self.spool.append(step(1))
        self.spool.append(step(2))
        with mock.patch.object(faf_post, "post_batch", return_value=(200, {"accepted": 0, "rejected": 2})):
            faf_post.flush(self.root, retries=0)
        self.assertEqual([r["event"] for r in self.rejected()], [step(1), step(2)])

    def test_client_error_quarantines_batch_and_continues(self):
        self.configure()
        self.spool.append(step(1))
        self.spool.take_batches()  # first batch file
        self.spool.append(step(2))
        with mock.patch.object(faf_post, "post_batch", side_effect=[(400, "bad"), (200, {"accepted": 1})]) as post:
            self.assertTrue(faf_post.flush(self.root, retries=0))
        self.assertEqual(post.call_count, 2)
        self.assertEqual([r["reason"] for r in self.rejected()], ["HTTP 400: bad"])
        self.assertEqual(self.waiting(), 0)

    def test_server_error_stops_and_keeps_every_batch(self):
        self.configure()
        self.spool.append(step(1))
        self.spool.take_batches()
        self.spool.append(step(2))
        with mock.patch.object(faf_post, "post_batch", return_value=(503, "busy")) as post:
            result = faf_post.flush(self.root, retries=0)
        self.assertFalse(result)
        self.assertEqual(result.error, "503 unavailable")
        self.assertEqual(post.call_count, 1)  # the second batch is not tried
        self.assertEqual(self.waiting(), 2)
        self.assertEqual(self.rejected(), [])

    def test_uploaded_instruction_files_are_counted(self):
        self.configure()
        self.spool.append({"kind": "instruction_version", "files": [
            {"path": "a", "sha256": "x", "content": "t"}, {"path": "b", "sha256": "y", "content": None}]})
        with mock.patch.object(faf_post, "post_batch", return_value=(200, {"accepted": 1, "rejected": []})):
            self.assertEqual(faf_post.flush(self.root, retries=0).uploaded_files, 1)

    def test_requeue_moves_rejected_events_back(self):
        self.configure()
        self.spool.append(step(1))
        with mock.patch.object(faf_post, "post_batch", return_value=(422, "no")):
            faf_post.flush(self.root, retries=0)
        self.assertEqual(faf_post.requeue_rejected(self.root), 1)
        self.assertEqual(self.rejected(), [])
        self.assertEqual(self.waiting(), 1)

    def test_chunks_respect_byte_limit(self):
        big = {"kind": "instruction_version", "files": [{"content": "x" * 1_500_000}]}
        chunks = list(faf_post._chunks([big, big, big, step(1)]))
        self.assertEqual([(len(c), too_large) for c, too_large in chunks], [(2, False), (2, False)])
        huge = {"content": "x" * (faf_post.MAX_BATCH_BYTES + 1)}
        self.assertEqual([t for _, t in faf_post._chunks([step(1), huge])], [False, True])

    def test_reading_settings_creates_nothing(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        self.assertFalse(faf_post.is_configured(other))
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((self.home / ".faf").exists())

    def test_post_batch_sends_key_and_version_headers(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"accepted":1}'
        with mock.patch.object(faf_post._OPENER, "open", return_value=response) as urlopen:
            self.assertEqual(faf_post.post_batch([step(1)], "https://example.test", KEY), (200, {"accepted": 1}))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertEqual(req.get_header("User-agent"), f"faf-collector/{faf_settings.COLLECTOR_VERSION}")
        self.assertEqual(req.get_header("X-faf-collector"), faf_settings.COLLECTOR_VERSION)
        self.assertIsNone(req.get_header("X-functions-key"))

    def loopback_server(self, status=200, body=b'{"accepted": 1}', location=None):
        """A local ingest stand-in: (server, hits) where hits lists (method, key header) of every request."""
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                hits.append((self.command, self.headers.get("x-api-key")))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, hits

    def test_redirect_is_not_followed_and_batch_is_kept(self):
        other, other_hits = self.loopback_server()
        start, start_hits = self.loopback_server(
            status=302, location=f"http://127.0.0.1:{other.server_address[1]}/other-host")
        self.configure(endpoint=f"http://127.0.0.1:{start.server_address[1]}/api/ingest")
        self.spool.append(step(1))
        result = faf_post.flush(self.root, retries=0)
        self.assertFalse(result)
        self.assertEqual((result.error, result.status), ("302 redirect refused", 302))
        self.assertEqual(start_hits, [("POST", KEY)])
        self.assertEqual(other_hits, [])
        self.assertEqual(self.waiting(), 1)
        self.assertEqual(self.rejected(), [])

    def test_200_with_html_body_keeps_batch(self):
        server, hits = self.loopback_server(body=b"<html>parked domain</html>")
        self.configure(endpoint=f"http://127.0.0.1:{server.server_address[1]}/api/ingest")
        self.spool.append(step(1))
        result = faf_post.flush(self.root, retries=0)
        self.assertFalse(result)
        self.assertEqual(result.error, "200 not an ingest answer")
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.waiting(), 1)
        self.assertEqual(self.rejected(), [])

    def report_lines(self, result):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            faf_log.setup(None, console=True)
            try:
                faf_post.report(result, "https://example.test/api/ingest")
            finally:
                faf_log.close()
        return out.getvalue().splitlines()

    def test_report_lines_follow_the_logging_table(self):
        R = faf_post.FlushResult
        self.assertEqual(self.report_lines(R(sent=2, status=200, duration=0.62)),
                         ["[faf] sent 2 events to example.test (200, 0.6 s)"])
        self.assertEqual(self.report_lines(R(sent=1, status=200, uploaded_files=4)),
                         ["[faf] sent 1 events to example.test (200, 0.0 s)", "[faf] uploaded instructions (4 files)"])
        self.assertEqual(self.report_lines(R()), ["[faf] nothing to send"])
        self.assertEqual(self.report_lines(R(error="no API key")), ["[faf] not recording: no API key"])
        self.assertEqual(self.report_lines(R(error="401 key not accepted", status=401, queued=3)),
                         ["[faf] send failed (401 key not accepted), 3 events queued"])
        self.assertEqual(self.report_lines(R(sent=1, status=200, rejected=2))[-1],
                         "[faf] 2 events rejected, kept in .faf/local/rejected.jsonl")

    def test_crash_line_has_type_and_place_only(self):
        try:
            raise ValueError("secret-detail /home/me/repo")
        except ValueError as exc:
            line = faf_log.crash_line(exc)
        self.assertRegex(line, r"^error ValueError at test_configuration\.py:\d+$")


if __name__ == "__main__":
    unittest.main()
