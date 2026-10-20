# Bundled desktop components

The application source is MIT licensed. Desktop archives also contain unmodified,
dynamically loaded third-party runtimes and libraries with their own licenses.
Their license files and distribution metadata remain in the bundled runtime.
The complete versions, original artifact URLs, and SHA-256 checksums are recorded
in `desktop-build-lock.json`.

- CPython and its runtime libraries: [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases/tag/20261013), Python 3.13.16. Python's license and bundled-library notices are retained in the runtime. [Python source](https://www.python.org/downloads/source/).
- Qt for Python / PySide6 Essentials and Shiboken 6.12.0: LGPLv3/GPLv3 or commercial licensing. This application uses the LGPL distribution. The shared libraries remain replaceable; reverse engineering for debugging modifications to these libraries is permitted. [Qt source archives](https://download.qt.io/official_releases/qt/6.12/6.12.0/), [PySide source](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0), [LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.html).
- Paramiko, bcrypt, cffi, cryptography, Invoke, PyNaCl, pycparser, typing_extensions, and certifi: their original `*.dist-info` metadata and license files are included in the runtime's `site-packages`.
- Windows desktop archives also include pywinpty 3.1.0 (MIT), providing ConPTY support. [Source and license](https://github.com/andfoy/pywinpty).
- Linux desktop archives include X11/XCB and xkbcommon shared libraries from Ubuntu Jammy packages. Their exact original URLs and checksums are in `desktop-build-lock.json`; each package's copyright and license notices are retained under `platform-libs/licenses`. [XCB source](https://gitlab.freedesktop.org/xorg/lib), [xkbcommon source](https://github.com/xkbcommon/libxkbcommon). No Debian package installation or maintainer scripts run on the user's system.

Recovery backend repositories, Apple firmware, and on-device executables are not
redistributed inside the desktop archive. Existing backend download and licensing
arrangements remain separate from the application's bundled runtime.

To replace a bundled Python or Qt library, work on a separate extracted copy of
the app and replace the corresponding files under `runtime`. The update verifier
rejects modified update installations; use an independently extracted development
copy with `--original` instead. Application source, native launcher source, and the
build recipe are included in the project's source release.
