import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from ios_timeout.cli import parser
from ios_timeout.core import ResetError
from ios_timeout.desktop_model import Settings, TerminalText, operation_arguments
from ios_timeout import desktop_updates as updates
from ios_timeout import legacy_names


def desktop_zip(path, version="2.1.0", target="linux-x86_64", extra=None, legacy=False):
    prefix = updates.code_prefix(target, legacy)
    entries = {
        prefix + "desktop.py": b"# test fixture, never executed\n",
        prefix + "ios_timeout/gui.py": b"# GUI fixture\n",
        updates.python_relative(target, legacy=legacy): b"# runtime fixture\n",
    }
    for required in updates.required_files(target, legacy):
        entries.setdefault(required, b"# native launcher fixture\n")
    entries.update(extra or {})
    import hashlib
    manifest = {
        "schema": 1, "version": version, "target": target, "revision": "b" * 40, "dirty": False,
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(),
                         "mode": 0o755 if name.endswith("python3") else 0o644} for name, value in entries.items()},
    }
    entries["DESKTOP.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            name_root = legacy_names.SLUG if legacy else updates.bundle_name(target)
            info = zipfile.ZipInfo(name_root + "/" + name)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return updates.file_hash(path)


class DesktopPlanningTests(unittest.TestCase):
    def test_desktop_entry_point_prevents_bytecode_in_backend_helper_children(self):
        import importlib.util
        import ios_timeout
        from ios_timeout import cli

        entry_path = Path(ios_timeout.__file__).resolve().parent.parent / "desktop.py"
        spec = importlib.util.spec_from_file_location("desktop_under_test", entry_path)
        desktop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(desktop)
        with tempfile.TemporaryDirectory(prefix="backend helper with spaces ") as directory:
            root = Path(directory)
            (root / "fixture_import.py").write_text("VALUE = 'imported'\n", encoding="utf-8")
            (root / "fixture_helper.py").write_text(
                "import json, sys\n"
                "import fixture_import\n"
                "print(json.dumps({'no_bytecode': sys.dont_write_bytecode, "
                "'value': fixture_import.VALUE}))\n", encoding="utf-8",
            )
            children = []

            def launch_helper(arguments):
                self.assertEqual(arguments, ["fixture-operation"])
                # Match the shell helpers' unisolated `python -m` invocation:
                # no -B is passed, so only the entry point's inherited setting
                # can protect imports inside a signed application bundle.
                child = subprocess.run(
                    [sys.executable, "-m", "fixture_helper"], cwd=root,
                    capture_output=True, text=True, timeout=15, check=True,
                )
                children.append(json.loads(child.stdout))
                return 0

            with patch.dict(os.environ), \
                 patch.object(sys, "argv", ["desktop.py", "--cli", "fixture-operation"]), \
                 patch.object(sys, "path", list(sys.path)), \
                 patch.object(sys, "dont_write_bytecode", False), \
                 patch.object(cli, "main", side_effect=launch_helper):
                os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
                # The test interpreter is already running with its libraries
                # loaded, including when tests target a Linux desktop bundle.
                os.environ["BL_IOS_LIBRARIES_ROOT"] = str(entry_path.parent)
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(children, [{"no_bytecode": True, "value": "imported"}])
            self.assertEqual(list(root.rglob("__pycache__")), [])
            self.assertEqual(list(root.rglob("*.pyc")), [])

    def test_gui_arguments_parse_with_spaces_and_shell_characters(self):
        settings = Settings(udid="abcdef123456", ipsw="/tmp/a ' $(touch bad).ipsw")
        args = operation_arguments("prepare", settings)
        parsed = parser().parse_args(args)
        self.assertEqual(args[args.index("--ipsw") + 1], settings.ipsw)
        self.assertEqual(parsed.ipsw, Path(settings.ipsw))
        self.assertEqual(parsed.model, "iPhone1,2")
        self.assertFalse(parsed.apply)

    def test_recovery_uses_bundled_armv6_for_3g_and_rejects_damaged_bundle(self):
        args = operation_arguments("recover", Settings(), apply=True)
        self.assertIn("--apply", args)
        self.assertNotIn("--armv6-tools", args)
        self.assertEqual(parser().parse_args(args).model, "iPhone1,2")
        for problem in ("missing", "corrupt"):
            with self.subTest(problem=problem), patch(
                "ios_timeout.armv6_payloads.read_bundled_payloads",
                side_effect=ResetError(problem),
            ):
                with self.assertRaisesRegex(ResetError, problem):
                    operation_arguments("recover", Settings(), apply=True)
        args = operation_arguments("recover", Settings(), install_only=True)
        self.assertIn("--install-only", args)
        self.assertNotIn("--apply", args)

    def test_inspection_cannot_accidentally_apply_or_reboot(self):
        parsed = parser().parse_args(operation_arguments("inspect", Settings(reboot=True), apply=True))
        self.assertFalse(hasattr(parsed, "apply"))
        self.assertFalse(hasattr(parsed, "reboot"))

    def test_restore_keeps_backup_separate_from_flags(self):
        with tempfile.TemporaryDirectory(prefix="backup with spaces ") as folder:
            args = operation_arguments("restore", Settings(), apply=True, backup=folder)
            self.assertEqual(parser().parse_args(args).backup, Path(folder).absolute())
        with self.assertRaises(ResetError):
            operation_arguments("restore", Settings(), apply=True)

    def test_terminal_controls_split_between_chunks(self):
        sanitizer = TerminalText()
        self.assertEqual(sanitizer.feed("a\x1b[3"), "a")
        self.assertEqual(sanitizer.feed("1mred\x1b[0m\n"), "red\n")
        self.assertEqual(sanitizer.feed("\x1b]52;c;secret"), "")
        self.assertEqual(sanitizer.feed("\x07plain"), "plain")
        self.assertEqual(sanitizer.feed("<b>plain text</b>"), "<b>plain text</b>")

    def test_wsl_runtime_avoids_python_install_probe(self):
        from ios_timeout import wsl
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "wsl-runtime/bin").mkdir(parents=True)
            (root / "wsl-runtime/bin/python3").write_bytes(b"fixture")
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="/mnt/c/app/path\n")
            args = parser().parse_args(["prepare", "--model", "iPhone1,2"])
            with patch.dict(os.environ, {"BL_IOS_DESKTOP": "1", "BL_IOS_APP_ROOT": str(root)}), \
                 patch.object(wsl.shutil, "which", return_value="wsl.exe"), \
                 patch.object(wsl.subprocess, "run", side_effect=run):
                self.assertEqual(wsl.run_in_wsl(["prepare", "--model", "iPhone1,2"], args), 0)
            self.assertFalse(any("-c" in command for command in calls))
            self.assertIn("--cli", calls[-1])


class DesktopUpdateTests(unittest.TestCase):
    def test_existing_installation_remains_available_for_explicit_rollback(self):
        for target in sorted(updates.TARGETS):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                archive = state / "legacy.zip"
                digest = desktop_zip(archive, version="2.2.2", target=target, legacy=True)
                old_root = state / "desktop-updates/versions" / ("2.2.2-" + target) / legacy_names.SLUG
                updates.extract_archive(archive, old_root, "2.2.2", target, digest)
                manifest = updates.verify_bundle(old_root, target, "2.2.2")
                self.assertTrue(updates.legacy_layout(manifest, target))
                self.assertEqual(updates.installed_root(state, "2.2.2", target), old_root)
                pointer = state / "desktop-updates/active.json"
                pointer.write_text(json.dumps({"current": "2.2.2", "previous": None}))
                with patch.object(updates, "state_directory", return_value=state), \
                     patch.object(updates, "target", return_value=target), \
                     patch.object(updates.subprocess, "run") as launch:
                    # Opening a new Openup must not silently run the older app.
                    self.assertIsNone(updates.dispatch(state / "fresh-app", []))
                    launch.assert_not_called()
                    pointer.write_text(json.dumps({"current": "2.3.0", "previous": "2.2.2"}))
                    self.assertTrue(updates.rollback(state))
                    launch.return_value.returncode = 0
                    self.assertEqual(updates.dispatch(state / "fresh-app", []), 0)
                    expected = old_root / updates.python_relative(target, windowed=True, legacy=True)
                    self.assertEqual(launch.call_args.args[0][0], str(expected))
                    # An explicit rollback persists on the next ordinary launch.
                    self.assertEqual(updates.dispatch(state / "fresh-app", []), 0)

    def downloaded_fixture(self, root, target="linux-x86_64", version="99.0.0"):
        archive = root / f"Openup-{version}-{target}.zip"
        digest = desktop_zip(archive, version=version, target=target)
        checksum = Path(str(archive) + ".sha256")
        checksum.write_text(f"{digest}  {archive.name}\n")
        return archive, checksum

    def test_downloaded_update_installs_without_network_on_each_target_and_rolls_back(self):
        for target in sorted(updates.TARGETS):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, checksum = self.downloaded_fixture(root, target)
                with patch.object(updates, "target", return_value=target), patch.object(updates, "open_github") as network:
                    result = updates.install_downloaded(archive, checksum, state=root / "state")
                network.assert_not_called()
                self.assertEqual(result["version"], "99.0.0")
                updates.verify_bundle(result["path"], target, "99.0.0")
                self.assertEqual(updates.read_pointer(root / "state")["current"], "99.0.0")
                self.assertTrue(updates.rollback(root / "state"))
                self.assertIsNone(updates.read_pointer(root / "state")["current"])

    def test_downloaded_update_rejects_corruption_wrong_platform_and_old_release(self):
        for problem in ("corrupt", "platform", "old", "checksum-name", "checksum-format"):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, checksum = self.downloaded_fixture(root, version="1.0.0" if problem == "old" else "99.0.0")
                if problem == "corrupt":
                    with archive.open("ab") as output:
                        output.write(b"changed after download")
                if problem == "checksum-name":
                    checksum.write_text(checksum.read_text().replace(archive.name, "other.zip"))
                if problem == "checksum-format":
                    checksum.write_text("not a checksum")
                target = "windows-x86_64" if problem == "platform" else "linux-x86_64"
                with patch.object(updates, "target", return_value=target), self.assertRaises(ResetError):
                    updates.install_downloaded(archive, checksum, state=root / "state")
                self.assertIsNone(updates.read_pointer(root / "state")["current"])

    def test_downloaded_archive_changed_after_inspection_cannot_activate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, checksum = self.downloaded_fixture(root)
            original = updates.downloaded_release

            def inspect_then_change(*args, **kwargs):
                release = original(*args, **kwargs)
                data = bytearray(archive.read_bytes())
                data[50] ^= 1
                archive.write_bytes(data)
                return release

            with patch.object(updates, "target", return_value="linux-x86_64"), \
                 patch.object(updates, "downloaded_release", side_effect=inspect_then_change), \
                 self.assertRaisesRegex(ResetError, "SHA-256"):
                updates.install_downloaded(archive, checksum, state=root / "state")
            self.assertIsNone(updates.read_pointer(root / "state")["current"])

    def test_cancelled_downloaded_install_keeps_active_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, checksum = self.downloaded_fixture(root)

            def cancel(message):
                if message.startswith("Activating"):
                    raise ResetError("cancelled")

            with patch.object(updates, "target", return_value="linux-x86_64"), self.assertRaisesRegex(ResetError, "cancelled"):
                updates.install_downloaded(archive, checksum, state=root / "state", progress=cancel)
            self.assertIsNone(updates.read_pointer(root / "state")["current"])

    def test_extract_and_verify_full_runtime_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "update.zip"
            digest = desktop_zip(archive)
            updates.extract_archive(archive, root / "app", "2.1.0", "linux-x86_64", digest)
            manifest = updates.verify_bundle(root / "app", "linux-x86_64", "2.1.0")
            self.assertEqual(len(manifest["files"]), 5)
            if os.name != "nt":
                self.assertTrue((root / "app/runtime/bin/python3").stat().st_mode & 0o100)

    def test_mac_inventory_stays_outside_signed_application(self):
        for target in ("macos-arm64", "macos-x86_64"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = root / "update.zip"
                digest = desktop_zip(archive, target=target)
                updates.extract_archive(archive, root / "bundle", "2.1.0", target, digest)
                self.assertTrue((root / "bundle/DESKTOP.json").exists())
                app = root / "bundle/Openup.app"
                self.assertFalse((app / "DESKTOP.json").exists())
                source = app / "Contents/Resources/app"
                self.assertEqual(updates.find_bundle(source), root / "bundle")

    def test_intel_mac_update_selects_intel_package(self):
        releases = [{"tag_name": "v9.0.0", "assets": [
            {"name": f"Openup-9.0.0-{target}.zip", "id": index + 1,
             "size": 20, "digest": "sha256:" + "a" * 64}
            for index, target in enumerate(("macos-arm64", "macos-x86_64"))
        ]}]
        with patch.object(updates.sys, "platform", "darwin"), \
             patch.object(updates.platform, "machine", return_value="x86_64"), \
             patch.object(updates, "open_github", return_value=io.BytesIO(json.dumps(releases).encode())):
            release = updates.check()
        self.assertEqual(release["target"], "macos-x86_64")
        self.assertEqual(release["asset"]["id"], 2)

    def test_wrong_target_and_unsafe_paths_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "update.zip"
            digest = desktop_zip(archive)
            with self.assertRaises(ResetError):
                updates.extract_archive(archive, root / "app", "2.1.0", "windows-x86_64", digest)
            digest = desktop_zip(archive, extra={"../outside": b"bad"})
            with self.assertRaises(ResetError):
                updates.extract_archive(archive, root / "app", "2.1.0", "linux-x86_64", digest)
            self.assertFalse((root / "outside").exists())

    def test_install_and_rollback_dont_touch_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            archive = state / "download.zip"
            digest = desktop_zip(archive)
            release = {"version": "2.1.0", "target": "linux-x86_64",
                       "asset": {"id": 3, "size": archive.stat().st_size, "digest": "sha256:" + digest}}
            (state / "backups").mkdir()
            (state / "backups/keep").write_bytes(b"original")
            with patch.object(updates, "open_github", return_value=io.BytesIO(archive.read_bytes())):
                installed = updates.install(release, state=state)
            self.assertTrue(installed.is_dir())
            self.assertEqual(updates.read_pointer(state)["current"], "2.1.0")
            self.assertTrue(updates.rollback(state))
            self.assertIsNone(updates.read_pointer(state)["current"])
            self.assertEqual((state / "backups/keep").read_bytes(), b"original")

    def test_bad_digest_does_not_activate_update(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            archive = state / "download.zip"
            desktop_zip(archive)
            release = {"version": "2.1.0", "target": "linux-x86_64",
                       "asset": {"id": 3, "size": archive.stat().st_size, "digest": "sha256:" + "0" * 64}}
            with patch.object(updates, "open_github", return_value=io.BytesIO(archive.read_bytes())), self.assertRaises(ResetError):
                updates.install(release, state=state)
            self.assertIsNone(updates.read_pointer(state)["current"])

    def test_cancel_before_activation_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            archive = state / "download.zip"
            digest = desktop_zip(archive)
            release = {"version": "2.1.0", "target": "linux-x86_64",
                       "asset": {"id": 3, "size": archive.stat().st_size, "digest": "sha256:" + digest}}
            def progress(text):
                if text.startswith("Activating"):
                    raise ResetError("cancelled")
            with patch.object(updates, "open_github", return_value=io.BytesIO(archive.read_bytes())), self.assertRaises(ResetError):
                updates.install(release, state=state, progress=progress)
            self.assertIsNone(updates.read_pointer(state)["current"])
            installed = updates.install(release, state=state)
            self.assertTrue(installed.is_dir())

    def test_mutated_installed_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "update.zip"
            digest = desktop_zip(archive)
            updates.extract_archive(archive, root / "app", "2.1.0", "linux-x86_64", digest)
            (root / "app/runtime/bin/python3").write_bytes(b"altered")
            with self.assertRaises(ResetError):
                updates.verify_bundle(root / "app", "linux-x86_64", "2.1.0")

    def test_private_token_is_removed_on_asset_redirect(self):
        request = urllib.request.Request("https://api.github.com/repos/a/b/assets/3",
                                         headers={"Authorization": "Bearer test-token"})
        redirect = updates.SafeRedirect().redirect_request(
            request, None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/file")
        self.assertIsNone(redirect.get_header("Authorization"))
        with self.assertRaises(ResetError):
            updates.SafeRedirect().redirect_request(request, None, 302, "redirect", {}, "http://evil.invalid/file")

    def test_release_selection_requires_matching_platform_asset(self):
        releases = [{"tag_name": "v9.0.0", "assets": [{"name": "Openup-9.0.0-windows-x86_64.zip",
                     "id": 1, "size": 20, "digest": "sha256:" + "a" * 64}]}]
        with patch.object(updates, "open_github", return_value=io.BytesIO(json.dumps(releases).encode())):
            self.assertEqual(updates.check(platform_target="windows-x86_64")["version"], "9.0.0")
        with patch.object(updates, "open_github", return_value=io.BytesIO(json.dumps(releases).encode())), self.assertRaises(ResetError):
            updates.check(platform_target="linux-x86_64")
