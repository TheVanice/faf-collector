"""The update notice: the daily release lookup, the cache, and the synchronous --notice output."""
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_hook  # noqa: E402
import faf_update  # noqa: E402
from support import temp_home  # noqa: E402

NOW = 1_800_000_000.0
REAL_LOOKUP = faf_update._fetch_latest_tag  # before temp_home stubs it
DAY = faf_update.CHECK_INTERVAL_SECONDS


class Versions(unittest.TestCase):
    def test_is_newer_compares_numbers_not_text(self):
        self.assertTrue(faf_update.is_newer("v1.10.0", "1.9.0"))
        self.assertTrue(faf_update.is_newer("1.0.1", "1.0.0"))
        self.assertFalse(faf_update.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(faf_update.is_newer("0.9.0", "1.0.0"))
        self.assertFalse(faf_update.is_newer("latest", "1.0.0"))  # not a version: no notice


class Cache(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        patcher = mock.patch.object(faf_update, "_fetch_latest_tag", return_value="1.1.0")
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_lookup_is_cached_for_a_day(self):
        self.assertEqual(faf_update.latest_known(NOW), "1.1.0")
        self.assertEqual(faf_update.latest_known(NOW + DAY - 1), "1.1.0")
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(faf_update.latest_known(NOW + DAY + 1), "1.1.0")
        self.assertEqual(self.fetch.call_count, 2)

    def test_failed_lookup_is_not_repeated_every_session(self):
        self.fetch.return_value = None
        self.assertIsNone(faf_update.latest_known(NOW))
        self.assertIsNone(faf_update.latest_known(NOW + 60))
        self.assertEqual(self.fetch.call_count, 1)

    def test_read_only_never_touches_the_network(self):
        self.assertIsNone(faf_update.latest_known(NOW, refresh=False))  # no cache yet
        self.assertEqual(self.fetch.call_count, 0)
        faf_update.latest_known(NOW)
        self.assertEqual(faf_update.latest_known(NOW + 10 * DAY, refresh=False), "1.1.0")  # stale, still read
        self.assertEqual(self.fetch.call_count, 1)

    def test_notice_only_for_a_newer_release(self):
        current = faf_update.faf_settings.COLLECTOR_VERSION
        self.assertIsNone(faf_update.notice(now=NOW))  # nothing cached
        self.fetch.return_value = current
        faf_update.latest_known(NOW + 2 * DAY)
        self.assertIsNone(faf_update.notice(now=NOW + 2 * DAY))
        self.fetch.return_value = "99.0.0"
        faf_update.latest_known(NOW + 4 * DAY)
        text = faf_update.notice(now=NOW + 4 * DAY)
        self.assertIn("99.0.0 is available", text)
        self.assertIn("faf upgrade", text)

class Redirect(unittest.TestCase):
    def test_release_tag_is_read_from_the_redirect(self):
        # The real lookup, not the stub temp_home installs: the tag is the last path part of the redirect.
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://github.com/TheVanice/faf-collector/releases/tag/v2.3.4"
        with mock.patch.object(faf_update, "urlopen", return_value=response):
            self.assertEqual(REAL_LOOKUP(), "2.3.4")

    def test_unreachable_release_page_gives_no_version(self):
        with mock.patch.object(faf_update, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(REAL_LOOKUP())


class Hook(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.repo = Path(self.home) / "repo"
        (self.repo / ".faf").mkdir(parents=True)
        cache = faf_update.cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"checked": time.time(), "latest": "99.0.0"}), encoding="utf-8")

    def notice(self, payload):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = faf_hook.main(["--provider", "claude-code", "--notice"], stdin=io.StringIO(payload))
        self.assertEqual(code, 0)
        return out.getvalue()

    def payload(self):
        return json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(self.repo),
                           "transcript_path": str(self.repo / "t.jsonl")})

    def test_notice_prints_only_the_system_message(self):
        out = self.notice(self.payload())
        self.assertEqual(json.loads(out)["systemMessage"], faf_update.notice())
        self.assertIn("99.0.0", json.loads(out)["systemMessage"])

    def test_notice_is_silent_for_copilot_payloads(self):
        self.assertEqual(self.notice(json.dumps({"sessionId": "x", "cwd": str(self.repo)})), "")

    def test_notice_is_silent_when_up_to_date(self):
        faf_update.cache_path().write_text(json.dumps({"checked": time.time(), "latest": "0.0.1"}),
                                           encoding="utf-8")
        self.assertEqual(self.notice(self.payload()), "")


if __name__ == "__main__":
    unittest.main()
