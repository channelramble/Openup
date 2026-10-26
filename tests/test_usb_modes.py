import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ios_timeout import usb_modes


SERIAL = "CPID:8950 CPRV:20 CPFM:03 SCEP:10 BDID:00 ECID:00000123456789AB IBFL:00"


def node(*, modern=True, serial=SERIAL, product="0x1227", vendor="0x05ac"):
    if modern:
        return {"_name": "Apple Mobile Device (DFU Mode)", "USBDeviceKeyVendorID": vendor,
                "USBDeviceKeyProductID": product, "USBDeviceKeySerialNumber": serial,
                "USBKeyLocationID": "0x01200000"}
    return {"_name": "Apple Mobile Device (DFU Mode)", "vendor_id": vendor,
            "product_id": product, "serial_num": serial, "location_id": "0x01200000"}


def payload(devices, data_type="SPUSBHostDataType"):
    return json.dumps({data_type: [{"_name": "USB bus", "_items": [
        {"_name": "Hub", "_items": devices}]}]}).encode()


class USBModeTests(unittest.TestCase):
    def test_both_profiler_schemas_resolve_nested_iphone5_variants(self):
        for modern, data_type in ((True, "SPUSBHostDataType"), (False, "SPUSBDataType")):
            with self.subTest(schema=data_type):
                rows = usb_modes._parse(payload([
                    node(modern=modern),
                    node(modern=modern, serial=SERIAL.replace("BDID:00", "BDID:02")),
                ], data_type), data_type)
                self.assertEqual({row["model"] for row in rows}, {"iPhone5,1", "iPhone5,2"})
                self.assertEqual({row["board"] for row in rows}, {"n41ap", "n42ap"})
                self.assertTrue(all(row["mode"] == "DFU" and row["udid"] == "" for row in rows))
                self.assertTrue(all("device_id" not in row for row in rows))
                self.assertNotIn("123456789AB", json.dumps(rows))
                self.assertNotIn(SERIAL, json.dumps(rows))

    def test_recovery_pids_and_numeric_usb_ids(self):
        for product in (0x1280, 0x1281, 0x1282, 0x1283):
            with self.subTest(product=product):
                rows = usb_modes._parse(payload([node(product=product, vendor=1452)]), usb_modes.DATA_TYPES[0])
                self.assertEqual(rows[0]["mode"], "Recovery")
        rows = usb_modes._parse(payload([node(modern=False, vendor="0x05ac (Apple Inc.)")]), usb_modes.DATA_TYPES[0])
        self.assertEqual(rows[0]["model"], "iPhone5,1")

    def test_names_and_vendor_alone_never_invent_hardware_identity(self):
        cases = [node(vendor="0x1234"), node(product="0x12a8"), node(product="0x1222"),
                 node(serial=""), node(serial="iPhone5,1"), node(serial=SERIAL.replace("8950", "8960")),
                 node(serial=SERIAL.replace("BDID:00", "BDID:FF")),
                 node(serial=SERIAL.replace("CPID:8950", "CPID:8950junk")),
                 node(serial=SERIAL + " CPID:8950"), node(serial=SERIAL + " ECID:BAD"),
                 node(serial=SERIAL.replace("00000123456789AB", "0000000000000000")),
                 node(serial=SERIAL.replace("BDID:00", "")), node(vendor=True)]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(usb_modes._parse(payload([case]), usb_modes.DATA_TYPES[0]), [])

    def test_conflicting_schema_fields_are_rejected(self):
        device = node()
        device["vendor_id"] = "0x1234"
        self.assertEqual(usb_modes._parse(payload([device]), usb_modes.DATA_TYPES[0]), [])

    def test_ecid_selection_survives_mode_and_port_change(self):
        first = node()
        second = node(product="0x1281", serial=SERIAL.replace("00000123456789AB", "123456789ab"))
        second["USBKeyLocationID"] = "0x09900000"
        a, b = (usb_modes._parse(payload([item]), usb_modes.DATA_TYPES[0])[0] for item in (first, second))
        self.assertEqual(a["selection_id"], b["selection_id"])
        third = node(serial=SERIAL.replace("00000123456789AB", "00000123456789AC"))
        c = usb_modes._parse(payload([third]), usb_modes.DATA_TYPES[0])[0]
        self.assertNotEqual(a["selection_id"], c["selection_id"])

    def test_no_ecid_uses_port_identity_without_merging_multiple_phones(self):
        first = node(serial="CPID:8950 BDID:00")
        second = dict(first, USBKeyLocationID="0x01300000")
        rows = usb_modes._parse(payload([first, second]), usb_modes.DATA_TYPES[0])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["selection_id"] for row in rows}), 2)

    def test_original_iphone_dfu_with_zero_ecid_is_a_location_hint_not_a_udid(self):
        first = node(serial="CPID:8900 BDID:00 ECID:0")
        second = dict(first, USBKeyLocationID="0x01300000")
        rows = usb_modes._parse(payload([first, second]), usb_modes.DATA_TYPES[0])
        self.assertEqual([row["model"] for row in rows], ["iPhone1,1", "iPhone1,1"])
        self.assertTrue(all(row["udid"] == "" and row["mode"] == "DFU" for row in rows))
        self.assertEqual(len({row["selection_id"] for row in rows}), 2)
        # Generic WTF shares the S5L8900 boot ROM identity and cannot select 2G.
        self.assertEqual(usb_modes._parse(payload([dict(first, USBDeviceKeyProductID="0x1222")]),
                                         usb_modes.DATA_TYPES[0]), [])

    def test_malformed_and_oversized_json_do_not_escape(self):
        for bad in (None, b"{", b"[]", b'{"SPUSBHostDataType":{}}', b"x" * (usb_modes.MAX_OUTPUT + 1)):
            with self.subTest(length=len(bad) if bad else None):
                self.assertIsNone(usb_modes._parse(bad, usb_modes.DATA_TYPES[0]))

    def test_old_data_type_fallback_and_empty_current_inventory(self):
        with patch.object(usb_modes.sys, "platform", "darwin"), \
             patch.object(usb_modes, "_query", side_effect=[b"{}", payload([node(modern=False)], "SPUSBDataType")]) as query:
            self.assertEqual(usb_modes.list_devices()[0]["model"], "iPhone5,1")
            self.assertEqual([call.args[0] for call in query.call_args_list], list(usb_modes.DATA_TYPES))
        with patch.object(usb_modes.sys, "platform", "darwin"), \
             patch.object(usb_modes, "_query", return_value=payload([])) as query:
            self.assertEqual(usb_modes.list_devices(), [])
            self.assertEqual(query.call_count, 1)

    def test_unavailable_or_unsupported_inventory_is_nonfatal(self):
        for platform in ("win32", "linux"):
            with patch.object(usb_modes.sys, "platform", platform), patch.object(usb_modes, "_query") as query:
                self.assertEqual(usb_modes.list_devices(), [])
                query.assert_not_called()
        with patch.object(usb_modes.sys, "platform", "darwin"), \
             patch.object(usb_modes, "_query", side_effect=FileNotFoundError):
            self.assertEqual(usb_modes.list_devices(), [])

    def test_fallback_shares_total_deadline(self):
        with patch.object(usb_modes.sys, "platform", "darwin"), \
             patch.object(usb_modes.time, "monotonic", side_effect=[10, 10, 15.01]), \
             patch.object(usb_modes, "_query", return_value=None) as query:
            self.assertEqual(usb_modes.list_devices(), [])
            query.assert_called_once_with("SPUSBHostDataType", 5.0)

    def run_fake_profiler(self, program, timeout=2):
        original = subprocess.Popen
        children = []

        def spawn(command, **kwargs):
            self.assertEqual(command, ["/usr/sbin/system_profiler", "SPUSBHostDataType", "-json"])
            child = original([sys.executable, "-c", program], **kwargs)
            children.append(child)
            return child

        with patch.object(usb_modes.subprocess, "Popen", side_effect=spawn):
            result = usb_modes._query("SPUSBHostDataType", timeout)
        self.assertTrue(all(child.poll() is not None for child in children))
        return result

    def test_profiler_success_and_failure_status(self):
        self.assertEqual(self.run_fake_profiler("import sys; sys.stdout.write('{}')"), b"{}")
        self.assertIsNone(self.run_fake_profiler("import sys; print('{}'); sys.exit(1)"))

    def test_hung_and_excessive_profiler_are_killed_and_reaped(self):
        start = time.monotonic()
        self.assertIsNone(self.run_fake_profiler("import time; time.sleep(30)", timeout=0.2))
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNone(self.run_fake_profiler(
            "import os,time; os.write(1, b'x' * (1024*1024+1)); time.sleep(30)"))


if __name__ == "__main__":
    unittest.main()
