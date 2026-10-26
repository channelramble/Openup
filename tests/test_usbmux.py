import plistlib
import socket
import unittest
from unittest.mock import patch

from ios_timeout import usbmux
from ios_timeout.core import ResetError


class Socket:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.closed = False

    def recv(self, count):
        # Exercise partial reads at every protocol boundary.
        answer, self.response = self.response[:min(count, 7)], self.response[min(count, 7):]
        return answer

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def response(data, length=None, tag=1, kind=8):
    body = plistlib.dumps(data)
    return usbmux.HEADER.pack(length or (len(body) + 16), 1, kind, tag) + body


class USBMuxTests(unittest.TestCase):
    def test_list_excludes_network_devices(self):
        sock = Socket(response({"DeviceList": [
            {"DeviceID": 3, "Properties": {"ConnectionType": "USB", "SerialNumber": "a" * 40}},
            {"DeviceID": 7, "Properties": {"ConnectionType": "Network", "SerialNumber": "b" * 40}},
        ]}))
        with patch.object(usbmux, "connect_socket", return_value=sock):
            devices = usbmux.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["device_id"], 3)
        self.assertTrue(sock.closed)
        self.assertEqual(plistlib.loads(sock.sent[16:])["MessageType"], "ListDevices")

    def test_multiple_devices_require_selection(self):
        devices = [{"udid": "a", "device_id": 1}, {"udid": "b", "device_id": 2}]
        with self.assertRaises(ResetError):
            usbmux.select_device(devices)
        self.assertEqual(usbmux.select_device(devices, "b")["device_id"], 2)
        with self.assertRaises(ResetError):
            usbmux.select_device(devices, "c")

    def test_no_devices(self):
        with self.assertRaises(ResetError):
            usbmux.select_device([])

    def test_wait_for_device_retries_until_the_phone_reappears(self):
        device = {"udid": "a" * 40, "device_id": 7}
        waits = []
        with patch.object(usbmux, "list_devices", side_effect=[[], [], [device]]) as listed, \
             patch.object(usbmux.time, "sleep", lambda _s: None):
            result = usbmux.wait_for_device("a" * 40, timeout=30, on_wait=waits.append)
        self.assertEqual(result["device_id"], 7)
        self.assertEqual(listed.call_count, 3)
        self.assertEqual(waits, [0, 1])

    def test_wait_for_device_raises_the_clear_error_after_the_deadline(self):
        times = iter([100.0, 100.0, 101.0, 116.0])  # start, loop checks past the 15s deadline
        with patch.object(usbmux, "list_devices", return_value=[]), \
             patch.object(usbmux.time, "sleep", lambda _s: None), \
             patch.object(usbmux.time, "monotonic", lambda: next(times)), \
             self.assertRaisesRegex(ResetError, "No matching USB iPhone"):
            usbmux.wait_for_device("a" * 40, timeout=15)

    def test_wait_for_device_does_not_retry_several_connected_devices(self):
        devices = [{"udid": "a" * 40, "device_id": 1}, {"udid": "b" * 40, "device_id": 2}]
        with patch.object(usbmux, "list_devices", return_value=devices) as listed, \
             patch.object(usbmux.time, "sleep", lambda _s: None), \
             self.assertRaisesRegex(ResetError, "Select one with --udid"):
            usbmux.wait_for_device(timeout=30)
        self.assertEqual(listed.call_count, 1)

    def test_connect_uses_network_byte_order_and_selected_device(self):
        sock = Socket(response({"Number": 0}))
        with patch.object(usbmux, "connect_socket", return_value=sock):
            self.assertIs(usbmux.tunnel(42), sock)
        sent = plistlib.loads(sock.sent[16:])
        self.assertEqual(sent["DeviceID"], 42)
        self.assertEqual(sent["PortNumber"], socket.htons(22))
        self.assertFalse(sock.closed)

    def test_refused_connection_closes_socket(self):
        sock = Socket(response({"Number": 3}))
        with patch.object(usbmux, "connect_socket", return_value=sock), self.assertRaises(ResetError):
            usbmux.tunnel(2)
        self.assertTrue(sock.closed)

    def test_invalid_headers_rejected(self):
        for packet in (response({}, length=15), response({}, length=100000000),
                       response({}, tag=8), response({}, kind=6)):
            with self.subTest(packet=packet[:16]), self.assertRaises(ResetError):
                usbmux.request(Socket(packet), {})

    def test_truncated_response_rejected(self):
        with self.assertRaises(ResetError):
            usbmux.request(Socket(response({})[:-10]), {})

    def test_invalid_device_port(self):
        for port in (0, -1, 65536, True, 22.5):
            with self.assertRaises(ResetError):
                usbmux.tunnel(1, port)

    def test_hub_reenumeration_reconnects_to_the_new_device_id(self):
        # A USB-C hub/dock re-enumeration keeps the phone's serial and only
        # reassigns usbmuxd's DeviceID. The tunnel must follow the serial to the
        # new DeviceID instead of rejecting it as a swapped device.
        sock = Socket(response({"Number": 0}))
        with patch.object(usbmux, "connect_socket", return_value=sock), \
             patch.object(usbmux, "list_devices", return_value=[{"udid": "a" * 40, "device_id": 9}]):
            self.assertIs(usbmux.tunnel(8, expected_udid="a" * 40), sock)
        self.assertEqual(plistlib.loads(sock.sent[16:])["DeviceID"], 9)
        self.assertFalse(sock.closed)

    def test_absent_serial_before_connect_is_rejected(self):
        # A different (or missing) serial is a genuine swap: reject before connecting.
        with patch.object(usbmux, "list_devices", return_value=[{"udid": "b" * 40, "device_id": 9}]), \
             patch.object(usbmux, "connect_socket") as connection, self.assertRaises(ResetError):
            usbmux.tunnel(8, expected_udid="a" * 40)
        connection.assert_not_called()

    def test_serial_change_during_connect_closes_tunnel(self):
        # The selected serial disappears while connecting: the post-connect
        # serial check must fail and close the socket.
        sock = Socket(response({"Number": 0}))
        with patch.object(usbmux, "connect_socket", return_value=sock), \
             patch.object(usbmux, "list_devices", side_effect=[
                 [{"udid": "a" * 40, "device_id": 9}],
                 [{"udid": "b" * 40, "device_id": 9}],
             ]), self.assertRaises(ResetError):
            usbmux.tunnel(9, expected_udid="a" * 40)
        self.assertTrue(sock.closed)

    def test_malformed_and_duplicate_usb_entries_are_rejected(self):
        valid = {"DeviceID": 3, "Properties": {"ConnectionType": "USB", "SerialNumber": "a" * 40}}
        for entries in ([None], [{"Properties": None}], [valid, valid],
                        [{"DeviceID": True, "Properties": valid["Properties"]}]):
            # plist cannot represent None; exercise parsed daemon response directly.
            with patch.object(usbmux, "connect_socket", return_value=Socket(b"")), \
                 patch.object(usbmux, "request", return_value={"DeviceList": entries}), \
                 self.subTest(entries=entries), self.assertRaises(ResetError):
                usbmux.list_devices()

    def test_boolean_connect_result_is_rejected(self):
        sock = Socket(response({"Number": False}))
        with patch.object(usbmux, "connect_socket", return_value=sock), self.assertRaises(ResetError):
            usbmux.tunnel(1)
        self.assertTrue(sock.closed)


if __name__ == "__main__":
    unittest.main()
