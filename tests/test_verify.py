import contextlib
import hashlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import faf_verify  # noqa: E402
from support import ed25519_sign, release_test_key  # noqa: E402

NAME = "faf-collector-v9.0.0.zip"


def signed(data, name=NAME):
    checksum = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    return checksum, ed25519_sign(checksum)


def reason(*args):
    return faf_verify.check_release(*args) or ""


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        cases = [  # (public key, message, signature): RFC 8032 section 7.1, tests 1 and 2
            ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
             "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
             "d25bf5f0595bbe24655141438e7a100b"),
            ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
             "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
             "387b2eaeb4302aeeb00d291612bb0c00"),
        ]
        for public, message, signature in cases:
            public, message, signature = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
            self.assertTrue(faf_verify.ed25519_verify(public, message, signature))
            self.assertFalse(faf_verify.ed25519_verify(public, message + b"x", signature))
            bad = bytearray(signature)
            bad[0] ^= 1
            self.assertFalse(faf_verify.ed25519_verify(public, message, bytes(bad)))

    def test_signature_with_s_out_of_range_is_refused(self):
        sig = ed25519_sign(b"m")
        s = int.from_bytes(sig[32:], "little") + faf_verify._L
        public = faf_verify.public_key_bytes(release_test_key())
        self.assertTrue(faf_verify.ed25519_verify(public, b"m", sig))
        self.assertFalse(faf_verify.ed25519_verify(public, b"m", sig[:32] + s.to_bytes(32, "little")))

    def test_built_in_key_is_an_ed25519_key(self):
        self.assertEqual(len(faf_verify.public_key_bytes()), 32)

    def test_base_point(self):
        self.assertEqual(faf_verify._recover_x(faf_verify._GY, 0), faf_verify._GX)


class CheckReleaseTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(faf_verify, "PUBLIC_KEY", release_test_key())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_signed_release_passes(self):
        self.assertIsNone(faf_verify.check_release(NAME, b"zip", *signed(b"zip")))

    def test_changed_zip_with_matching_new_checksum_is_refused(self):
        _, signature = signed(b"zip")
        checksum = f"{hashlib.sha256(b'evil').hexdigest()}  {NAME}\n".encode()
        self.assertIn("not valid", reason(NAME, b"evil", checksum, signature))

    def test_changed_zip_under_signed_checksum_is_refused(self):
        self.assertIn("checksum mismatch", reason(NAME, b"evil", *signed(b"zip")))

    def test_missing_or_wrong_signature_is_refused(self):
        checksum, signature = signed(b"zip")
        self.assertIn("no signature", reason(NAME, b"zip", checksum, b""))
        self.assertIn("not valid", reason(NAME, b"zip", checksum, signature[::-1]))
        other = ed25519_sign(checksum, seed=b"\x01" * 32)
        self.assertIn("not valid", reason(NAME, b"zip", checksum, other))

    def test_signed_checksum_of_another_release_is_refused(self):
        old = "faf-collector-v1.0.0.zip"
        self.assertIn("not for", reason(NAME, b"zip", *signed(b"zip", old)))

    def test_command_line(self):
        checksum, signature = signed(b"zip")
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / NAME, Path(temp) / f"{NAME}.sha256", Path(temp) / f"{NAME}.sha256.sig"]
            for path, content in zip(paths, (b"zip", checksum, signature)):
                path.write_bytes(content)
            self.assertEqual(faf_verify.main([str(p) for p in paths]), 0)
            paths[0].write_bytes(b"evil")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(faf_verify.main([str(p) for p in paths]), 1)
            self.assertIn("checksum mismatch", err.getvalue())


class InstallersTest(unittest.TestCase):
    def test_installers_embed_faf_verify_verbatim(self):
        source = (COLLECTOR / "faf_verify.py").read_text(encoding="utf-8")
        for name, pattern in (("install.sh", r"<<'FAF_VERIFY'\n(.*?)FAF_VERIFY\n"),
                              ("install.ps1", r"\$fafVerify = @'\n(.*?)'@\n")):
            with self.subTest(installer=name):
                text = (COLLECTOR / name).read_text(encoding="utf-8").replace("\r\n", "\n")
                found = re.search(pattern, text, re.S)
                if found is None:
                    self.fail(f"{name} does not embed faf_verify.py")
                self.assertEqual(found.group(1), source.replace("\r\n", "\n"),
                                 f"copy faf_verify.py into {name} again")


if __name__ == "__main__":
    unittest.main()
