"""Read only the legacy SpringBoard passcode from an already acquired keychain.

Format evidence: Jean-Baptiste Bedrune and Jean Sigwald, SSTIC 2012, slide 17:
https://www.sstic.org/media/SSTIC2012/SSTIC-actes/forensicsios/
SSTIC2012-Slides-forensicsios-sigwald_bedrune.pdf

The independently implemented decoder follows their BSD-licensed project's
keychain3.py and keychain/__init__.py at this exact source revision:
https://github.com/nabla-c0d3/iphone-dataprotection/tree/
572dd5cd8c07f5f14f7ea9488041031dd22a26bb/python_scripts/keychain
Its version-3 records contain IV || AES-128-CBC(key835, data || SHA1(data)),
with PKCS#7 padding. Existing third_party/iphone-dataprotection/licenses holds
the upstream project metadata and BSD license. This is not a system-keybag
brute force and does not establish compatibility with iPhone OS 1.x schemas.

The caller must first verify the physical phone and installed OS, acquire a
coherent database from a quiescent read-only mount, and refuse pending journal
or WAL sidecars. This module accepts bytes only, never accesses the phone,
never changes the supplied database, and never logs a key or recovered value.
"""

import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .core import ResetError


MAX_DATABASE_BYTES = 16 * 1024 * 1024
MAX_PASSCODE_BYTES = 256
MAX_RECORD_BYTES = 16 + ((MAX_PASSCODE_BYTES + 20) // 16 + 1) * 16
SQLITE_TIMEOUT = 2.0
SUPPORTED_VERSIONS = frozenset(("2.2", "2.2.1", "3.0", "3.0.1", "3.1", "3.1.2", "3.1.3"))
SQLITE_MAGIC = b"SQLite format 3\x00"


def _check_database(database):
    if not isinstance(database, bytes) or not 512 <= len(database) <= MAX_DATABASE_BYTES:
        raise ResetError("Legacy keychain is missing or exceeds the permitted size.")
    if database[:16] != SQLITE_MAGIC:
        raise ResetError("Legacy keychain is not a SQLite 3 database.")
    page_size = int.from_bytes(database[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    if (not 512 <= page_size <= 65536 or page_size & (page_size - 1)
            or len(database) % page_size):
        raise ResetError("Legacy keychain has an invalid or truncated page layout.")
    # Old supported databases use rollback journals. An immutable connection
    # cannot apply a WAL, and silently ignoring one could return a stale code.
    if database[18:20] != b"\x01\x01":
        raise ResetError("Legacy keychain requires an unsupported journal format.")


def _read_record(database):
    deadline = time.monotonic() + SQLITE_TIMEOUT
    connection = None
    try:
        with tempfile.TemporaryDirectory(prefix="openup-legacy-keychain-") as folder:
            path = Path(folder) / "keychain.db"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(database)
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True,
                                             timeout=0, isolation_level=None)
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA cache_size=-1024")
                # Structural checking must not evaluate untrusted CHECK
                # expressions, which can allocate outside the VM timer.
                connection.execute("PRAGMA ignore_check_constraints=ON")
                # Reject virtual tables and generated columns in every table:
                # quick_check walks the whole database, not just our two tables.
                tables = connection.execute(
                    "SELECT name, type, sql FROM sqlite_master WHERE type='table' "
                    "OR name IN ('tversion', 'genp')"
                ).fetchall()
                if (not {"tversion", "genp"}.issubset({row[0] for row in tables})
                        or any(row[1] != "table" or not isinstance(row[2], str)
                               or not row[2].lstrip().upper().startswith("CREATE TABLE")
                               for row in tables)):
                    raise ResetError("Legacy keychain has an unsupported table layout.")
                for table, _, _ in tables:
                    if time.monotonic() >= deadline:
                        raise ResetError("Legacy keychain read exceeded its time limit.")
                    required = {"version"} if table == "tversion" else (
                        {"acct", "svce", "agrp", "data"} if table == "genp" else set())
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = connection.execute("PRAGMA table_xinfo(" + quoted + ")").fetchall()
                    if (not columns or any(len(column) < 7 or column[6] != 0 for column in columns)
                            or not required.issubset({column[1] for column in columns})):
                        # Generated columns can synthesize arbitrarily large
                        # blobs from a tiny file; accept stored columns only.
                        raise ResetError("Legacy keychain has an unsupported column layout.")
                if connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
                    raise ResetError("Legacy keychain integrity check failed.")
                versions = connection.execute("SELECT version FROM tversion LIMIT 2").fetchall()
                if len(versions) != 1 or type(versions[0][0]) is not int or versions[0][0] != 3:
                    raise ResetError("Legacy keychain schema version is unsupported.")
                # BINARY overrides a column's declared collation: a similar
                # account/service name must never identify another credential.
                rows = connection.execute(
                    "SELECT data FROM genp WHERE acct COLLATE BINARY = ? "
                    "AND svce COLLATE BINARY = ? AND agrp COLLATE BINARY = ? LIMIT 2",
                    ("DeviceLockPassword", "SpringBoard", "apple"),
                ).fetchall()
                if time.monotonic() >= deadline:
                    raise ResetError("Legacy keychain read exceeded its time limit.")
                if len(rows) != 1:
                    raise ResetError("Legacy keychain does not contain exactly one supported passcode record.")
                return rows[0][0]
            finally:
                if connection is not None:
                    connection.close()
    except (sqlite3.Error, OSError):
        # SQLite errors and local paths can contain source-controlled text.
        # Do not surface them alongside sensitive keychain data.
        raise ResetError("Legacy keychain could not be read safely.") from None


def _decode_record(record, key835):
    if (not isinstance(record, bytes) or not 48 <= len(record) <= MAX_RECORD_BYTES
            or len(record) % 16):
        raise ResetError("Legacy passcode record has an invalid encrypted layout.")
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        decoder = Cipher(algorithms.AES(key835), modes.CBC(record[:16])).decryptor()
        padded = decoder.update(record[16:]) + decoder.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        clear = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise ResetError("Legacy passcode record failed decryption integrity checks.") from None
    if not 20 < len(clear) <= MAX_PASSCODE_BYTES + 20:
        raise ResetError("Legacy passcode record failed decryption integrity checks.")
    value, checksum = clear[:-20], clear[-20:]
    if not hmac.compare_digest(hashlib.sha1(value).digest(), checksum):
        raise ResetError("Legacy passcode record failed decryption integrity checks.")
    try:
        passcode = value.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ResetError("Legacy passcode record has an unsupported text encoding.") from None
    if not passcode.isprintable():
        raise ResetError("Legacy passcode record contains unsupported control characters.")
    return passcode


def recover_passcode(database: bytes, key835: bytes, *, installed_version: str) -> str:
    """Return only the verified passcode; caller controls explicit result display.

    Accept only the evidenced OS range and version-3 database format. A missing
    record is inconclusive, never proof that the phone has no passcode. All
    temporary database copies are private and removed on success or failure.
    """
    if not isinstance(installed_version, str) or installed_version not in SUPPORTED_VERSIONS:
        raise ResetError("Legacy passcode recovery requires iPhone OS 2.2 through 3.1.3.")
    if not isinstance(key835, bytes) or len(key835) != 16 or key835 == bytes(16):
        raise ResetError("Legacy device key is missing or invalid.")
    _check_database(database)
    return _decode_record(_read_record(database), key835)
