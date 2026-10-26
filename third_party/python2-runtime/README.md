# Bundled CPython 2.7.18

These maintainer recipes build the interpreter included in Openup desktop
archives. They never install to the system and never access a USB device.

Both use official CPython 2.7.18 source, SHA-256
`b62c0e7937551d0cc02b8fd5cb0f544f9405bafc9a54d3808ed4594812edef43`.
The macOS recipe verifies the included pyenv patches from revision
`0effba62c4bbf8f8ca2ad8f0eabbfd8221cc7bc6` and excludes developer-installed
library discovery. Native build tools and a macOS SDK are maintainer inputs.

```sh
python3 third_party/python2-runtime/build_macos.py arm64
python3 third_party/python2-runtime/build_macos.py x86_64
# Ubuntu 22.04 x86_64; build libraries are listed in the workflow:
python3 third_party/python2-runtime/build_linux.py
```

The Intel recipe needs an Intel Mac or an already available Rosetta runtime.
GitHub's `python2-runtime.yml` builds and checks Linux and native Intel outputs.
Results go under ignored `build/python2-runtime/`. Reviewed ZIPs and their
combined manifest belong under `packaging/python2/`; the desktop builder always
includes the correct checked runtime. Windows uses the Linux archive in WSL.

The runtime consists of a statically linked CPython executable, standard-library
files and extensions needed by local helpers. Optional TLS, database, readline
and GUI extensions are omitted. `ctypes`, callbacks, `struct`, `hashlib`, `zlib`
and `bz2` are exercised. Mac binaries use only system libraries; Linux includes
its libffi/bzip2/zlib dependencies and license notices with relative rpaths.

Build tests relocate the runtime to paths containing spaces and set hostile
Python environment variables. The app wrapper ignores that configuration. The
application's final signing step may rewrite Mach-O signatures; its final
DESKTOP.json covers signed bytes. Archive and per-file checksums in
`packaging/python2/manifest.json` describe the original runtime inputs.

Python's complete license and compatibility-patch notices are retained inside
each runtime. These licenses are independent of Openup's MIT license.
