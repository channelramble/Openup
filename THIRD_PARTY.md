# Bundled desktop components

The application source is MIT licensed. Desktop archives also contain unmodified,
dynamically loaded third-party runtimes and libraries with their own licenses.
Their license files and distribution metadata remain in the bundled runtime.
The complete versions, original artifact URLs, and SHA-256 checksums are recorded
in `desktop-build-lock.json`.

- CPython and its runtime libraries: [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases/tag/20261013), Python 3.13.16. Python's license and bundled-library notices are retained in the runtime. [Python source](https://www.python.org/downloads/source/).
- Qt for Python / PySide6 Essentials and Shiboken: 6.9.3 on both Mac targets, 6.12.0 on Windows/Linux. LGPLv3/GPLv3 or commercial licensing; this application uses the LGPL distribution. The shared libraries remain replaceable; reverse engineering for debugging modifications to these libraries is permitted. The Mac packages omit unused Qt SQL driver plugins that require external database libraries. [Qt 6.9 platform support](https://doc.qt.io/archives/qt-6.9/supported-platforms.html), [Qt 6.9.3 source](https://download.qt.io/archive/qt/6.9/6.9.3/), [Qt 6.12.0 source](https://download.qt.io/official_releases/qt/6.12/6.12.0/), [PySide 6.9.3 source](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.9.3), [PySide 6.12.0 source](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0), [LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.html).
- Paramiko, bcrypt, cffi, cryptography, Invoke, PyNaCl, pycparser, typing_extensions, and certifi: their original `*.dist-info` metadata and license files are included in the runtime's `site-packages`. The Intel Mac compatibility build pins [cryptography 48.0.1](https://pypi.org/project/cryptography/48.0.1/#files); other targets use 50.0.2. Each target's exact wheel inventory is recorded in the build lock.
- Windows desktop archives also include pywinpty 3.1.0 (MIT), providing ConPTY support. [Source and license](https://github.com/andfoy/pywinpty).
- Linux desktop archives include X11/XCB and xkbcommon shared libraries from Ubuntu Jammy packages. Their exact original URLs and checksums are in `desktop-build-lock.json`; each package's copyright and license notices are retained under `platform-libs/licenses`. [XCB source](https://gitlab.freedesktop.org/xorg/lib), [xkbcommon source](https://github.com/xkbcommon/libxkbcommon). No Debian package installation or maintainer scripts run on the user's system.

## iPhone 3G ARMv6 executables

Version 2.0.1 includes `bruteforce`, `device_infos`, and `restored_external` built
from [nabla-c0d3/iphone-dataprotection](https://github.com/nabla-c0d3/iphone-dataprotection/tree/572dd5cd8c07f5f14f7ea9488041031dd22a26bb),
commit `572dd5cd8c07f5f14f7ea9488041031dd22a26bb`. These components retain their
upstream per-file licenses, including the applicable APSL and BSD terms; the
application's MIT license does not replace those terms.

The complete source/build/provenance/license package is included under
`third_party/iphone-dataprotection` in the source checkout and in
`ios_timeout/data/iphone3g-source.zip` in the app. The adjacent
`iphone3g-NOTICE.txt` supplies attribution. `iphone3g-armv6.zip` contains the
executables, with the adjacent JSON manifest recording their verification data.
The ZIP preserves the executables' SHA-1 and SHA-256 code signatures when the
host Mac app is signed. The app checks hashes before extracting the tools into
its private temporary operation directory.

The supplied `restored_external` includes a documented patch removing automatic
filesystem-repair calls and adding a bounded `--sleep SECONDS` mode that returns
before device initialization. Original source copies and the patch are included
beside the build recipe. The managed 3G startup uses a separate verified copy for
waiting, including when custom recovery tools are selected. It skips the upstream
read/write mount script; the recovery executable uses its own read-only mount.

The recovery source also disables stdout buffering before initialization, so
progress remains immediate through the on-phone display wrapper. Its original
source and exact `payload-console-output.patch` are included in the same package.
This changes console output only; it does not change the recovery algorithm.

The tools were built using the iPhoneOS 4.2 SDK with an iOS 4.0 minimum deployment
target. Apple SDKs and firmware are not redistributed. Recovery backend
repositories and firmware continue to be downloaded separately under their
respective terms.

To replace a bundled Python or Qt library, work on a separate extracted copy of
the app and replace the corresponding files under `runtime`. The update verifier
rejects modified update installations; use an independently extracted development
copy with `--original` instead. Application source, native launcher source, and the
build recipe are included in the project's source release.

The original iPhone 2G uses a separate Openup helper built for the 3.1.3 RAM
disk. Its source, recipe, notices and actual-library validation are under
`third_party/iphone2g-helper`, also bundled as `ios_timeout/data/iphone2g-source.zip`.
`iphone2g-helper.zip` preserves its signatures; the adjacent JSON is checked
before extraction. The helper's fixed IOAES key835 interface follows the
documented iPhone Data Protection Tools protocol. The host's independent
legacy-keychain decoder follows the same pinned project's version-3 format,
retaining attribution in `legacy_keychain.py`. It uses the already bundled
cryptography library. Apple SDKs, firmware and proprietary libraries are not
redistributed. Research also consulted the user's MIT-licensed
[iPhone2Gkit](https://github.com/channelramble/iPhone2Gkit/tree/4e23a7434e8bc5e1d8f9a95bd1c8248a2c2bbc75);
its installer, activation and filesystem-repair routines are not included.
