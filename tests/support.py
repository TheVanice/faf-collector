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


# A test release key (RFC 8032, test 1). faf_verify only verifies, so the tests sign here.
TEST_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def _compress(point):
    import faf_verify as v
    zinv = pow(point[2], v._P - 2, v._P)
    x, y = point[0] * zinv % v._P, point[1] * zinv % v._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def release_test_key(seed=TEST_SEED):
    """The key as faf_verify.PUBLIC_KEY holds it (base64 of the SubjectPublicKeyInfo)."""
    import base64
    import hashlib

    import faf_verify as v
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    return base64.b64encode(v._SPKI_PREFIX + _compress(v._mul(a, v._G))).decode("ascii")


def ed25519_sign(message, seed=TEST_SEED):
    import hashlib

    import faf_verify as v
    h = hashlib.sha512(seed).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    public = _compress(v._mul(a, v._G))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % v._L
    big_r = _compress(v._mul(r, v._G))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % v._L
    return big_r + ((r + k * a) % v._L).to_bytes(32, "little")
