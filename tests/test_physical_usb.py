import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ios_timeout import physical_usb
from ios_timeout.core import ResetError


def endpoint(product=0x1227, vendor=0x05AC, **extra):
    return {"USBDeviceKeyVendorID": vendor, "USBDeviceKeyProductID": product,
            "USBDeviceKeySerialNumber": "CPID:8900 BDID:00 ECID:0", **extra}


def inventory(nodes, kind="SPUSBHostDataType"):
    return json.dumps({kind: [{"_name": "controller", "_items": nodes}]}).encode()


class PhysicalMacInventoryTests(unittest.TestCase):
    def require(self, response):
        with patch.object(physical_usb.sys, "platform", "darwin"), \
                patch.object(physical_usb.usb_modes, "_query", return_value=response):
            return physical_usb.require_single_mobile_device()

    def test_every_mobile_mode_counts_without_relying_on_serial_or_model(self):
        for product in (0x1222, 0x1227, 0x1280, 0x1281, 0x1282, 0x1283,
                        0x1290, 0x12A8, 0x12AF, 0xF014):
            with self.subTest(product=product):
                result = self.require(inventory([endpoint(product, USBDeviceKeySerialNumber="")]))
                self.assertEqual(result["product_id"], product)
                self.assertEqual(result["location"], "tree:0/0")

    def test_second_phone_with_unknown_or_missing_identity_blocks_singleton(self):
        for serial in ("", "ECID:0", "CPID:FFFF BDID:FF", "CPID:8900 CPID:8900"):
            with self.subTest(serial=serial), self.assertRaisesRegex(ResetError, "found 2"):
                self.require(inventory([endpoint(), endpoint(0x12A8, USBDeviceKeySerialNumber=serial)]))

    def test_apple_peripherals_and_computer_restore_modes_do_not_count(self):
        nodes = [endpoint(), endpoint(0x0267), endpoint(0x8600), endpoint(0x1901),
                 endpoint(0x1227, vendor=0x1234)]
        self.assertEqual(self.require(inventory(nodes))["product_id"], 0x1227)

    def test_zero_phones_is_refused(self):
        with self.assertRaisesRegex(ResetError, "found 0"):
            self.require(inventory([]))

    def test_invalid_or_incomplete_inventory_never_becomes_singleton(self):
        malformed = [b"{}", b"{", b"x" * (physical_usb.usb_modes.MAX_OUTPUT + 1),
                     inventory([endpoint(), {"USBDeviceKeyVendorID": 0x05AC}]),
                     inventory([endpoint(), {"USBDeviceKeySerialNumber": ""}]),
                     inventory([endpoint(), {"_name": "iPhone"}]),
                     inventory([endpoint(), {"_items": {}}]),
                     inventory([endpoint(), "missing endpoint"]),
                     inventory([endpoint(vendor_id=0x1234)])]
        for payload in malformed:
            with self.subTest(payload=payload[:30]), self.assertRaises(ResetError):
                self.require(payload)

    def test_old_profiler_fallback_shares_deadline(self):
        old = {"vendor_id": "0x05ac (Apple Inc.)", "product_id": "0x1222",
               "location_id": "0x12300000"}
        with patch.object(physical_usb.sys, "platform", "darwin"), \
                patch.object(physical_usb.time, "monotonic", return_value=100), \
                patch.object(physical_usb.usb_modes, "_query", side_effect=[None, inventory([old], "SPUSBDataType")]) as query:
            result = physical_usb.require_single_mobile_device(deadline=101)
        self.assertEqual(result, {"product_id": 0x1222, "location": "0x12300000"})
        self.assertEqual([call.args for call in query.call_args_list],
                         [("SPUSBHostDataType", 1), ("SPUSBDataType", 1)])

    def test_reply_after_deadline_cannot_authorize_singleton(self):
        now = [100]
        def late_reply(*_args):
            now[0] = 103
            return inventory([endpoint()])
        with patch.object(physical_usb.sys, "platform", "darwin"), \
                patch.object(physical_usb.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(physical_usb.usb_modes, "_query", side_effect=late_reply), \
                self.assertRaisesRegex(ResetError, "timed out"):
            physical_usb.require_single_mobile_device()

    def test_deep_or_excessive_trees_are_refused(self):
        node = endpoint()
        for _ in range(physical_usb.MAX_DEPTH + 1):
            node = {"_items": [node]}
        for nodes in ([node], [{}] * (physical_usb.MAX_NODES + 1)):
            with self.subTest(count=len(nodes)), self.assertRaises(ResetError):
                self.require(inventory(nodes))

    def test_late_parsing_cannot_authorize_singleton(self):
        now = [100]
        original = physical_usb._profiler_devices
        def late_parse(*args):
            result = original(*args)
            now[0] = 103
            return result
        with patch.object(physical_usb.sys, "platform", "darwin"), \
                patch.object(physical_usb.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(physical_usb.usb_modes, "_query", return_value=inventory([endpoint()])), \
                patch.object(physical_usb, "_profiler_devices", side_effect=late_parse), \
                self.assertRaisesRegex(ResetError, "timed out"):
            physical_usb.require_single_mobile_device()


class PhysicalLinuxInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (patch.object(physical_usb, "SYSFS", self.root),
                        patch.object(physical_usb.sys, "platform", "linux")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def device(self, name, product=0x1227, vendor=0x05AC):
        folder = self.root / name
        folder.mkdir()
        (folder / "idVendor").write_text(f"{vendor:04x}\n")
        (folder / "idProduct").write_text(f"{product:04x}\n")
        return folder

    def test_physical_device_counts_once_despite_multiple_usb_interfaces(self):
        self.device("usb1", 0x0002, 0x1D6B)
        self.device("1-2.1", 0x1290)
        (self.root / "1-2.1:1.0").mkdir()
        (self.root / "1-2.1:1.1").mkdir()
        self.assertEqual(physical_usb.require_single_mobile_device(),
                         {"product_id": 0x1290, "location": "1-2.1"})

    def test_other_mode_phone_counts_even_without_serial(self):
        self.device("1-1", 0x1222)
        self.device("1-2", 0x1281)
        with self.assertRaisesRegex(ResetError, "found 2"):
            physical_usb.require_single_mobile_device()

    def test_missing_or_invalid_descriptor_refuses_incomplete_inventory(self):
        self.device("1-1")
        extra = self.device("1-2", 0x12A8)
        for contents in (None, "unknown", "05ac" * 10, "05ac" + " " * 20):
            path = extra / "idVendor"
            path.unlink(missing_ok=True)
            if contents is not None:
                path.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(ResetError):
                physical_usb.require_single_mobile_device()

    def test_unavailable_sysfs_is_not_an_empty_authoritative_inventory(self):
        with patch.object(physical_usb, "SYSFS", self.root / "missing"), self.assertRaises(ResetError):
            physical_usb.require_single_mobile_device()

    def test_reenumeration_during_scan_is_refused(self):
        self.device("1-1")
        original = physical_usb._sysfs_entries
        count = [0]
        def changing_entries():
            count[0] += 1
            if count[0] == 2:
                self.device("1-2", 0x12A8)
            return original()
        with patch.object(physical_usb, "_sysfs_entries", side_effect=changing_entries), self.assertRaises(ResetError):
            physical_usb.require_single_mobile_device()

    def test_native_windows_cannot_skip_physical_verification(self):
        with patch.object(physical_usb.sys, "platform", "win32"), self.assertRaisesRegex(ResetError, "Linux/WSL"):
            physical_usb.require_single_mobile_device()


if __name__ == "__main__":
    unittest.main()
