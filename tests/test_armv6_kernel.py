import hashlib
import io
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout import armv6_kernel
from ios_timeout.core import ResetError


def fixture():
    # Synthetic firmware avoids redistributing Apple's kernel. Its digest is
    # registered only inside individual tests; production pins Apple's image.
    header = struct.pack("<7I", 0xFEEDFACE, 12, 6, 2, 0, 0, 1)
    return (
        header + b"\xA5" * 32
        + (b"\xA5" * 32).join(before for _, before, _ in armv6_kernel.PATCHES)
        + b"\xA5" * 32
    )


class Armv6KernelTests(unittest.TestCase):
    def accepted_fixture(self, data):
        return patch.object(armv6_kernel, "KERNEL_SHA256", hashlib.sha256(data).hexdigest())

    def test_all_changes_preserve_length_and_original(self):
        data = bytearray(fixture())
        original = bytes(data)
        expected = original
        for _, before, after in armv6_kernel.PATCHES:
            expected = expected.replace(before, after)
        with self.accepted_fixture(data):
            result = armv6_kernel.patch_kernel(data)
        self.assertEqual(result, expected)
        self.assertEqual(bytes(data), original)
        self.assertEqual(len(result), len(original))

    def test_unknown_or_already_modified_kernel_is_rejected(self):
        data = fixture()
        with self.assertRaisesRegex(ResetError, "original"):
            armv6_kernel.patch_kernel(data)
        with self.accepted_fixture(data):
            patched = armv6_kernel.patch_kernel(data)
            with self.assertRaisesRegex(ResetError, "original"):
                armv6_kernel.patch_kernel(patched)

    def test_wrong_architecture_or_container_is_rejected(self):
        for data in (b"", b"x" * 32, struct.pack("<7I", 0xFEEDFACE, 12, 9, 2, 0, 0, 0)):
            with self.accepted_fixture(data), self.assertRaises(ResetError):
                armv6_kernel.patch_kernel(data)

    def test_missing_or_duplicate_pattern_is_rejected_before_modification(self):
        original = fixture()
        before = armv6_kernel.PATCHES[-1][1]
        for data in (original.replace(before, b"\xAA" * len(before)), original + before):
            mutable = bytearray(data)
            with self.accepted_fixture(data), self.assertRaisesRegex(ResetError, "exactly once"):
                armv6_kernel.patch_kernel(mutable)
            self.assertEqual(mutable, data)

    def test_overlapping_patterns_are_rejected(self):
        data = fixture()
        overlapping = armv6_kernel.PATCHES + (("overlap", b"nand-disable", b"x" * 12),)
        with self.accepted_fixture(data), patch.object(armv6_kernel, "PATCHES", overlapping):
            with self.assertRaisesRegex(ResetError, "overlap"):
                armv6_kernel.patch_kernel(data)

    def test_cli_writes_verified_output_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "kernel", Path(folder) / "patched"
            data = fixture()
            source.write_bytes(data)
            with self.accepted_fixture(data), redirect_stdout(io.StringIO()):
                self.assertEqual(armv6_kernel.main([str(source), str(output)]), 0)
                self.assertEqual(output.read_bytes(), armv6_kernel.patch_kernel(data))
            self.assertEqual(source.read_bytes(), data)

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "kernel", Path(folder) / "patched"
            source.write_bytes(fixture())
            output.write_bytes(b"previous")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(armv6_kernel.main([str(source), str(output)]), 1)
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["kernel", "patched"])

    def test_original_same_path_or_hard_link_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source, alias = Path(folder) / "kernel", Path(folder) / "alias"
            data = fixture()
            source.write_bytes(data)
            os.link(source, alias)
            with self.accepted_fixture(data), redirect_stderr(io.StringIO()):
                for destination in (source, alias):
                    self.assertEqual(armv6_kernel.main([str(source), str(destination)]), 1)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(alias.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
