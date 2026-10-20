"""Check the deployment target of every native library in a desktop bundle.

Only Mach-O headers and load-command metadata are read. This verifies the
declared operating-system requirement; execution on the oldest supported OS is
still needed to establish runtime compatibility.

Format references: Apple's xnu EXTERNAL_HEADERS/mach-o/{loader,fat}.h.
"""

import os
from pathlib import Path
import posixpath
import stat
import struct


class MacOSCompatibilityError(ValueError):
    """A native component cannot satisfy the advertised macOS requirement."""


_CPUS = {"x86_64": 0x01000007, "arm64": 0x0100000C}
_THIN = {
    b"\xce\xfa\xed\xfe": ("<", 28),
    b"\xfe\xed\xfa\xce": (">", 28),
    b"\xcf\xfa\xed\xfe": ("<", 32),
    b"\xfe\xed\xfa\xcf": (">", 32),
}
_FAT = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}
_ARCHIVE_MAGIC = b"!<arch>\n"
_LOADABLE_TYPES = {2, 3, 5, 6, 7, 8, 11, 12}
_MAX_COMMANDS = 65536
_MAX_COMMAND_BYTES = 16 * 1024 * 1024
_MAX_ARCHITECTURES = 128
_DYLIB_LOAD_COMMANDS = {0xC, 0x80000018, 0x8000001F, 0x80000023, 0x20}


def _version(number):
    return (number >> 16, (number >> 8) & 0xFF, number & 0xFF)


def _version_text(version):
    return ".".join(str(part) for part in version)


def _read(handle, offset, size, limit):
    if offset < 0 or size < 0 or offset + size > limit:
        raise MacOSCompatibilityError("truncated Mach-O header or load commands")
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise MacOSCompatibilityError("truncated Mach-O header or load commands")
    return data


def _thin_version(handle, offset, size, requested_cpu):
    """Return the selected loadable image's min version, or None for objects."""
    limit = offset + size
    magic = _read(handle, offset, 4, limit)
    if magic not in _THIN:
        raise MacOSCompatibilityError("fat slice is not a Mach-O image")
    endian, header_size = _THIN[magic]
    fields = struct.unpack(
        endian + ("8I" if header_size == 32 else "7I"),
        _read(handle, offset, header_size, limit),
    )
    _, cpu, _, filetype, command_count, command_bytes, *_ = fields
    if cpu != requested_cpu:
        raise MacOSCompatibilityError("missing requested architecture")
    if (header_size == 32) != bool(cpu & 0x01000000):
        raise MacOSCompatibilityError("Mach-O CPU and header width disagree")
    if command_count > _MAX_COMMANDS or command_bytes > _MAX_COMMAND_BYTES:
        raise MacOSCompatibilityError("Mach-O load-command metadata exceeds safe limits")
    if command_count * 8 > command_bytes:
        raise MacOSCompatibilityError("invalid Mach-O load-command count")
    cursor = offset + header_size
    end = cursor + command_bytes
    if end > limit:
        raise MacOSCompatibilityError("truncated Mach-O load commands")
    versions = []
    for _ in range(command_count):
        command, length = struct.unpack(endian + "2I", _read(handle, cursor, 8, end))
        alignment = 8 if header_size == 32 else 4
        if length < 8 or length % alignment or cursor + length > end:
            raise MacOSCompatibilityError("invalid Mach-O load-command size")
        if command == 0x32:  # LC_BUILD_VERSION
            if length < 24:
                raise MacOSCompatibilityError("truncated LC_BUILD_VERSION")
            platform, minimum, _, tool_count = struct.unpack(
                endian + "4I", _read(handle, cursor + 8, 16, cursor + length)
            )
            if 24 + tool_count * 8 > length:
                raise MacOSCompatibilityError("truncated LC_BUILD_VERSION tool records")
            if platform != 1:  # PLATFORM_MACOS
                raise MacOSCompatibilityError(
                    "Mach-O targets platform %d instead of macOS" % platform
                )
            versions.append(_version(minimum))
        elif command == 0x24:  # LC_VERSION_MIN_MACOSX
            if length != 16:
                raise MacOSCompatibilityError("invalid LC_VERSION_MIN_MACOSX")
            minimum, _ = struct.unpack(
                endian + "2I", _read(handle, cursor + 8, 8, cursor + length)
            )
            versions.append(_version(minimum))
        elif command in _DYLIB_LOAD_COMMANDS:
            if length < 24:
                raise MacOSCompatibilityError("truncated dylib load command")
            name_offset, = struct.unpack(
                endian + "I", _read(handle, cursor + 8, 4, cursor + length)
            )
            if not 24 <= name_offset < length:
                raise MacOSCompatibilityError("invalid dylib load-command name offset")
            raw_name = _read(
                handle, cursor + name_offset, min(length - name_offset, 16384),
                cursor + length,
            )
            if b"\x00" not in raw_name:
                raise MacOSCompatibilityError("unterminated dylib load-command name")
            try:
                name = raw_name.split(b"\x00", 1)[0].decode("utf-8")
            except UnicodeDecodeError as error:
                raise MacOSCompatibilityError("invalid dylib load-command name") from error
            system_path = posixpath.normpath(name) if name.startswith("/") else name
            allowed = system_path.startswith(("/System/Library/", "/usr/lib/")) or (
                name.startswith(("@rpath/", "@loader_path/", "@executable_path/"))
            )
            if not allowed:
                raise MacOSCompatibilityError("nonportable dylib dependency: %s" % name)
        cursor += length
    if cursor != end:
        raise MacOSCompatibilityError("Mach-O load-command sizes do not match header")
    if filetype not in _LOADABLE_TYPES:
        return None
    if not versions or any(version == (0, 0, 0) for version in versions):
        raise MacOSCompatibilityError("loadable Mach-O has no macOS deployment version")
    return max(versions)


def _image_version(handle, size, requested_cpu):
    magic = handle.read(4)
    if magic in _THIN:
        return _thin_version(handle, 0, size, requested_cpu)
    if magic not in _FAT:
        return None
    endian, wide = _FAT[magic]
    count, = struct.unpack(endian + "I", _read(handle, 4, 4, size))
    if not 1 <= count <= _MAX_ARCHITECTURES:
        raise MacOSCompatibilityError("invalid fat Mach-O architecture count")
    record_size = 32 if wide else 20
    table_end = 8 + count * record_size
    if table_end > size:
        raise MacOSCompatibilityError("truncated fat Mach-O architecture table")
    slices = []
    for index in range(count):
        fields = struct.unpack(
            endian + ("IIQQII" if wide else "5I"),
            _read(handle, 8 + index * record_size, record_size, size),
        )
        cpu, _, offset, length, *_ = fields
        if offset < table_end or length < 4 or offset + length > size:
            raise MacOSCompatibilityError("invalid or truncated fat Mach-O slice")
        slices.append((offset, length, cpu))
    ordered = sorted(slices)
    for previous, following in zip(ordered, ordered[1:]):
        if previous[0] + previous[1] > following[0]:
            raise MacOSCompatibilityError("overlapping fat Mach-O slices")
    # Universal static libraries use the fat wrapper too. They are link-time
    # inputs, not images loaded by this app, and do not impose a runtime target.
    archives = [
        length >= 8 and _read(handle, offset, 8, size) == _ARCHIVE_MAGIC
        for offset, length, _ in slices
    ]
    if all(archives):
        return None
    selected = [item for item in slices if item[2] == requested_cpu]
    if not selected:
        raise MacOSCompatibilityError("missing requested architecture in fat Mach-O")
    versions = []
    for offset, length, _ in selected:
        version = _thin_version(handle, offset, length, requested_cpu)
        if version is not None:
            versions.append(version)
    return max(versions) if versions else None


def validate_macos_bundle(root, architecture, minimum_version):
    """Validate native deployment versions against the advertised OS minimum.

    ``architecture`` is ``"x86_64"`` or ``"arm64"``; ``minimum_version`` is a
    three-integer tuple, for example ``(12, 0, 0)``. Symlinks, static archives
    and non-Mach-O files are ignored. Returns a JSON-serializable report.
    Raises :class:`MacOSCompatibilityError` on an incompatible/malformed image,
    an empty native bundle, or invalid arguments.
    """
    if architecture not in _CPUS:
        raise MacOSCompatibilityError("unsupported architecture: %s" % architecture)
    if (
        not isinstance(minimum_version, (tuple, list))
        or len(minimum_version) != 3
        or any(type(part) is not int or part < 0 for part in minimum_version)
    ):
        raise MacOSCompatibilityError("minimum_version must contain three nonnegative integers")
    minimum_version = tuple(minimum_version)
    root = Path(root)
    if not root.is_dir():
        raise MacOSCompatibilityError("bundle root is not a directory: %s" % root)
    count = 0
    maximum = (0, 0, 0)
    for folder, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = Path(folder) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            try:
                with path.open("rb") as handle:
                    version = _image_version(
                        handle, os.fstat(handle.fileno()).st_size, _CPUS[architecture]
                    )
                if version is None:
                    continue
                if version > minimum_version:
                    raise MacOSCompatibilityError(
                        "requires macOS %s, above advertised macOS %s"
                        % (_version_text(version), _version_text(minimum_version))
                    )
                count += 1
                maximum = max(maximum, version)
            except MacOSCompatibilityError as error:
                raise MacOSCompatibilityError(
                    "%s: %s" % (path.relative_to(root), error)
                ) from error
    if not count:
        raise MacOSCompatibilityError("bundle contains no loadable Mach-O images")
    return {
        "architecture": architecture,
        "minimum_version": _version_text(minimum_version),
        "binary_count": count,
        "maximum_deployment_target": _version_text(maximum),
    }
