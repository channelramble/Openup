import contextlib
import hashlib
import io
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ios_timeout import legacy_keychain
from ios_timeout.core import ResetError


KEY = bytes(range(16))
IV = bytes(range(16, 32))
# Independent OpenSSL `enc -aes-128-cbc` fixture: b"0427" || SHA1(b"0427"),
# key 000102...0f, IV 101112...1f. This is synthetic, never device material.
KNOWN_RECORD = bytes.fromhex(
    "101112131415161718191a1b1c1d1e1f"
    "b2566b54e82c5a3f8bcd392ee902d260f89dce1d72fbe86e94cdbf33c86b5720"
)


def encrypted(value, *, checksum=None, padded=None):
    if padded is None:
        checksum = hashlib.sha1(value).digest() if checksum is None else checksum
        padder = padding.PKCS7(128).padder()
        padded = padder.update(value + checksum) + padder.finalize()
    encoder = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return IV + encoder.update(padded) + encoder.finalize()


class LegacyKeychainTests(unittest.TestCase):
    def database(self, *, rows=None, versions=(3,), schema=None):
        rows = [("DeviceLockPassword", "SpringBoard", "apple", KNOWN_RECORD)] if rows is None else rows
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.db"
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE tversion (version INTEGER)")
                connection.executemany("INSERT INTO tversion VALUES (?)", ((value,) for value in versions))
                connection.execute(schema or "CREATE TABLE genp (acct TEXT, svce TEXT, agrp TEXT, data BLOB)")
                connection.executemany("INSERT INTO genp VALUES (?, ?, ?, ?)", rows)
                connection.commit()
            return path.read_bytes()

    def recover(self, database=None, key=KEY, version="3.1.3"):
        return legacy_keychain.recover_passcode(
            self.database() if database is None else database, key, installed_version=version,
        )

    def test_independent_vector_preserves_leading_zero_and_database_bytes(self):
        database = self.database()
        before = hashlib.sha256(database).digest()
        self.assertEqual(self.recover(database), "0427")
        self.assertEqual(hashlib.sha256(database).digest(), before)

    def test_only_exact_passcode_record_is_decoded(self):
        rows = [
            ("mail", "Mail", "apple", b"not decoded"),
            ("DeviceLockPassword", "Other", "apple", b"not decoded"),
            ("DeviceLockPassword", "SpringBoard", "other", b"not decoded"),
            ("DeviceLockPassword", "SpringBoard", "apple", KNOWN_RECORD),
        ]
        self.assertEqual(self.recover(self.database(rows=rows)), "0427")

    def test_missing_and_duplicate_records_are_inconclusive_errors(self):
        row = ("DeviceLockPassword", "SpringBoard", "apple", KNOWN_RECORD)
        for rows in ([], [row, row]):
            with self.subTest(rows=len(rows)), self.assertRaises(ResetError):
                self.recover(self.database(rows=rows))

    def test_declared_nocase_collation_cannot_select_similar_credentials(self):
        schema = "CREATE TABLE genp (acct TEXT COLLATE NOCASE, svce TEXT COLLATE NOCASE, agrp TEXT COLLATE NOCASE, data BLOB)"
        rows = [("devicelockpassword", "springboard", "Apple", KNOWN_RECORD)]
        with self.assertRaises(ResetError):
            self.recover(self.database(rows=rows, schema=schema))

    def test_supported_os_versions_still_require_version_three_schema(self):
        for version in sorted(legacy_keychain.SUPPORTED_VERSIONS):
            with self.subTest(version=version):
                self.assertEqual(self.recover(version=version), "0427")
        for versions in ((), (1,), (2,), (4,), (3, 3), ("unknown",)):
            with self.subTest(versions=versions), self.assertRaises(ResetError):
                self.recover(self.database(versions=versions))

    def test_os_one_unknown_and_ios_four_do_not_create_temporary_files(self):
        database = self.database()
        for version in ("1.0", "1.1.4", "2.0", "2.1", "3.1.4", "4.0", "", None, [], "03.1.3"):
            with self.subTest(version=version), patch.object(legacy_keychain.tempfile, "TemporaryDirectory") as temporary:
                with self.assertRaises(ResetError):
                    self.recover(database, version=version)
                temporary.assert_not_called()

    def test_invalid_keys_fail_before_database_work(self):
        database = self.database()
        for key in (b"", bytes(16), KEY[:-1], KEY + b"x", KEY.hex(), None, bytearray(KEY)):
            with self.subTest(key_type=type(key).__name__), patch.object(legacy_keychain, "_read_record") as read:
                with self.assertRaises(ResetError):
                    self.recover(database, key=key)
                read.assert_not_called()

    def test_wrong_key_and_modified_iv_do_not_return_unverified_plaintext(self):
        with self.assertRaises(ResetError):
            self.recover(key=b"x" * 16)
        record = bytes([KNOWN_RECORD[0] ^ 1]) + KNOWN_RECORD[1:]
        with self.assertRaises(ResetError):
            self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))

    def test_valid_padding_does_not_replace_sha1_integrity_check(self):
        record = encrypted(b"0427", checksum=bytes(20))
        with self.assertRaises(ResetError):
            self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))

    def test_invalid_pkcs7_padding_is_rejected(self):
        # Last byte claims eight padding bytes, but the preceding seven differ.
        record = encrypted(b"", padded=b"0427" + hashlib.sha1(b"0427").digest() + b"\x07" * 7 + b"\x08")
        with self.assertRaises(ResetError):
            self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))

    def test_non_blob_null_truncated_and_oversized_records_are_rejected(self):
        for record in (None, "secret-looking-text", 1234, b"", KNOWN_RECORD[:-1], b"x" * 1024):
            with self.subTest(record_type=type(record).__name__), self.assertRaises(ResetError):
                self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))

    def test_nonempty_utf8_passcodes_are_preserved_without_normalization(self):
        for value in (" 0427 ", "aB-42!", "é123"):
            record = encrypted(value.encode("utf-8"))
            with self.subTest(value=value):
                self.assertEqual(self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)])), value)

    def test_invalid_utf8_controls_empty_and_too_long_values_are_rejected(self):
        for value in (b"", b"\xff123", b"1234\x00", b"12\n34", b"\x1b[31m", b"1" * 257):
            record = encrypted(value)
            with self.subTest(length=len(value)), self.assertRaises(ResetError):
                self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))

    def test_database_magic_page_layout_size_and_wal_are_checked(self):
        good = self.database()
        bad_page = bytearray(good)
        bad_page[16:18] = b"\x02\x01"
        wal = bytearray(good)
        wal[18:20] = b"\x02\x02"
        for value in (None, "text", b"x" * 512, good[:-1], bytes(bad_page), bytes(wal)):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ResetError):
                legacy_keychain.recover_passcode(value, KEY, installed_version="3.1.3")
        with patch.object(legacy_keychain, "MAX_DATABASE_BYTES", len(good) - 1), self.assertRaises(ResetError):
            self.recover(good)

    def test_corrupt_sqlite_content_is_rejected(self):
        value = bytearray(self.database())
        value[100:120] = b"x" * 20
        with self.assertRaises(ResetError):
            self.recover(bytes(value))

    def test_schema_views_are_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.db"
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE tversion (version INTEGER)")
                connection.execute("INSERT INTO tversion VALUES (3)")
                connection.execute("CREATE VIEW genp AS SELECT 'DeviceLockPassword' AS acct, 'SpringBoard' AS svce, 'apple' AS agrp, zeroblob(1000000000) AS data")
                connection.commit()
            with self.assertRaises(ResetError):
                self.recover(path.read_bytes())

    def test_generated_columns_are_rejected_before_query_or_integrity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.db"
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE tversion (version INTEGER)")
                connection.execute("INSERT INTO tversion VALUES (3)")
                connection.execute("CREATE TABLE genp (acct TEXT, svce TEXT, agrp TEXT, data BLOB GENERATED ALWAYS AS (zeroblob(64)) VIRTUAL)")
                connection.execute("INSERT INTO genp (acct, svce, agrp) VALUES ('DeviceLockPassword', 'SpringBoard', 'apple')")
                connection.commit()
            with patch.object(legacy_keychain, "_decode_record") as decode:
                with self.assertRaisesRegex(ResetError, "column layout"):
                    self.recover(path.read_bytes())
                decode.assert_not_called()

    def test_structural_check_does_not_evaluate_schema_check_expressions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.db"
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute("CREATE TABLE tversion (version INTEGER)")
                connection.execute("INSERT INTO tversion VALUES (3)")
                connection.execute("CREATE TABLE genp (acct TEXT, svce TEXT, agrp TEXT, data BLOB CHECK(0))")
                connection.execute("INSERT INTO genp VALUES (?, ?, ?, ?)",
                                   ("DeviceLockPassword", "SpringBoard", "apple", KNOWN_RECORD))
                connection.commit()
            self.assertEqual(self.recover(path.read_bytes()), "0427")

    def test_generated_columns_in_other_keychain_tables_are_also_rejected(self):
        database = self.database()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.db"
            path.write_bytes(database)
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE unrelated (value INTEGER, generated BLOB GENERATED ALWAYS AS (zeroblob(value)) VIRTUAL)")
                connection.execute("INSERT INTO unrelated (value) VALUES (64)")
                connection.commit()
            with self.assertRaisesRegex(ResetError, "column layout"):
                self.recover(path.read_bytes())

    def test_read_deadline_is_enforced_even_for_small_successful_query(self):
        database = self.database()
        with patch.object(legacy_keychain, "SQLITE_TIMEOUT", 0), self.assertRaises(ResetError):
            self.recover(database)

    def test_private_immutable_read_only_temp_database_is_always_removed(self):
        database = self.database()
        original_connect = sqlite3.connect
        observed = []

        def checked_connect(database_uri, **kwargs):
            from urllib.parse import urlsplit
            from urllib.request import url2pathname
            path = Path(url2pathname(urlsplit(database_uri).path))
            self.assertEqual(urlsplit(database_uri).query, "mode=ro&immutable=1")
            self.assertTrue(kwargs["uri"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            observed.append(path.parent)
            connection = original_connect(database_uri, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM genp")
            return connection

        for key in (KEY, b"x" * 16):
            with patch.object(legacy_keychain.sqlite3, "connect", side_effect=checked_connect):
                try:
                    self.recover(database, key=key)
                except ResetError:
                    self.assertNotEqual(key, KEY)
            self.assertTrue(observed)
            self.assertFalse(any(path.exists() for path in observed))

    def test_no_stdout_stderr_or_secret_exception_details(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        record = "SECRET_MARKER_DO_NOT_LOG"
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.recover(), "0427")
            with self.assertRaises(ResetError) as failure:
                self.recover(self.database(rows=[("DeviceLockPassword", "SpringBoard", "apple", record)]))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(record, str(failure.exception))
        self.assertNotIn(KEY.hex(), str(failure.exception))


if __name__ == "__main__":
    unittest.main()
