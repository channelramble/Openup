# Research and scope

Reviewed 2026-10-20.

The requested community is `r/setupapp`. Its discussions distinguish old passcode lockout-state edits from modern Secure Enclave enforcement. Reports are inconsistent across versions, especially around mounting iOS 9/10 data volumes; they are leads, not proof that a particular implementation works.

The [r/setupapp disabled-iPhone discussion](https://www.reddit.com/r/setupapp/comments/v3xw31) describes cached timer state that can survive incomplete edits. The software here uses a limited list of recognized fields and rejects unknown layouts. It does not follow the thread's broad file deletion or unlimited-attempt suggestions.

A [first-hand iPhone 5 write-up and accompanying code discussion](https://gist.github.com/MDX-Tom/b9ac6209d36fce1a652e08e9fab60e61) identifies the legacy SpringBoard preference and lockout-journal locations. Its full workflow is broader than this tool. A zero-counter one-time reset, preserving unrelated preferences, is our implementation decision and still needs device testing.

[Legacy iOS Kit's SSH ramdisk documentation](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/SSH-Ramdisk) documents booting, mounting, and accessing legacy devices. Its [A5 documentation](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/checkm8-a5) explains the additional hardware requirement for iPhone 4S.

The integrated backend downloader pins [Legacy iOS Kit revision 23622f8fe72c09952089d5145a7b008cfcc90d9b](https://github.com/LukeZGD/Legacy-iOS-Kit/tree/23622f8fe72c09952089d5145a7b008cfcc90d9b). The source's `--sshrd` argument starts the ramdisk workflow. Its bundled mount scripts were inspected; this tool instead mounts only the recognized HFS partitions, avoiding their filesystem-repair fallback. An explicitly supplied existing backend can differ and is identified by its script hash when launched. Upstream may download further dependencies or firmware during its workflow.

The [libusbmuxd source](https://github.com/libimobiledevice/libusbmuxd/blob/master/src/libusbmuxd.c) was used to check the local plist protocol, USB-only selection, packet framing, and network-byte-order port representation. [Paramiko's SFTP API](https://docs.paramiko.org/en/stable/api/sftp.html) supplies the file transport. No third-party exploit binaries or firmware are included in this distribution.

[Apple Platform Security](https://support.apple.com/guide/security/passcodes-and-passwords-sec20230a10d/web) documents Secure Enclave delay enforcement on modern devices. That is why this release does not claim modern-iPhone timer removal.

The MacBook SSH connection timed out during development; local usbmuxd reported no connected devices. No phone was modified, no pwnDFU exploit was executed, and no physical compatibility claim was established.
