"""Test helpers: a throwaway home folder, so ~/.faf/settings.json is never the real one."""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

KEY = "faf_" + "A" * 43
OTHER_KEY = "faf_" + "B" * 43


def temp_home(test, clear_env=False):
    """Point HOME and USERPROFILE at a new temp folder for the test; returns it.

    clear_env also removes every FAF_* variable for the duration of the test.
    """
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    home = Path(temp.name)
    patcher = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
    patcher.start()
    test.addCleanup(patcher.stop)
    # No test reaches the release page: the update check answers "unknown" unless a test says otherwise.
    import faf_update  # the calling test has put the collector folder on sys.path
    lookup = mock.patch.object(faf_update, "_fetch_latest_tag", return_value=None)
    lookup.start()
    test.addCleanup(lookup.stop)
    if clear_env:
        for name in [k for k in os.environ if k.startswith("FAF_")]:
            del os.environ[name]
    return home


def write_settings(home, data):
    path = Path(home) / ".faf" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def read_settings(home):
    return json.loads((Path(home) / ".faf" / "settings.json").read_text(encoding="utf-8"))
