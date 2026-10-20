# Research and scope

Reviewed 2026-10-20.

The requested community is `r/setupapp`. Its discussions distinguish old passcode lockout-state edits from modern Secure Enclave enforcement. Reports are inconsistent across versions, especially around mounting iOS 9/10 data volumes; they are leads, not proof that a particular implementation works.

The [r/setupapp disabled-iPhone discussion](https://www.reddit.com/r/setupapp/comments/v3xw31) describes cached timer state that can survive incomplete edits. The software here uses a limited list of recognized fields and rejects unknown layouts. It does not follow the thread's broad file deletion or unlimited-attempt suggestions.

A [first-hand iPhone 5 write-up and accompanying code discussion](https://gist.github.com/MDX-Tom/b9ac6209d36fce1a652e08e9fab60e61) identifies the legacy SpringBoard preference and lockout-journal locations. Its full workflow is broader than this tool. A zero-counter one-time reset, preserving unrelated preferences, is our implementation decision and still needs device testing.

[Legacy iOS Kit's SSH ramdisk documentation](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/SSH-Ramdisk) documents booting, mounting, and accessing legacy devices. Its [A5 documentation](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/checkm8-a5) explains the additional hardware requirement for iPhone 4S.

The integrated backend downloader pins [Legacy iOS Kit revision 23622f8fe72c09952089d5145a7b008cfcc90d9b](https://github.com/LukeZGD/Legacy-iOS-Kit/tree/23622f8fe72c09952089d5145a7b008cfcc90d9b). The source's `--sshrd` argument starts the ramdisk workflow. Its bundled mount scripts were inspected; this tool instead mounts only the recognized HFS partitions, avoiding their filesystem-repair fallback. An explicitly supplied existing backend can differ and is identified by its script hash when launched. Upstream may download further dependencies or firmware during its workflow.

The [libusbmuxd source](https://github.com/libimobiledevice/libusbmuxd/blob/master/src/libusbmuxd.c) was used to check the local plist protocol, USB-only selection, packet framing, and network-byte-order port representation. [Paramiko's SFTP API](https://docs.paramiko.org/en/stable/api/sftp.html) supplies the file transport. No third-party exploit binaries or firmware are included in this distribution.

[Apple Platform Security](https://support.apple.com/guide/security/passcodes-and-passwords-sec20230a10d/web) documents Secure Enclave delay enforcement on modern devices. That is why this release does not claim modern-iPhone timer removal.

During 1.0/1.1 development, MacBook SSH timed out. During 1.2.0 work it was reachable, but USB inventories on both computers showed no connected iPhone 3G. No phone was modified, no pwnDFU exploit was executed, and no physical compatibility claim was established.
# Version 1.2.0 integration findings — 2026-10-20

- Requested backend: https://github.com/tuanemss/32bit-Bruteforce-Passcode
  Pinned commit: `4949674b318767de83eb149e57bcc543a6453d6e`.
  `Bruteforce.sh` SHA-256: `c6990c56f9631c91d1673246fa794e275e210211e5538a8dcf3b647a4649075b`.
  The source is downloaded separately; this app does not redistribute its binary bundle.
- Direct inspection with macOS `file` found ARMv7 Mach-O executables in upstream
  `resources/bruteforce`, `device_infos`, and `restored_external`.
  `bruteforce` SHA-256: `f2f39fc039974bd9a34b297fb4bd4c9c687cdd67624dfd92440b34612fd5714d`.
  The upstream README's broad 32-bit support claim does not establish ARMv6 support.
- The authoritative libirecovery device table maps iPhone 3G to `iPhone1,2`,
  `n82ap`, CPID `0x8900`, BDID `0x04`. `m68ap` is iPhone1,1.
  https://github.com/libimobiledevice/libirecovery/blob/master/src/libirecovery.c
- The pinned backend strips `ap` from its board name but omits it from fallback
  iBSS/iBEC filenames. Legacy firmware key indexes omit those unencrypted images.
  It also selects a 4.2.1 kernel for 3G while forcing the generic older kernel
  filename and using a patch matching the Legacy iOS Kit 3.1.3 kernel patch.
  The adapter uses the 3G 7E18 ramdisk profile and ensures the patched legacy
  kernel is repacked into the component that is actually booted.
- Legacy iOS Kit's pinned `device_ramdisk` chooses 3.1.3/7E18 for iPhone1,2:
  https://github.com/LukeZGD/Legacy-iOS-Kit/blob/23622f8fe72c09952089d5145a7b008cfcc90d9b/restore.sh
  Installed iOS and boot-ramdisk iOS are distinct.
- The upstream key metadata for iPhone1,2/8C148 names `kernelcache.release.n82`;
  7E18 names `kernelcache.release.s5l8900x`.
  https://github.com/LukeZGD/Legacy-iOS-Kit-Keys/tree/master/iPhone1%2C2
- Windows libusbmuxd uses a loopback TCP endpoint; macOS/Linux use a Unix socket:
  https://github.com/libimobiledevice/libusbmuxd/blob/master/src/libusbmuxd.c
- WSL USB attachment requires usbipd-win and WSL 2; device reconnections need
  attention during DFU transitions:
  https://learn.microsoft.com/en-us/windows/wsl/connect-usb
- Exact device failure logs and a connected iPhone 3G were unavailable during
  this work. The changes address identified source defects; they do not prove
  the reported physical device failure is resolved.
- A bounded range read of Apple's 3G 7E18 IPSW confirmed its Restore.plist
  identity and the 109,940-byte `iBSS.n82ap.RELEASE.dfu` member:
  https://secure-appldnld.apple.com/iPhone/061-7468.20100202.pbnrt/iPhone1,2_3.1.3_7E18_Restore.ipsw
  The 3G preparation route seeds this fixed profile's Apple URL. The metadata
  API returned HTTP 403 in this session; the GitHub metadata mirror succeeded.
  The integrated brute-force backend now makes curl reject HTTP error responses
  and validates metadata JSON before accepting it.
