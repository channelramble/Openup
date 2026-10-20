import struct
import tempfile
import unittest
from pathlib import Path

from macos_compatibility import MacOSCompatibilityError, validate_macos_bundle


X86_64 = 0x01000007
ARM64 = 0x0100000C


def version_number(version):
    return (version[0] << 16) | (version[1] << 8) | version[2]


def thin(cpu=X86_64, minimum=(12, 0, 0), endian="<", legacy=False,
         commands=None, filetype=6, wide=True):
    if commands is None:
        version = version_number(minimum)
        commands = [
            struct.pack(endian + "4I", 0x24, 16, version, version)
            if legacy else
            struct.pack(endian + "6I", 0x32, 24, 1, version, version, 0)
        ]
    header = [0xFEEDFACF if wide else 0xFEEDFACE, cpu, 3, filetype,
              len(commands), sum(map(len, commands)), 0]
    if wide:
        header.append(0)
    return struct.pack(endian + "%dI" % len(header), *header) + b"".join(commands)


def fat(slices, endian=">", wide=False):
    width = 32 if wide else 20
    offset = 8 + len(slices) * width
    table = []
    for cpu, content in slices:
        fields = [cpu, 3, offset, len(content), 0]
        if wide:
            fields.append(0)
        table.append(struct.pack(endian + ("IIQQII" if wide else "5I"), *fields))
        offset += len(content)
    return (
        struct.pack(endian + "2I", 0xCAFEBABF if wide else 0xCAFEBABE, len(slices))
        + b"".join(table) + b"".join(content for _, content in slices)
    )


def dylib_command(path, command=0xC):
    name = path.encode("utf-8") + b"\x00"
    padded_length = (24 + len(name) + 7) & ~7
    return struct.pack("<6I", command, padded_length, 24, 0, 0, 0) + (
        name.ljust(padded_length - 24, b"\x00")
    )


class MacOSCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, content, name="Contents/Frameworks/native.dylib"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def validate(self, architecture="x86_64", minimum=(12, 0, 0)):
        return validate_macos_bundle(self.root, architecture, minimum)

    def test_reports_maximum_across_nested_libraries_and_ignores_resources(self):
        self.write(thin(minimum=(10, 15, 0)))
        self.write(thin(minimum=(12, 0, 0)), "Contents/MacOS/launcher")
        self.write(b"not a native library", "Contents/Resources/icon.svg")
        self.write(b"!<arch>\narchive", "Contents/Resources/libstatic.a")
        report = self.validate()
        self.assertEqual(report["binary_count"], 2)
        self.assertEqual(report["maximum_deployment_target"], "12.0.0")

    def test_rejects_newer_library_despite_compatible_launcher(self):
        self.write(thin(minimum=(12, 0, 0)), "Contents/MacOS/launcher")
        self.write(thin(minimum=(13, 0, 0)))
        with self.assertRaisesRegex(
            MacOSCompatibilityError, r"native.dylib: requires macOS 13.0.0"
        ):
            self.validate()

    def test_compares_minor_and_patch_versions(self):
        for minimum in ((12, 1, 0), (12, 0, 1), (13, 0, 0)):
            with self.subTest(minimum=minimum):
                self.write(thin(minimum=minimum))
                with self.assertRaisesRegex(MacOSCompatibilityError, "above advertised"):
                    self.validate()

    def test_fat_and_fat64_both_endians_ignore_other_architecture_minimum(self):
        for wide in (False, True):
            for endian in ("<", ">"):
                with self.subTest(wide=wide, endian=endian):
                    self.write(fat([
                        (ARM64, thin(ARM64, (14, 0, 0))),
                        (X86_64, thin(X86_64, (11, 0, 0))),
                    ], endian=endian, wide=wide))
                    self.assertEqual(
                        self.validate()["maximum_deployment_target"], "11.0.0"
                    )
                    self.assertEqual(
                        self.validate("arm64", (14, 0, 0))["maximum_deployment_target"],
                        "14.0.0",
                    )

    def test_legacy_version_and_both_thin_endians(self):
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                self.write(thin(minimum=(10, 9, 0), endian=endian, legacy=True))
                self.assertEqual(
                    self.validate()["maximum_deployment_target"], "10.9.0"
                )

    def test_missing_thin_or_fat_architecture_rejected(self):
        for data in (
            thin(ARM64),
            fat([(ARM64, thin(ARM64))]),
            thin(cpu=7, wide=False),
            thin(cpu=7, wide=False, endian=">"),
        ):
            with self.subTest(data=data[:8]):
                self.write(data)
                with self.assertRaisesRegex(MacOSCompatibilityError, "missing requested"):
                    self.validate()

    def test_loadable_image_must_declare_deployment_version(self):
        for data in (thin(commands=[]), thin(minimum=(0, 0, 0))):
            self.write(data)
            with self.assertRaisesRegex(MacOSCompatibilityError, "no macOS deployment"):
                self.validate()

    def test_nonloadable_object_and_universal_static_archive_are_ignored(self):
        self.write(thin(filetype=1, commands=[]), "object.o")
        self.write(fat([(ARM64, b"!<arch>\ncontent")]), "universal.a")
        self.write(thin(), "launcher")
        self.assertEqual(self.validate()["binary_count"], 1)

    def test_truncated_headers_and_tables_rejected(self):
        original = thin()
        invalid = [
            original[:4], original[:20], original[:-1],
            fat([(X86_64, original)])[:7],
            fat([(X86_64, original)])[:20],
            fat([(X86_64, original)])[:-1],
        ]
        for data in invalid:
            with self.subTest(length=len(data)):
                self.write(data)
                with self.assertRaises(MacOSCompatibilityError):
                    self.validate()

    def test_fat_record_cannot_claim_different_inner_architecture(self):
        self.write(fat([(X86_64, thin(ARM64))]))
        with self.assertRaisesRegex(MacOSCompatibilityError, "missing requested"):
            self.validate()

    def test_invalid_load_command_metadata_rejected(self):
        commands = [
            struct.pack("<2I", 0x32, 8),
            struct.pack("<6I", 0x32, 24, 1, 0xC0000, 0, 1),
            struct.pack("<6I", 0x32, 24, 2, 0xC0000, 0, 0),
            struct.pack("<2I", 0x32, 0),
        ]
        for command in commands:
            with self.subTest(command=command):
                self.write(thin(commands=[command]))
                with self.assertRaises(MacOSCompatibilityError):
                    self.validate()

    def test_stricter_of_multiple_version_commands_is_used(self):
        old = struct.pack("<4I", 0x24, 16, 0xB0000, 0xB0000)
        new = struct.pack("<6I", 0x32, 24, 1, 0xD0000, 0xD0000, 0)
        self.write(thin(commands=[old, new]))
        with self.assertRaisesRegex(MacOSCompatibilityError, "requires macOS 13.0.0"):
            self.validate()

    def test_host_dylib_dependencies_rejected_for_all_load_commands(self):
        for command in (0xC, 0x80000018, 0x8000001F, 0x80000023, 0x20):
            for path in (
                "/opt/homebrew/lib/libssl.dylib",
                "/usr/local/lib/libssl.dylib",
                "/Users/builder/dependency.dylib",
                "libssl.dylib",
                "/usr/lib/../local/lib/libssl.dylib",
            ):
                with self.subTest(command=command, path=path):
                    self.write(thin(commands=[
                        struct.pack("<4I", 0x24, 16, 0xC0000, 0xC0000),
                        dylib_command(path, command),
                    ]))
                    with self.assertRaisesRegex(
                        MacOSCompatibilityError, "nonportable dylib dependency"
                    ):
                        self.validate()

    def test_system_relative_dylibs_and_install_id_are_allowed(self):
        commands = [
            struct.pack("<4I", 0x24, 16, 0xC0000, 0xC0000),
            dylib_command("/usr/lib/libSystem.B.dylib"),
            dylib_command("/System/Library/Frameworks/AppKit.framework/AppKit"),
            dylib_command("@rpath/libssl.dylib"),
            dylib_command("@loader_path/../lib/libssl.dylib"),
            dylib_command("@executable_path/../lib/libssl.dylib"),
            # Install IDs identify the library and are not loaded dependencies.
            dylib_command("/opt/homebrew/lib/libidentity.dylib", command=0xD),
        ]
        self.write(thin(commands=commands))
        self.assertEqual(self.validate()["binary_count"], 1)

    def test_empty_bundle_is_not_reported_as_compatible(self):
        self.write(b"resource")
        with self.assertRaisesRegex(MacOSCompatibilityError, "no loadable Mach-O"):
            self.validate()

    def test_symlinks_are_not_followed(self):
        self.write(thin(), "launcher")
        (self.root / "alias").symlink_to(self.root / "launcher")
        (self.root / "loop").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.validate()["binary_count"], 1)


if __name__ == "__main__":
    unittest.main()
