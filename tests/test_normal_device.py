import plistlib
import socket
import struct
import unittest
from unittest.mock import patch

from ios_timeout import normal_device, usbmux
from ios_timeout.core import ResetError
from tests.test_usbmux import Socket, response


DEVICE = {"udid": "a" * 40, "device_id": 7, "product_id": 0x12A8}


class TimedSocket(Socket):
    def __init__(self, response):
        super().__init__(response)
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


def reply(value):
    body = plistlib.dumps(value)
    return struct.pack(">I", len(body)) + body


def normal_replies():
    return reply({"Request": "QueryType", "Type": "com.apple.mobile.lockdown"}) + b"".join(
        reply({"Request": "GetValue", "Value": value})
        for value in ("iPhone5,1", "N41AP", "10.3.4"))


class NormalDeviceTests(unittest.TestCase):
    def test_normal_mode_reads_only_bounded_public_identity_without_pairing(self):
        sock = TimedSocket(normal_replies())
        with patch.object(usbmux, "connect_port", return_value=sock) as connect, \
                patch.object(normal_device.time, "monotonic", return_value=100):
            result = normal_device.enrich_devices([DEVICE])
        self.assertEqual(result, [{**DEVICE, "model": "iPhone5,1", "board": "n41ap",
                                   "version": "10.3.4", "mode": "normal"}])
        self.assertNotIn("mode", DEVICE)
        connect.assert_called_once_with(DEVICE, 62078, None, deadline=102)
        self.assertTrue(sock.closed)
        self.assertTrue(all(0 < timeout <= 2 for timeout in sock.timeouts))
        sent, requests = sock.sent, []
        while sent:
            length, = struct.unpack(">I", sent[:4])
            requests.append(plistlib.loads(sent[4:4 + length]))
            sent = sent[4 + length:]
        self.assertEqual(requests, [
            {"Label": "openup", "Request": "QueryType"},
            *({"Label": "openup", "Request": "GetValue", "Key": key}
              for key in ("ProductType", "HardwareModel", "ProductVersion")),
        ])

    def test_locked_or_untrusted_phone_remains_in_usb_list(self):
        for error in ("PasswordProtected", "InvalidHostID", "PairingDialogResponsePending"):
            sock = TimedSocket(reply({"Request": "QueryType", "Error": error}))
            with self.subTest(error=error), patch.object(usbmux, "connect_port", return_value=sock):
                result = normal_device.enrich_devices([DEVICE])
            self.assertEqual(result[0]["udid"], DEVICE["udid"])
            self.assertNotIn("mode", result[0])
            self.assertIn("still listed", result[0]["info_error"])
            self.assertTrue(sock.closed)

    def test_malformed_or_rejected_lockdown_packets_are_not_normal_devices(self):
        packets = [struct.pack(">I", size) for size in (0, normal_device.MAX_REPLY + 1)] + [
            reply(["unexpected"]), reply({"Request": "Other"}),
            reply({"Request": "QueryType", "Type": "other.service"}),
            reply({"Request": "QueryType", "Result": "Failure"}),
            struct.pack(">I", 8) + b"<?xml???",
            normal_replies()[:-10],
        ]
        for packet in packets:
            sock = TimedSocket(packet)
            with self.subTest(packet=packet[:16]), patch.object(usbmux, "connect_port", return_value=sock):
                result = normal_device.enrich_devices([DEVICE])
            self.assertNotIn("mode", result[0])
            self.assertIn("info_error", result[0])
            self.assertTrue(sock.closed)

    def test_unexpected_identity_values_do_not_change_the_base_row(self):
        for invalid in (False, "iPhone5,1\nspoof", "iPhone" + "5" * 80 + ",1"):
            packet = (reply({"Request": "QueryType", "Type": "com.apple.mobile.lockdown"})
                      + reply({"Request": "GetValue", "Value": invalid}))
            with self.subTest(value=invalid), \
                    patch.object(usbmux, "connect_port", return_value=TimedSocket(packet)):
                result = normal_device.enrich_devices([DEVICE])
            self.assertNotIn("model", result[0])
            self.assertEqual(result[0]["device_id"], DEVICE["device_id"])

    def test_conflicting_product_and_board_do_not_autoselect_a_variant(self):
        packet = reply({"Request": "QueryType", "Type": "com.apple.mobile.lockdown"}) + b"".join(
            reply({"Request": "GetValue", "Value": value})
            for value in ("iPhone5,1", "N42AP", "10.3.4"))
        sock = TimedSocket(packet)
        with patch.object(usbmux, "connect_port", return_value=sock):
            result = normal_device.enrich_devices([DEVICE])
        self.assertEqual(result[0]["udid"], DEVICE["udid"])
        self.assertNotIn("model", result[0])
        self.assertNotIn("mode", result[0])
        self.assertIn("info_error", result[0])
        self.assertTrue(sock.closed)

    def test_total_deadline_is_shared_across_devices_and_remaining_rows_survive(self):
        now = [100.0]
        deadlines = []
        def unavailable(_device, *, path, deadline):
            deadlines.append(deadline)
            now[0] = deadline
            raise TimeoutError("fixture timeout")
        devices = [{**DEVICE, "device_id": number} for number in range(1, 20)]
        with patch.object(normal_device.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(normal_device, "device_info", side_effect=unavailable):
            result = normal_device.enrich_devices(devices, timeout=3, device_timeout=2)
        self.assertEqual(deadlines, [102, 103])
        self.assertEqual(len(result), len(devices))
        self.assertTrue(all("info_error" in row for row in result))
        self.assertIn("scan deadline", result[-1]["info_error"])

    def test_fragmented_reply_cannot_reset_the_read_deadline(self):
        now = [100.0]
        class SlowSocket(TimedSocket):
            def recv(self, count):
                now[0] += .6
                return super().recv(min(count, 1))
        sock = SlowSocket(normal_replies())
        with patch.object(normal_device.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(usbmux, "connect_port", return_value=sock):
            result = normal_device.enrich_devices([DEVICE], timeout=1)
        self.assertIn("info_error", result[0])
        self.assertLess(now[0], 102)
        self.assertTrue(sock.closed)


class DiscoveryTransportTests(unittest.TestCase):
    def test_generic_port_reselects_serial_and_uses_network_byte_order(self):
        sock = TimedSocket(response({"Number": 0}))
        newer = {**DEVICE, "device_id": 9}
        with patch.object(usbmux, "list_devices", return_value=[newer]) as listing, \
                patch.object(usbmux, "connect_socket", return_value=sock), \
                patch.object(usbmux.time, "monotonic", return_value=100):
            self.assertIs(usbmux.connect_port(DEVICE, 62078, deadline=102), sock)
        request = plistlib.loads(sock.sent[16:])
        self.assertEqual(request["DeviceID"], 9)
        self.assertEqual(request["PortNumber"], socket.htons(62078))
        self.assertEqual(listing.call_count, 2)
        self.assertFalse(sock.closed)

    def test_generic_port_closes_when_serial_disappears_after_connection(self):
        sock = TimedSocket(response({"Number": 0}))
        with patch.object(usbmux, "list_devices", side_effect=[[DEVICE], []]), \
                patch.object(usbmux, "connect_socket", return_value=sock), \
                self.assertRaisesRegex(ResetError, "unavailable or ambiguous"):
            usbmux.connect_port(DEVICE, 62078)
        self.assertTrue(sock.closed)

    def test_generic_port_failure_does_not_instruct_booting_ssh(self):
        sock = TimedSocket(response({"Number": 3}))
        with patch.object(usbmux, "list_devices", return_value=[DEVICE]), \
                patch.object(usbmux, "connect_socket", return_value=sock), \
                self.assertRaisesRegex(ResetError, "device service") as error:
            usbmux.connect_port(DEVICE, 62078)
        self.assertNotIn("SSH", str(error.exception))
        self.assertTrue(sock.closed)

    def test_list_devices_deadline_preserves_strict_usb_filter(self):
        packet = response({"DeviceList": [
            {"DeviceID": 7, "Properties": {"ConnectionType": "USB", "SerialNumber": "a" * 40}},
            {"DeviceID": 8, "Properties": {"ConnectionType": "Network", "SerialNumber": "b" * 40}},
        ]})
        sock = TimedSocket(packet)
        with patch.object(usbmux, "connect_socket", return_value=sock) as connect, \
                patch.object(usbmux.time, "monotonic", return_value=100):
            result = usbmux.list_devices(deadline=102)
        self.assertEqual([row["udid"] for row in result], ["a" * 40])
        connect.assert_called_once_with(None, deadline=102)
        self.assertTrue(all(timeout == 2 for timeout in sock.timeouts))

    def test_expired_deadline_closes_socket_before_connecting(self):
        with patch.object(usbmux.socket, "socket") as factory, \
                patch.object(usbmux.time, "monotonic", return_value=100), \
                self.assertRaises(ResetError):
            usbmux.connect_socket(deadline=99)
        factory.return_value.connect.assert_not_called()
        factory.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
